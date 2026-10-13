"""
Социальная часть трекера: очки (XP), серия дней, лига недели, друзья, публичный профиль, общий онлайн.

Очки и серию считает приложение по своим данным и присылает итог (POST /api/social/sync).
Сервер проверяет разумные пределы, хранит и сравнивает пользователей между собой.
"""
import json
import re
import zlib
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import models
from database import get_db

router = APIRouter(prefix="/api")


MSK = timezone(timedelta(hours=3))
ONLINE_WINDOW = timedelta(minutes=3)      # «в сети», если приложение отзывалось за последние 3 минуты
LEAGUE_SIZE = 20
MAX_WEEK_XP = 3000                        # больше за неделю честно не набрать
MAX_TOTAL_XP = 1_000_000
ALLOWED_STATUS = {"auto", "online", "offline", "sleep"}
ID_RE = re.compile(r"^[a-z0-9_\-]{1,32}$")


def week_key(dt: Optional[datetime] = None) -> str:
    """Календарная неделя по Москве: 2026-W42. Лига обнуляется в понедельник в 00:00 МСК."""
    d = (dt or datetime.now(MSK)).astimezone(MSK)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def prev_week_key() -> str:
    return week_key(datetime.now(MSK) - timedelta(days=7))


def _now():
    return datetime.utcnow()


def _profile(db: Session, user_id: int) -> models.PublicProfile:
    p = db.get(models.PublicProfile, user_id)
    if not p:
        p = models.PublicProfile(user_id=user_id, achievements="[]")
        db.add(p)
        db.flush()
    return p


def _is_online(p: Optional[models.PublicProfile]) -> bool:
    if not p or not p.last_seen or p.status_mode == "offline":
        return False
    if p.status_mode == "online":
        return _now() - p.last_seen < timedelta(hours=12)
    return _now() - p.last_seen < ONLINE_WINDOW


def _status(p: Optional[models.PublicProfile]) -> str:
    if not p or p.status_mode == "offline":
        return "offline"
    if p.status_mode == "sleep":
        return "sleep"
    return "online" if _is_online(p) else "offline"


def _card(db: Session, user: models.User, p: Optional[models.PublicProfile], week_xp: int = 0, full: bool = False) -> dict:
    """Карточка пользователя для лиги, друзей и публичного профиля."""
    card = {
        "id": user.id,
        "name": user.username,
        "avatar": (p.avatar if p else "") or "",
        "tag": (p.tag if p else "newbie") or "",
        "streak": p.streak_days if p else 0,
        "week_xp": week_xp,
        "status": _status(p),
    }
    if full:
        try:
            ach = json.loads(p.achievements) if p and p.achievements else []
        except ValueError:
            ach = []
        card.update({
            "bio": (p.bio if p else "") or "",
            "achievements": ach,
            "streak_best": p.streak_best if p else 0,
            "xp_total": p.xp_total if p else 0,
            "university": user.university,
            "since": user.created_at.date().isoformat() if user.created_at else None,
            "attendance_pct": p.attendance_pct if (p and p.share_attendance) else None,
        })
    return card


# ---------- Схемы ----------

class SyncRequest(BaseModel):
    week_key: str
    week_xp: int
    xp_total: int
    streak_days: int
    streak_best: int
    achievements: List[str] = []
    tag: str = ""
    avatar: str = ""
    bio: str = ""
    attendance_pct: Optional[int] = None
    share_attendance: bool = False
    status_mode: str = "auto"


class FollowRequest(BaseModel):
    username: str


