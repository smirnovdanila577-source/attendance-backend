import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Optional, Dict, Any

from fastapi import FastAPI, Depends, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel

from parser import get_schedule
import models
from database import engine, get_db

# Автоматически создаем таблицы в базе данных
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Attendance Tracker API")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=False,  # куки не используются (авторизация через заголовок Authorization)
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- БЕЗОПАСНОСТЬ ---
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return f"{salt}${key.hex()}"

def verify_password(stored_password_hash: str, provided_password: str) -> bool:
    try:
        salt, key_hex = stored_password_hash.split("$")
        check_key = hashlib.pbkdf2_hmac("sha256", provided_password.encode("utf-8"), salt.encode("utf-8"), 100_000)
        return secrets.compare_digest(key_hex, check_key.hex())
    except Exception:
        return False

# --- ТОКЕНЫ ДОСТУПА (подписанные, без хранения в базе) ---
# ВАЖНО: на хостинге задайте переменную окружения TOKEN_SECRET (длинная случайная строка).
# Если её нет, ключ генерируется заново при каждом запуске, и все пользователи будут «разлогиниваться».
TOKEN_SECRET = os.environ.get("TOKEN_SECRET") or secrets.token_hex(32)
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 дней
MAX_CLOUD_DATA_BYTES = 2_000_000       # лимит на размер данных одного пользователя

def make_token(user_id: int) -> str:
    exp = int(time.time()) + TOKEN_TTL_SECONDS
    msg = f"{user_id}.{exp}"
    sig = hmac.new(TOKEN_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{msg}.{sig}"

def read_token(token: str) -> Optional[int]:
    try:
        uid, exp, sig = token.split(".")
        expected = hmac.new(TOKEN_SECRET.encode("utf-8"), f"{uid}.{exp}".encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(exp) < time.time():
            return None
        return int(uid)
    except Exception:
        return None

def get_current_user_id(authorization: Optional[str] = Header(None)) -> int:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется авторизация")
    user_id = read_token(authorization[7:].strip())
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Сессия истекла, войдите заново")
    return user_id

# --- ЗАЩИТА ОТ ПЕРЕБОРА ПАРОЛЯ (в памяти процесса) ---
_failed_logins: Dict[str, list] = {}
MAX_FAILED_ATTEMPTS = 8
FAIL_WINDOW_SECONDS = 600

def _login_key(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{ip}|{username.lower()}"

def _check_login_allowed(key: str):
    now = time.time()
    attempts = [t for t in _failed_logins.get(key, []) if now - t < FAIL_WINDOW_SECONDS]
    _failed_logins[key] = attempts
    if len(attempts) >= MAX_FAILED_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Слишком много попыток входа. Попробуйте через несколько минут")

# --- СХЕМЫ ДАННЫХ ---
class UserRegisterRequest(BaseModel):
    username: str
    password: str
    group_name: Optional[str] = None

class UserLoginRequest(BaseModel):
    username: str
    password: str

class UserResponse(BaseModel):
    id: int
    username: str
    university: str
    group_name: Optional[str]
    token: Optional[str] = None

    class Config:
        from_attributes = True

class ScheduleSyncRequest(BaseModel):
    user_id: int
    database_state: dict

# --- ЭНДПОИНТЫ ---
@app.get("/")
def root():
    return {"status": "ok", "message": "Сервер работает!"}

@app.post("/api/register", response_model=UserResponse)
def register_user(data: UserRegisterRequest, db: Session = Depends(get_db)):
    clean_username = data.username.strip()
    clean_password = data.password.strip()

    if not clean_username or not clean_password:
        raise HTTPException(status_code=400, detail="Логин и пароль обязательны")

    existing_user = db.query(models.User).filter(models.User.username == clean_username).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Пользователь с таким именем уже существует")

    user = models.User(
        username=clean_username,
        password_hash=hash_password(clean_password),
        university="ННГУ им. Лобачевского",
        group_name=data.group_name.strip() if data.group_name else None
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "id": user.id, "username": user.username, "university": user.university,
        "group_name": user.group_name, "token": make_token(user.id)
    }

@app.post("/api/login", response_model=UserResponse)
def login_user(data: UserLoginRequest, request: Request, db: Session = Depends(get_db)):
    clean_username = data.username.strip()
    clean_password = data.password.strip()

    key = _login_key(request, clean_username)
    _check_login_allowed(key)

    user = db.query(models.User).filter(models.User.username == clean_username).first()
    if not user or not verify_password(user.password_hash, clean_password):
        _failed_logins.setdefault(key, []).append(time.time())
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверное имя пользователя или пароль"
        )
    _failed_logins.pop(key, None)
    return {
        "id": user.id, "username": user.username, "university": user.university,
        "group_name": user.group_name, "token": make_token(user.id)
    }

import json

@app.post("/api/schedule/sync")
def sync_schedule(payload: ScheduleSyncRequest, current_user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    # Менять можно только свои данные
    if payload.user_id != current_user_id:
        raise HTTPException(status_code=403, detail="Нет доступа к чужим данным")
    user = db.query(models.User).filter(models.User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    # Сохраняем весь JSON приложения в базу
    serialized = json.dumps(payload.database_state)
    if len(serialized.encode("utf-8")) > MAX_CLOUD_DATA_BYTES:
        raise HTTPException(status_code=413, detail="Слишком большой объём данных")
    user.cloud_data = serialized
    db.commit()
    return {"status": "synced", "user_id": payload.user_id}

@app.get("/api/schedule/load/{user_id}")
def load_schedule(user_id: int, current_user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
    if user_id != current_user_id:
        raise HTTPException(status_code=403, detail="Нет доступа к чужим данным")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    # Если у пользователя уже есть данные в облаке, отдаем их
    if user.cloud_data:
        return json.loads(user.cloud_data)
    return {}

# --- ПОЛУЧЕНИЕ РАСПИСАНИЯ ЧЕРЕЗ ПАРСЕР ---
@app.get("/api/schedule")
def get_schedule_route(group: str, date: str = None):
    from datetime import datetime
    
    # Если фронтенд прислал дату (например, для следующей недели) — используем её
    # Если нет — используем сегодняшнюю дату
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
        
    return get_schedule(group, date)

from parser import get_schedule
from datetime import datetime

@app.get("/api/schedule_v2")
def get_schedule_route_v2(group: str, date: str = None):
    # Если дата не пришла, берем сегодняшнюю
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    
    print(f"[Бэкенд] Запрошена группа {group} на дату {date}")
    return get_schedule(group, date)
