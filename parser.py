import re
import json
import os
from datetime import datetime
from curl_cffi import requests

# Загружаем статичную базу групп
DB_PATH = os.path.join(os.path.dirname(__file__), "groups.json")
GROUP_DB = {}

if os.path.exists(DB_PATH):
    try:
        with open(DB_PATH, "r", encoding="utf-8") as f:
            GROUP_DB = json.load(f)
    except Exception as e:
        print(f"[Парсер] Ошибка чтения базы: {e}")

def normalize_name(name: str) -> str:
    """Удаляем пробелы и переводим латиницу в кириллицу (защита от опечаток)"""
    clean = name.strip().lower().replace(" ", "")
    for en, ru in zip('abcehkmoptxy', 'авсенкмортху'):
        clean = clean.replace(en, ru)
    return clean

def find_group_id(group_name: str) -> str:
    norm = normalize_name(group_name)
    if norm in GROUP_DB:
        return GROUP_DB[norm]
    # Поиск по частичному совпадению (например, если ввели "0524")
    for db_name, db_id in GROUP_DB.items():
        if norm in db_name or db_name in norm:
            return db_id
    return None

# --- ТИП ЗАНЯТИЯ ---
# Сайт ННГУ не отдаёт тип отдельным полем, поэтому определяем его по нескольким признакам:
#   1) слова-маркеры в служебных частях описания (не в названии предмета и не в фамилии преподавателя);
#   2) легенда цветов на странице («жёлтый = лекция» и т.п.);
#   3) «голосование» по цвету: цвет занятия берём у большинства пар этого же цвета, у которых тип удалось определить.
TYPE_LECTURE = "Лекция"
TYPE_PRACTICE = "Практика"
TYPE_LAB = "Лабораторная"

_RU = "[а-яё]"
# Границы слов проверяем вручную: \b в Python плохо дружит с кириллицей в сочетании с точками
_TYPE_PATTERNS = [
    (TYPE_LAB, re.compile(rf"(?<!{_RU})лаб(?:ор{_RU}*|(?!{_RU}))", re.I)),
    (TYPE_PRACTICE, re.compile(rf"(?<!{_RU})(?:практ{_RU}*|семинар{_RU}*|пр|сем)\.?(?!{_RU})", re.I)),
    (TYPE_LECTURE, re.compile(rf"(?<!{_RU})(?:лекци{_RU}*|лек)\.?(?!{_RU})", re.I)),
]
# Служебные поля события, в которых типа точно нет
_SKIP_KEYS = {"start", "end", "color", "tooltip", "title", "id", "allDay", "url", "textColor", "borderColor", "backgroundColor"}


def _strip_tags(text) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(text or ""))).strip()


def type_from_text(text):
    """Тип занятия по тексту: лабораторная важнее практики, практика важнее лекции. Нет маркера, вернёт None."""
    text = _strip_tags(text)
    if not text:
        return None
    for kind, pattern in _TYPE_PATTERNS:
        if pattern.search(text):
            return kind
    return None


def parse_color_legend(html: str) -> dict:
    """Ищет на странице подписи вида <span style="background:#ccffcc">Практика</span> и возвращает {цвет: тип}."""
    legend = {}
    pattern = re.compile(r"(?:background(?:-color)?\s*:\s*|bgcolor\s*=\s*[\"']?)(#[0-9a-fA-F]{3,6})[^>]*>([^<]{2,60})<", re.I)
    for m in pattern.finditer(html or ""):
        color = m.group(1).lower()
        kind = type_from_text(m.group(2))
        if kind and color not in legend:
            legend[color] = kind
    return legend


