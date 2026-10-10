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

def format_date_to_unn(date_str: str) -> str:
    try:
        if "-" in date_str:
            p = date_str.split("-")
            if len(p) == 3 and len(p[0]) == 4:
                return f"{p[2]}.{p[1]}.{p[0]}"
    except:
        pass
    return date_str

def get_schedule(group_name: str, target_date: str) -> dict:
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

        for event in events_list:
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
                "color": event.get("color", "#ffffff")
            }
            if day_key not in schedule_data: 
                schedule_data[day_key] = []
            schedule_data[day_key].append(lesson)

        # Сортируем уроки по времени
        sorted_schedule = dict(sorted(schedule_data.items(), key=lambda item: item[1][0]['time']))
        return {"status": "success", "group": group_name, "data": sorted_schedule}

    except Exception as e:
        return {"error": f"Сбой скачивания расписания: {str(e)}"}