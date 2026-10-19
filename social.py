"""
Социальная часть трекера: очки (XP), серия дней, лига недели, друзья, публичный профиль, общий онлайн.

Очки, серию и важные достижения считает сервер по событиям-отметкам (см. progress.py):
приложение присылает «отметил пару / выполнил пункт ДЗ», а не готовые цифры.
Через /api/social/sync приложение обновляет только внешний вид профиля (аватар, тег, «о себе», статус).
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
import progress
from database import get_db

router = APIRouter(prefix="/api")


MSK = timezone(timedelta(hours=3))
ONLINE_WINDOW = timedelta(minutes=3)      # «в сети», если приложение отзывалось за последние 3 минуты
LEAGUE_SIZE = 20
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


def _card(db: Session, user: models.User, p: Optional[models.PublicProfile], week_xp: int = 0, full: bool = False,
          lg: Optional[dict] = None) -> dict:
    """Карточка пользователя для лиги, друзей и публичного профиля. lg — результат недели из progress.league_scores."""
    lg = lg or {}
    card = {
        "id": user.id,
        "name": user.username,
        "avatar": (p.avatar if p else "") or "",
        "tag": (p.tag if p else "newbie") or "",
        "streak": p.streak_days if p else 0,
        "week_xp": week_xp,                                  # очки опыта за неделю (старые версии приложения)
        "score": lg.get("score", 0),                         # лига: процент от возможного
        "pairs_done": lg.get("pairs_done", 0),
        "pairs_total": lg.get("pairs_total", 0),
        "ranked": bool(lg.get("ranked")),                     # от 6 пар в неделю — в зачёте
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
    # Очки и серию сервер считает сам; эти поля старые версии приложения ещё присылают — они игнорируются
    week_key: Optional[str] = None
    week_xp: Optional[int] = None
    xp_total: Optional[int] = None
    streak_days: Optional[int] = None
    streak_best: Optional[int] = None
    achievements: List[str] = []
    tag: str = ""
    avatar: str = ""
    bio: str = ""
    attendance_pct: Optional[int] = None
    share_attendance: bool = False
    status_mode: str = "auto"


class FollowRequest(BaseModel):
    username: str


class EventsRequest(BaseModel):
    events: List[dict] = []


def _league_members(db: Session, wk: str, user_id: int, include_me: bool):
    """Участники лиги недели wk. Место — по проценту от возможного (см. progress.league_scores);
    при равном проценте выше тот, кто набрал его раньше. Недели меньше чем с 6 парами — без места, ниже черты."""
    scores = {uid: v for uid, v in progress.league_scores(db, wk).items() if v["possible"] > 0}
    if include_me:
        scores.setdefault(user_id, {"score": 0, "earned": 0, "possible": 0, "pairs_done": 0, "pairs_total": 0,
                                    "ranked": False, "last_at": None})
    ids = sorted(scores)
    if len(ids) > LEAGUE_SIZE:
        # Стабильное деление на группы на всю неделю: по хэшу (неделя + id), чтобы соседи не менялись
        groups = max(1, round(len(ids) / LEAGUE_SIZE))
        gid = lambda uid: zlib.crc32(f"{wk}:{uid}".encode()) % groups
        mine = gid(user_id)
        ids = [i for i in ids if gid(i) == mine]
    users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(ids)).all()} if ids else {}
    profs = {p.user_id: p for p in db.query(models.PublicProfile).filter(models.PublicProfile.user_id.in_(ids)).all()} if ids else {}
    xp = progress.xp_by_week(db, wk, ids) if ids else {}
    far = datetime.max
    ranked = sorted((i for i in ids if i in users and scores[i]["ranked"]),
                    key=lambda i: (-scores[i]["score"], scores[i]["last_at"] or far))
    rest = sorted((i for i in ids if i in users and not scores[i]["ranked"]),
                  key=lambda i: (-scores[i]["score"], users[i].username.lower()))
    members, prev, rank = [], None, 0
    for n, i in enumerate(ranked, 1):
        key = (scores[i]["score"], scores[i]["last_at"])
        rank = rank if key == prev else n          # полное совпадение — одно место на двоих
        prev = key
        c = _card(db, users[i], profs.get(i), xp.get(i, 0), lg=scores[i])
        c["rank"] = rank
        members.append(c)
    for i in rest:
        c = _card(db, users[i], profs.get(i), xp.get(i, 0), lg=scores[i])
        c["rank"] = None
        members.append(c)
    return members


def _state(db: Session, user_id: int, client_ach: Optional[List[str]] = None) -> dict:
    """Прогресс по версии сервера + обновление публичного профиля этими цифрами."""
    prev = _league_members(db, prev_week_key(), user_id, include_me=False)
    me = next((c for c in prev if c["id"] == user_id), None)
    podium = bool(me and me["ranked"] and me["rank"] and me["rank"] <= 3 and me["score"] > 0
                  and sum(1 for c in prev if c["ranked"]) >= 3)
    following = {f.followee_id for f in db.query(models.Follow).filter(models.Follow.follower_id == user_id).all()}
    friend = bool(following) and db.query(models.Follow).filter(
        models.Follow.followee_id == user_id, models.Follow.follower_id.in_(following)).first() is not None
    p = _profile(db, user_id)
    st = progress.compute_state(db, user_id, podium=podium, friend=friend, best_floor=p.streak_best or 0)
    if client_ach is None:
        try:
            client_ach = json.loads(p.achievements or "[]")
        except ValueError:
            client_ach = []
    allowed = progress.allowed_achievements(st["achievements"], client_ach)
    p.achievements = json.dumps(sorted(allowed))
    p.xp_total, p.streak_days = st["xp_total"], st["streak"]
    p.streak_best = max(p.streak_best or 0, st["best_streak"])
    if p.tag and not progress.tag_allowed(p.tag, allowed):
        p.tag = "newbie"
    if p.avatar and not progress.avatar_allowed(p.avatar, allowed):
        p.avatar = ""
    db.commit()
    st["public_achievements"] = sorted(allowed)
    return st


def setup(get_current_user_id):
    """Подключение к авторизации из main.py (чтобы не было циклического импорта)."""

    @router.post("/social/sync")
    def social_sync(data: SyncRequest, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        p = _profile(db, user_id)
        p.tag = data.tag if ID_RE.match(data.tag or "") else ""
        p.avatar = data.avatar if ID_RE.match(data.avatar or "") else ""
        p.bio = (data.bio or "")[:80]
        p.share_attendance = bool(data.share_attendance)
        p.attendance_pct = max(0, min(int(data.attendance_pct), 100)) if data.attendance_pct is not None else None
        p.status_mode = data.status_mode if data.status_mode in ALLOWED_STATUS else "auto"
        p.last_seen = _now()
        p.updated_at = _now()
        progress.touch_flags(db, user_id)
        db.commit()
        # Тег и аватар проверяются по достижениям, подтверждённым сервером
        st = _state(db, user_id, [a for a in data.achievements if a in progress.CLIENT_ACH])
        return {"status": "ok", "week_key": week_key(), "state": st}

    @router.post("/social/events")
    def social_events(data: EventsRequest, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        """Отметки пар и ДЗ. Очки начисляет сервер по своим правилам и часам."""
        user = db.get(models.User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")
        res = progress.apply_events(db, user, data.events)
        st = _state(db, user_id)
        st["gained"] = res["gained"]
        return st

    @router.get("/social/state")
    def social_state(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        return _state(db, user_id)

    @router.post("/social/ping")
    def social_ping(status_mode: Optional[str] = None, user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        """Сигнал «я в приложении» раз в минуту. Возвращает общий онлайн."""
        p = _profile(db, user_id)
        p.last_seen = _now()
        if status_mode in ALLOWED_STATUS:
            p.status_mode = status_mode
        progress.touch_flags(db, user_id)
        db.commit()
        return {"online": _online_count(db)}

    @router.get("/social/online")
    def social_online(db: Session = Depends(get_db)):
        return {"online": _online_count(db)}

    @router.get("/league")
    def league(which: str = "current", user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        """Лига недели. Пока людей мало — одна лига на всех; потом делим на группы по LEAGUE_SIZE."""
        wk = week_key() if which != "previous" else prev_week_key()
        members = _league_members(db, wk, user_id, include_me=(which != "previous"))  # себя видно всегда
        me = next((c for c in members if c["id"] == user_id), None)
        now = datetime.now(MSK)
        ends = (now + timedelta(days=7 - now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "week_key": wk,
            "ends_at": ends.isoformat() if which != "previous" else None,
            "members": members,
            "my_rank": me["rank"] if me else None,
            "size": len(members),
            "rules": {"min_pairs": progress.LEAGUE_MIN_PAIRS, "week_pairs": progress.WEEKLY_PAIRS},
        }

    @router.get("/friends")
    def friends(user_id: int = Depends(get_current_user_id), db: Session = Depends(get_db)):
        following = [f.followee_id for f in db.query(models.Follow).filter(models.Follow.follower_id == user_id).all()]
        followers = {f.follower_id for f in db.query(models.Follow).filter(models.Follow.followee_id == user_id).all()}
        ids = list(set(following) | followers)
        users = {u.id: u for u in db.query(models.User).filter(models.User.id.in_(ids)).all()} if ids else {}
        profs = {p.user_id: p for p in db.query(models.PublicProfile).filter(models.PublicProfile.user_id.in_(ids)).all()} if ids else {}
        wk = week_key()
        xp = progress.xp_by_week(db, wk, ids) if ids else {}
        lg = progress.league_scores(db, wk, ids) if ids else {}
        out = []
        for i in ids:
            if i not in users:
                continue
            c = _card(db, users[i], profs.get(i), xp.get(i, 0), lg=lg.get(i))
            c["you_follow"] = i in following
            c["follows_you"] = i in followers
            c["mutual"] = c["you_follow"] and c["follows_you"]
            out.append(c)
        out.sort(key=lambda c: (not c["mutual"], not c["you_follow"], -c["score"], c["name"].lower()))
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
        wk = week_key()
        card = _card(db, user, p, progress.xp_by_week(db, wk, [user.id]).get(user.id, 0), full=True,
                     lg=progress.league_scores(db, wk, [user.id]).get(user.id))
        card["you_follow"] = bool(db.get(models.Follow, (user_id, user.id)))
        card["follows_you"] = bool(db.get(models.Follow, (user.id, user_id)))
        return card

    return router


def _online_count(db: Session) -> int:
    since = _now() - ONLINE_WINDOW
    q = db.query(models.PublicProfile).filter(models.PublicProfile.last_seen >= since,
                                              models.PublicProfile.status_mode != "offline")
    return q.count()
