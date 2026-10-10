import hashlib
import secrets
from typing import Optional, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, status
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
    allow_credentials=True,
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
    return user

@app.post("/api/login", response_model=UserResponse)
def login_user(data: UserLoginRequest, db: Session = Depends(get_db)):
    clean_username = data.username.strip()
    clean_password = data.password.strip()

    user = db.query(models.User).filter(models.User.username == clean_username).first()
    if not user or not verify_password(user.password_hash, clean_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверное имя пользователя или пароль"
        )
    return user

import json

@app.post("/api/schedule/sync")
def sync_schedule(payload: ScheduleSyncRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == payload.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    
    # Сохраняем весь JSON приложения в базу
    user.cloud_data = json.dumps(payload.database_state)
    db.commit()
    return {"status": "synced", "user_id": payload.user_id}

@app.get("/api/schedule/load/{user_id}")
def load_schedule(user_id: int, db: Session = Depends(get_db)):
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