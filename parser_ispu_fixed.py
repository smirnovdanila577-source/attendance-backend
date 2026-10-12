"""
Парсер расписания ИГЭУ (http://schedule.ispu.ru/).
"""

import re
import time
from threading import Lock

import requests
from bs4 import BeautifulSoup

BASE_URL = "http://schedule.ispu.ru/"

PREFIX = "ctl00$ContentPlaceHolder1$"
F_SCHEDULE = PREFIX + "ddlSchedule"
F_FACULTY = PREFIX + "ddlSubDivision"
F_COURSE = PREFIX + "ddlCorse"
F_GROUP = PREFIX + "ddlObjectValue"

SCHEDULE_VALUES = {
    "permanent": "144",
    "lecture": "143"
}

DAY_KEYS = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]

KIND_WORDS = {
    "лек": "лекция",
    "сем": "практика",
    "пр": "практика",
    "практ": "практика",
    "лаб": "лабораторная"
}

FALLBACK_FACULTIES = {
    "30000": "ИВТФ",
    "10000": "ИФФ",
    "40000": "ТЭФ",
    "20000": "ФЭУ",
    "60100": "ЭМФ",
    "59900": "ЭЭФ"
}

TTL_FACULTIES = 3600 * 24 * 30
TTL_GROUPS = 3600 * 24
TTL_SCHEDULE = 3600 * 6

_cache = {}
_cache_lock = Lock()

# Чтобы два одинаковых запроса не парсили сайт ИГЭУ одновременно: второй ждёт первого и берёт результат из кэша
_inflight_guard = Lock()
_inflight_locks = {}


def _cache_get(key: str, ttl: int = 3600):
    with _cache_lock:
        if key in _cache:
            data, ts = _cache[key]
            if time.time() - ts < ttl:
                return data
    return None


def _cache_set(key: str, data):
    with _cache_lock:
        _cache[key] = (data, time.time())


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def _new_session():
    ss = requests.Session()
    ss.headers.update(HEADERS)
    return ss


def _parse(response):
    response.raise_for_status()
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, "html.parser")
    soup.raw_html = response.text  # нужен, чтобы таблицу расписания разобрать «как в браузере»
    return soup


def _collect_form(soup: BeautifulSoup) -> dict:
    form = soup.find("form", id="form1") or soup.find("form")
    data = {}
    if not form:
        return data

    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name:
            continue

        tag = el.name.lower()

        if tag == "select":
            opt = el.find("option", selected=True)
            if not opt:
                opt = el.find("option")
            if opt:
                data[name] = opt.get("value", opt.get_text(strip=True))

        elif tag == "input":
            type_ = el.get("type", "text").lower()
            if type_ in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    data[name] = el.get("value", "on")
            elif type_ not in ("submit", "button", "image", "file", "reset"):
                data[name] = el.get("value", "")

        elif tag == "textarea":
            data[name] = el.get_text()

    return data


def _postback(session, soup: BeautifulSoup, target: str, overrides: dict = None):
    data = _collect_form(soup)
    if overrides:
        data.update(overrides)

    data["__EVENTTARGET"] = target
    data["__EVENTARGUMENT"] = ""

    return _parse(session.post(BASE_URL, data=data, timeout=60))


def _options(soup, field_name: str) -> list[dict]:
    sel = soup.find("select", attrs={"name": field_name})
    if not sel:
        return []

    return [
        {"id": o.get("value"), "name": o.get_text(strip=True)}
        for o in sel.find_all("option")
    ]


def _selected_text(soup, field_name: str) -> str:
    sel = soup.find("select", attrs={"name": field_name})
    if sel:
        opt = sel.find("option", selected=True)
        if opt:
            return opt.get_text(strip=True)
    return ""


def _open_start_page(kind="permanent"):
    wanted = SCHEDULE_VALUES.get(kind, "144")
    session = _new_session()
    soup = _parse(session.get(BASE_URL))

    current = _selected_text(soup, F_SCHEDULE)
    if any(o.get("value") == wanted for o in soup.find_all("option")):
        if current != wanted:
            soup = _postback(session, soup, F_SCHEDULE, {F_SCHEDULE: wanted})

    return session, soup


