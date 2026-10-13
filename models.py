from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, Text
from sqlalchemy.orm import relationship
from database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    university = Column(String, default="ННГУ им. Лобачевского")
    group_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # НОВАЯ КОЛОНКА ДЛЯ ОБЛАЧНОГО ХРАНЕНИЯ РАСПИСАНИЯ
    cloud_data = Column(Text, nullable=True) 

    schedule = relationship("ScheduleItem", back_populates="user", cascade="all, delete-orphan")
    attendance = relationship("AttendanceRecord", back_populates="user", cascade="all, delete-orphan")

    # Связи для будущего сохранения пар и посещений
    schedule = relationship("ScheduleItem", back_populates="user", cascade="all, delete-orphan")
    attendance = relationship("AttendanceRecord", back_populates="user", cascade="all, delete-orphan")


class ScheduleItem(Base):
    __tablename__ = "schedule_items"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    day = Column(String, nullable=False)
    time = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    pair_type = Column(String, default="Лекция")
    week_type = Column(String, default="both")

    user = relationship("User", back_populates="schedule")


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    schedule_item_id = Column(Integer, ForeignKey("schedule_items.id"), nullable=True)
    week_number = Column(Integer, nullable=False)
    day = Column(String, nullable=False)
    status = Column(Boolean, nullable=True)

    user = relationship("User", back_populates="attendance")

# --- СОЦИАЛЬНАЯ ЧАСТЬ: публичный профиль, очки по неделям, друзья ---
# Отдельные таблицы, чтобы не менять уже существующую таблицу users (create_all создаст их сам).

class PublicProfile(Base):
    """То, что видят другие: ник, аватар, тег, достижения, серия, очки. Обновляет само приложение."""
    __tablename__ = "public_profiles"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    avatar = Column(String, default="")            # id готового аватара, пусто — буква на цветном фоне
    tag = Column(String, default="newbie")         # надетый тег
    bio = Column(String, default="")
    achievements = Column(Text, default="[]")      # JSON-список id полученных достижений
    streak_days = Column(Integer, default=0)       # текущая серия учебных дней
    streak_best = Column(Integer, default=0)
    xp_total = Column(Integer, default=0)
    attendance_pct = Column(Integer, nullable=True)  # показывается, только если share_attendance
    share_attendance = Column(Boolean, default=False)
    status_mode = Column(String, default="auto")   # auto / online / offline / sleep
    last_seen = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow)


class WeeklyXP(Base):
    """Очки пользователя за календарную неделю (ключ вида 2026-W42) — из них строится лига."""
    __tablename__ = "weekly_xp"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    week_key = Column(String, primary_key=True)
    xp = Column(Integer, default=0)


class Follow(Base):
    """Подписка как в Duolingo: без заявок. Взаимная подписка = друзья."""
    __tablename__ = "follows"

    follower_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    followee_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
