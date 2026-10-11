from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

import os

# Адрес базы берётся из переменной окружения; если её нет, работаем со старой SQLite
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./tracker.db")

# Приводим адрес к виду, который понимает SQLAlchemy с драйвером psycopg
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

if DATABASE_URL.startswith("sqlite"):
    # connect_args={"check_same_thread": False} нужен специально для SQLite в многопоточном FastAPI
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,   # проверять соединение перед использованием
        pool_size=3,
        max_overflow=2,
        pool_recycle=300,
        connect_args={"prepare_threshold": None},
    )
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