def get_faculties(kind="permanent"):
    cached = _cache_get(f"faculties_{kind}", TTL_FACULTIES)
    if cached:
        return cached

    try:
        session, soup = _open_start_page(kind)
        faculties = _options(soup, F_FACULTY)

        if not faculties:
            faculties = [
                {"id": k, "name": v} for k, v in FALLBACK_FACULTIES.items()
            ]

        for f in faculties:
            soup = _postback(session, soup, F_FACULTY, {F_FACULTY: f["id"]})
            courses = _options(soup, F_COURSE)
            f["courses"] = [{"id": c["id"], "name": c["name"]} for c in courses]

        res = {"success": True, "faculties": faculties}
        _cache_set(f"faculties_{kind}", res)
        return res
    except Exception as e:
        faculties = [
            {"id": k, "name": v} for k, v in FALLBACK_FACULTIES.items()
        ]
        for f in faculties:
            f["courses"] = [{"id": str(i), "name": str(i)} for i in range(1, 6)]
        return {"success": False, "faculties": faculties}


def _goto_course(session, soup, faculty: str, course: str):
    soup = _postback(session, soup, F_FACULTY, {F_FACULTY: faculty})
    soup = _postback(session, soup, F_COURSE, {F_COURSE: course})
    return soup


def get_groups(faculty: str, course: str, kind="permanent"):
    if not str(faculty).isdigit() or not str(course).isdigit():
        return {"success": False, "error": "Неверные параметры факультета или курса"}

    key = f"groups:{kind}:{faculty}:{course}"
    cached = _cache_get(key, TTL_GROUPS)
    if cached:
        return cached

    try:
        session, soup = _open_start_page(kind)
        soup = _goto_course(session, soup, faculty, course)
        groups = _options(soup, F_GROUP)
        if not groups:
            return {"success": False, "error": "Для этого факультета и курса групп не найдено"}

        res = {"success": True, "faculty": faculty, "course": course, "groups": groups}
        _cache_set(key, res)
        return res
    except Exception as e:
        return {"success": False, "error": f"Не удалось получить список групп ИГЭУ: {e}"}


def _get_schedule_ispu_uncached(faculty: str, course: str, group: str, kind="permanent", subgroup_name=None):
    if not str(faculty).isdigit() or not str(course).isdigit() or not str(group).isdigit():
        return {"success": False, "error": "Неверные параметры группы"}

    if kind not in SCHEDULE_VALUES:
        kind = "permanent"

    if subgroup_name:
        subgroup_name = subgroup_name.lower().replace('x', 'х') # Англ -> Рус

    key = f"schedule:{kind}:{faculty}:{course}:{group}:{subgroup_name}"
    cached = _cache_get(key, TTL_SCHEDULE)
    if cached:
        return cached

    try:
        session, soup = _open_start_page(kind)
        soup = _goto_course(session, soup, faculty, course)
        soup = _postback(session, soup, F_GROUP, {F_GROUP: group})

        # --- ОБРАБОТКА ПОДГРУПП ---
        F_SUBGROUP_PREFIX = "ctl00$ContentPlaceHolder1$rblSubGroup"
        rbl = soup.find("table", id="ContentPlaceHolder1_rblSubGroup")
        available_subgroups = []
        
        if rbl:
            for i, inp in enumerate(rbl.find_all("input", type="radio")):
                val = inp.get("value")
                label = rbl.find("label", {"for": inp.get("id")})
                name = label.get_text(strip=True).lower() if label else str(i)
                name = name.replace('x', 'х')
                available_subgroups.append({
                    "index": i,
                    "id": val,
                    "name": name,
                    "checked": inp.has_attr("checked")
                })
        
        applied_subgroup = None
        if available_subgroups:
            target_sg = None
            if subgroup_name:
                target_sg = next((s for s in available_subgroups if s["name"] == subgroup_name), None)
                
            if not target_sg:
                target_sg = next((s for s in available_subgroups if s["checked"]), available_subgroups[0])
            
            applied_subgroup = target_sg["name"]
            
            # ВАЖНОЕ ИСПРАВЛЕНИЕ: Делаем постбэк ТОЛЬКО если нужная подгруппа ЕЩЕ НЕ ВЫБРАНА.
            # Если она уже checked, таблица уже на странице.
            if not target_sg["checked"]:
                event_target = f"{F_SUBGROUP_PREFIX}${target_sg['index']}"
                form_overrides = {F_SUBGROUP_PREFIX: target_sg['id']}
                soup = _postback(session, soup, event_target, form_overrides)
        # --------------------------

        res = parse_schedule_page(soup)
        if not res.get("success"):
            return res

        res["group"] = group
        res["subgroups"] = [s["name"] for s in available_subgroups]  # какие подгруппы есть у группы
        if applied_subgroup:
            res["subgroup"] = applied_subgroup
            
        _cache_set(key, res)
        if applied_subgroup:
            # тот же результат доступен и по явно названной подгруппе (первый запрос обычно идёт без неё)
            _cache_set(f"schedule:{kind}:{faculty}:{course}:{group}:{applied_subgroup}", res)
        return res

    except Exception as e:
        return {"success": False, "error": f"Сбой загрузки расписания ИГЭУ: {e}"}


