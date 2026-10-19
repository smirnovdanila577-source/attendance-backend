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


# --- ЧЕСТНЫЙ ПОДСЧЁТ: отметки и ДЗ как события, очки начисляет только сервер ---

class MarkEvent(Base):
    """Отметка пары. pair_key = «дата|время|предмет». Очки даются один раз и только за свежую отметку."""
    __tablename__ = "mark_events"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    pair_key = Column(String, primary_key=True)
    pair_date = Column(String, nullable=False)       # YYYY-MM-DD
    pair_end = Column(DateTime, nullable=False)      # конец пары, UTC
    status = Column(String, nullable=True)           # a — был, m — пропуск, None — снята
    first_seen = Column(DateTime, nullable=False)    # когда сервер впервые узнал об отметке
    counted = Column(Boolean, default=False)         # дала ли очки
    counted_on = Column(String, nullable=True)       # день начисления по Москве (для дневного лимита)


class TaskEvent(Base):
    """Пункт ДЗ. Очки, если между появлением пункта на сервере и выполнением прошло 30+ минут."""
    __tablename__ = "task_events"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    task_key = Column(String, primary_key=True)
    first_seen = Column(DateTime, nullable=False)
    done_at = Column(DateTime, nullable=True)
    counted = Column(Boolean, default=False)
    counted_on = Column(String, nullable=True)


class XpLedger(Base):
    """Каждое начисление очков отдельной строкой: из неё строятся лига и сумма очков."""
    __tablename__ = "xp_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    week_key = Column(String, index=True, nullable=False)
    xp = Column(Integer, nullable=False)
    reason = Column(String, nullable=False)          # pair / task / day
    ref = Column(String, nullable=False)             # за что именно (ключ пары, задания или дня)
    created_at = Column(DateTime, default=datetime.utcnow)


class StudyDay(Base):
    """Прошедший учебный день, в котором по расписанию были пары (сообщает приложение).
    Если сервер не дал за него бонус «день закрыт» — этот день рвёт серию."""
    __tablename__ = "study_days"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    day = Column(String, primary_key=True)           # YYYY-MM-DD
    total = Column(Integer, default=0)               # сколько пар было
    created_at = Column(DateTime, default=datetime.utcnow)


class UserFlags(Base):
    """Отметки сервера для секретных достижений: заходил ночью / рано утром (по московским часам)."""
    __tablename__ = "user_flags"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    night = Column(Boolean, default=False)
    early = Column(Boolean, default=False)
