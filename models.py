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