def detect_types(events: list, subjects: list, html: str):
    """Возвращает (список типов для каждого события, легенда, голоса по цветам)."""
    legend = parse_color_legend(html)
    explicit = []
    votes = {}

    for event, subject in zip(events, subjects):
        texts = []
        tooltip_parts = [x.strip() for x in str(event.get("tooltip", "")).split("<br>")]
        # Первая часть описания это название предмета: в нём ищем тип только по уже разобранным остальным частям
        texts.extend(_strip_tags(x) for x in tooltip_parts[1:])
        title = _strip_tags(event.get("title", ""))
        if title and subject:
            title = title.replace(subject.strip(), " ")
        texts.append(title)
        for key, value in event.items():
            if key in _SKIP_KEYS:
                continue
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, (list, tuple)):
                texts.extend(str(v) for v in value)
        found = None
        for t in texts:
            found = type_from_text(t)
            if found:
                break
        explicit.append(found)
        color = str(event.get("color", "")).lower()
        if found and color:
            votes.setdefault(color, {})
            votes[color][found] = votes[color].get(found, 0) + 1

    result = []
    for event, found, subject in zip(events, explicit, subjects):
        color = str(event.get("color", "")).lower()
        kind = found or legend.get(color)
        if not kind and color in votes:
            kind = max(votes[color].items(), key=lambda kv: kv[1])[0]
        if not kind:
            # Последняя надежда: тип явно назван в самом предмете («Лабораторный практикум»)
            low = subject.lower()
            if "лабораторн" in low:
                kind = TYPE_LAB
            elif "практикум" in low:
                kind = TYPE_PRACTICE
        result.append(kind or TYPE_LECTURE)
    return result, legend, votes


def format_date_to_unn(date_str: str) -> str:
    try:
        if "-" in date_str:
            p = date_str.split("-")
            if len(p) == 3 and len(p[0]) == 4:
                return f"{p[2]}.{p[1]}.{p[0]}"
    except:
        pass
    return date_str

def get_schedule(group_name: str, target_date: str, debug: bool = False) -> dict:
    group_id = find_group_id(group_name)
    
    if not group_id:
        return {"error": f"Группа '{group_name}' не найдена. Убедитесь, что файл groups.json лежит в папке backend."}

    formatted_date = format_date_to_unn(target_date)
    url = "https://rasp.unn.ru/schedule.php"
    
    payload = {
        "gr": group_id, 
        "date": formatted_date, 
        "type": "2", 
        "view": "1", 
        "typemenu": "1", 
        "lang": "0", 
        "wid": "150"
    }

    # Используем curl_cffi с имитацией Chrome, чтобы сервер ННГУ отдал расписание без ошибок
    session = requests.Session(impersonate="chrome120")
    
    try:
        response = session.post(url, data=payload, params={"date": formatted_date}, timeout=10)
        match = re.search(r'events:\s*(\[.*?\]),?\s*\}\);', response.text, re.DOTALL)
        
        if not match:
            return {"error": f"Расписание для '{group_name}' сейчас пустое или закрыто ННГУ."}

        events_list = json.loads(match.group(1))
        schedule_data = {}
        weekdays = ["ВС", "ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ"]

        # Название предмета нужно заранее, чтобы не искать тип занятия в самом названии
        subjects = []
        for event in events_list:
            tip = event.get("tooltip", "").split("<br>")
            subjects.append((tip[0] if tip else event.get("title", "")).strip())
        lesson_types, legend, votes = detect_types(events_list, subjects, response.text)

        for event, lesson_type in zip(events_list, lesson_types):
            start_dt = datetime.fromisoformat(event["start"])
            end_dt = datetime.fromisoformat(event["end"])
            day_idx = int(start_dt.strftime("%w"))
            day_key = f"{weekdays[day_idx]} {start_dt.strftime('%d.%m')}"
            
            parts = event.get("tooltip", "").split("<br>")
            subject = parts[0] if len(parts) > 0 else event.get("title", "")
            teacher = parts[1] if len(parts) > 1 else ""
            room = parts[2] if len(parts) > 2 else ""

            time_str = f"{start_dt.strftime('%H:%M')} - {end_dt.strftime('%H:%M')}"
            lesson = {
                "time": time_str, 
                "subject": subject.strip(),
                "info": f"{teacher} | {room}".strip(" |"), 
                "type": lesson_type,
                "color": event.get("color", "#ffffff")
            }
            if day_key not in schedule_data: 
                schedule_data[day_key] = []
            schedule_data[day_key].append(lesson)

        # Сортируем уроки по времени
        sorted_schedule = dict(sorted(schedule_data.items(), key=lambda item: item[1][0]['time']))
        result = {"status": "success", "group": group_name, "data": sorted_schedule}
        if debug:
            # Для проверки: открой /api/schedule_v2?group=...&debug=true и посмотри, что реально приходит с сайта
            result["debug"] = {
                "legend": legend,
                "votes_by_color": votes,
                "detected_types": lesson_types,
                "sample_events": events_list[:6],
            }
        return result

    except Exception as e:
        return {"error": f"Сбой скачивания расписания: {str(e)}"}