def get_schedule_ispu(faculty: str, course: str, group: str, kind="permanent", subgroup_name=None):
    """Публичная функция: то же самое, но одинаковые одновременные запросы выполняются по очереди (см. _inflight_locks)."""
    lock_key = f"{kind}:{faculty}:{course}:{group}"
    with _inflight_guard:
        lock = _inflight_locks.setdefault(lock_key, Lock())
    with lock:
        return _get_schedule_ispu_uncached(faculty, course, group, kind, subgroup_name)


def _norm_time(text):
    m = re.search(r"(\d{1,2})[.:](\d{2})\s*-\s*(\d{1,2})[.:](\d{2})", text)
    if not m:
        return "00:00 - 00:00"
    a, b, c, d = m.groups()
    return f"{int(a):02d}:{b} - {int(c):02d}:{d}"


def _split_time(norm):
    start, end = norm.split(" - ")
    return start, end


def _clean(text):
    return re.sub(r"\s+", " ", text).replace(" ", " ").strip()


def parse_lesson(text):
    text = re.sub(r'\((\d+)\)', r'(\1)', text)
    m = re.search(r"(?<!\S)(лек|сем|лаб|практ|пр)\.(?=\s|$)", text, re.I)
    if not m:
        return {"subject": text, "kind": "", "teacher": "", "room": ""}

    subject = text[:m.start()].strip()
    kind = m.group(1).lower()
    rest = text[m.end():].strip()

    tokens = [t.strip() for t in rest.split(" ") if t.strip()]
    if not tokens:
        return {"subject": subject, "kind": kind, "teacher": "", "room": ""}

    room = tokens[-1]
    teacher = " ".join(tokens[:-1])

    return {"subject": subject, "kind": kind, "teacher": teacher, "room": room}