def setup(get_current_user_id):
    """Подключение к авторизации из main.py (чтобы не было циклического импорта)."""

    @router.post("/social/sync")
    def social_sync(data: SyncRequest, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        if data.week_key != week_key():
            # Часы устройства сильно врут или запрос из прошлой недели: принимаем профиль, очки недели — нет
            week_ok = False
        else:
            week_ok = True
        p = _profile(db, user_id)
        p.xp_total = max(0, min(int(data.xp_total), MAX_TOTAL_XP))
        p.streak_days = max(0, min(int(data.streak_days), 3650))
        p.streak_best = max(p.streak_days, min(int(data.streak_best), 3650))
        p.achievements = json.dumps([a for a in data.achievements if ID_RE.match(a)][:100])
        p.tag = data.tag if ID_RE.match(data.tag or "") else ""
        p.avatar = data.avatar if ID_RE.match(data.avatar or "") else ""
        p.bio = (data.bio or "")[:80]
        p.share_attendance = bool(data.share_attendance)
        p.attendance_pct = max(0, min(int(data.attendance_pct), 100)) if data.attendance_pct is not None else None
        p.status_mode = data.status_mode if data.status_mode in ALLOWED_STATUS else "auto"
        p.last_seen = _now()
        p.updated_at = _now()
        if week_ok:
            row = db.get(models.WeeklyXP, (user_id, data.week_key))
            if not row:
                row = models.WeeklyXP(user_id=user_id, week_key=data.week_key, xp=0)
                db.add(row)
            row.xp = max(0, min(int(data.week_xp), MAX_WEEK_XP))
        db.commit()
        return {"status": "ok", "week_key": week_key()}

    @router.post("/social/ping")
    def social_ping(status_mode: Optional[str] = None, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        """Сигнал «я в приложении» раз в минуту. Возвращает общий онлайн."""
        p = _profile(db, user_id)
        p.last_seen = _now()
        if status_mode in ALLOWED_STATUS:
            p.status_mode = status_mode
        db.commit()
        return {"online": _online_count(db)}

    @router.get("/social/online")
    def social_online(db: Session = Depends(get_db)):
        return {"online": _online_count(db)}

    @router.get("/league")
    def league(which: str = "current", user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        """Лига недели. Пока людей мало — одна лига на всех; потом делим на группы по LEAGUE_SIZE."""
        wk = week_key() if which != "previous" else prev_week_key()
        rows = db.query(models.WeeklyXP).filter(models.WeeklyXP.week_key == wk, models.WeeklyXP.xp > 0).all()
        active = {r.user_id: r.xp for r in rows}
        if which != "previous":
            active.setdefault(user_id, active.get(user_id, 0))   # сам пользователь виден в лиге всегда
        ids = sorted(active)
        if len(ids) > LEAGUE_SIZE:
            # Стабильное деление на группы на всю неделю: по хэшу (неделя + id), чтобы соседи не менялись
            groups = max(1, round(len(ids) / LEAGUE_SIZE))
            gid = lambda uid: zlib.crc32(f"{wk}:{uid}".encode()) % groups
            mine = gid(user_id)
            ids = [i for i in ids if gid(i) == mine]
        users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(ids)).all()}
        profs = {p.user_id: p for p in db.query(models.PublicProfile).filter(models.PublicProfile.user_id.in_(ids)).all()}
        members = [_card(db, users[i], profs.get(i), active.get(i, 0)) for i in ids if i in users]
        members.sort(key=lambda c: (-c["week_xp"], c["name"].lower()))
        for n, c in enumerate(members, 1):
            c["rank"] = n
        me = next((c for c in members if c["id"] == user_id), None)
        now = datetime.now(MSK)
        ends = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "week_key": wk,
            "ends_at": ends.isoformat() if which != "previous" else None,
            "members": members,
            "my_rank": me["rank"] if me else None,
            "size": len(members),
        }

    @router.get("/friends")
    def friends(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        following = [f.followee_id for f in db.query(models.Follow).filter(models.Follow.follower_id == user_id).all()]
        followers = {f.follower_id for f in db.query(models.Follow).filter(models.Follow.followee_id == user_id).all()}
        ids = list(set(following) | followers)
        users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(ids)).all()} if ids else {}
        profs = {p.user_id: p for p in db.query(models.PublicProfile).filter(models.PublicProfile.user_id.in_(ids)).all()} if ids else {}
        wk = week_key()
        xp = {r.user_id: r.xp for r in db.query(models.WeeklyXP).filter(models.WeeklyXP.week_key == wk, models.WeeklyXP.user_id.in_(ids)).all()} if ids else {}
        out = []
        for i in ids:
            if i not in users:
                continue
            c = _card(db, users[i], profs.get(i), xp.get(i, 0))
            c["you_follow"] = i in following
            c["follows_you"] = i in followers
            c["mutual"] = c["you_follow"] and c["follows_you"]
            out.append(c)
        out.sort(key=lambda c: (not c["mutual"], not c["you_follow"], -c["week_xp"], c["name"].lower()))
        return {"friends": out, "mutual_count": sum(1 for c in out if c["mutual"])}

    @router.post("/friends/follow")
    def follow(data: FollowRequest, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        name = (data.username or "").strip()
        target = db.query(models.User).filter(models.User.username == name).first()
        if not target:
            raise HTTPException(status_code=404, detail="Пользователь с таким ником не найден")
        if target.id == user_id:
            raise HTTPException(status_code=400, detail="Это ты сам")
        if not db.get(models.Follow, (user_id, target.id)):
            if db.query(models.Follow).filter(models.Follow.follower_id == user_id).count() >= 200:
                raise HTTPException(status_code=400, detail="Слишком много подписок")
            db.add(models.Follow(follower_id=user_id, followee_id=target.id))
            db.commit()
        return {"status": "ok", "id": target.id}

    @router.delete("/friends/{target_id}")
    def unfollow(target_id: int, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        f = db.get(models.Follow, (user_id, target_id))
        if f:
            db.delete(f)
            db.commit()
        return {"status": "ok"}

    @router.get("/profile/{username}")
    def public_profile(username: str, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        user = db.query(models.User).filter(models.User.username == username).first()
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        p = db.get(models.PublicProfile, user.id)
        row = db.get(models.WeeklyXP, (user.id, week_key()))
        card = _card(db, user, p, row.xp if row else 0, full=True)
        card["you_follow"] = bool(db.get(models.Follow, (user_id, user.id)))
        card["follows_you"] = bool(db.get(models.Follow, (user.id, user_id)))
        return card

    return router


def _online_count(db: Session) -> int:
    since = _now() - ONLINE_WINDOW
    q = db.query(models.PublicProfile).filter(models.PublicProfile.last_seen >= since,
                                              models.PublicProfile.status_mode != "offline")
    return q.count()