def parse_schedule_table(table, slots_out=None):
    if not table:
        return {}

    grid = {}
    rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
    for r, tr in enumerate(rows):
        c = 0
        for td in tr.find_all(["td", "th"], recursive=False):
            while (r, c) in grid:
                c += 1
            rs = int(td.get("rowspan", 1) or 1)
            cs = int(td.get("colspan", 1) or 1)
            for dr in range(rs):
                for dc in range(cs):
                    if dr == 0 and dc == 0:
                        grid[(r + dr, c + dc)] = td
                    else:
                        grid[(r + dr, c + dc)] = ('SPAN', r, c)
            c += cs

    weeks = {1: {d: [] for d in DAY_KEYS}, 2: {d: [] for d in DAY_KEYS}}

    for r in range(2, len(rows)):
        week_cell = grid.get((r, 0))
        time_cell = grid.get((r, 1))

        if week_cell is None or time_cell is None:
            continue
            
        if isinstance(week_cell, tuple):
            week_cell = grid.get((week_cell[1], week_cell[2]))
        if isinstance(time_cell, tuple):
            time_cell = grid.get((time_cell[1], time_cell[2]))

        if week_cell is None or time_cell is None:
            continue

        week_text = _clean(week_cell.get_text(" "))
        week = int(week_text) if week_text.isdigit() else 1

        slot = _norm_time(time_cell.get_text(" "))
        if slot == "00:00 - 00:00":
            continue
            
        if slots_out is not None and slot not in slots_out:
            slots_out.append(slot)  # собираем сетку звонков (время каждой строки таблицы)

        start_time, end_time = _split_time(slot)

        for col_offset, day_key in enumerate(DAY_KEYS):
            col = col_offset + 2
            entry = grid.get((r, col))
            
            if entry is None or isinstance(entry, tuple):
                continue
                
            raw_text = _clean(entry.get_text(" "))
            if not raw_text:
                continue

            # Пара, растянутая на несколько строк (например, лабораторная на 2 пары), даёт по записи на каждую строку
            span = min(max(int(entry.get("rowspan", 1) or 1), 1), 4)
            time_ranges = []
            for k in range(span):
                cell_k = grid.get((r + k, 1))
                if isinstance(cell_k, tuple):
                    cell_k = grid.get((cell_k[1], cell_k[2]))
                slot_k = _norm_time(cell_k.get_text(" ")) if cell_k is not None else "00:00 - 00:00"
                if slot_k != "00:00 - 00:00":
                    time_ranges.append(slot_k)
            if not time_ranges:
                time_ranges = [slot]

            bg_color = "#ffffff"
            style_m = re.search(r"background:\s*(#[0-9A-Fa-f]{6})", entry.get("style", ""), re.I)
            if style_m:
                bg_color = style_m.group(1).lower()

            parts = [p.strip() for p in raw_text.split(";") if p.strip()] or [raw_text]
            for time_range in time_ranges:
                for part in parts:
                    les = parse_lesson(part)
                    kind_word = KIND_WORDS.get(les["kind"], les["kind"])
                    info = " ".join([x for x in (kind_word, les["teacher"], les["room"]) if x])

                    weeks[week][day_key].append({
                        "time": time_range,
                        "subject": les["subject"],
                        "info": info,
                        "teacher": les["teacher"],
                        "room": les["room"],
                        "kind": les["kind"] or "лекция",
                        "color": bg_color
                    })

    return weeks


def _browser_soup(soup):
    """Разбор HTML так, как это делает браузер (достраивает незакрытые теги). Если нужных библиотек нет, вернёт None."""
    raw = vars(soup).get("raw_html")
    if not raw:
        return None
    for name in ("html5lib", "lxml"):
        try:
            return BeautifulSoup(raw, name)
        except Exception:
            continue
    return None


def _find_schedule_table(s):
    return s.find("table", class_="shedule", id="sheduleTable") or s.find("table", id="sheduleTable")


def parse_schedule_page(soup):
    table = None
    browser = _browser_soup(soup)
    if browser is not None:
        table = _find_schedule_table(browser)
    if table is None:
        table = _find_schedule_table(soup)
    if not table:
        return {"success": False, "error": "На странице ИГЭУ не найдена таблица расписания"}

    bells = []
    weeks = parse_schedule_table(table, bells)
    if not any(any(day for day in w.values()) for w in weeks.values()):
        return {"success": False, "error": "Расписание этой группы сейчас пустое"}

    current_week = 1
    m = re.search(r"(\d+)\s*учебная", soup.get_text())
    if m:
        current_week = int(m.group(1))

    return {
        "success": True,
        "university": "ispu",
        "current_week": current_week,
        "bells": bells,
        "weeks": weeks
    }
