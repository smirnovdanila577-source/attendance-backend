"""
Честный подсчёт прогресса: очки, серия и достижения считает сервер по событиям из приложения.

Правила (согласованы с автором трекера):
  * отметка пары даёт очки один раз, если сделана не позже чем через 24 часа после конца пары,
    пара закончилась после регистрации и не в будущем;
  * не больше 8 засчитанных пар и 8 пунктов ДЗ в день (по Москве) — больше честного отличника не набрать;
  * пункт ДЗ засчитывается, если от его появления на сервере до выполнения прошло 30+ минут;
  * отметки задним числом можно ставить для личной статистики, но очков и достижений они не дают.
Время всегда берётся по часам сервера, часы телефона не важны.
"""
import json
import re
from datetime import datetime, timedelta, timezone, date
from typing import List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session

import models

MSK = timezone(timedelta(hours=3))

XP_PAIR, XP_TASK, XP_FULL_DAY = 10, 5, 5
MARK_WINDOW = timedelta(hours=24)
FUTURE_SLACK = timedelta(minutes=5)          # расхождение часов телефона и сервера
TASK_MIN_AGE = timedelta(minutes=30)
DAILY_PAIRS, DAILY_TASKS = 8, 8
STREAK_MAX_GAP = 3                           # серия рвётся, если 3 дня подряд нет ни одной засчитанной отметки

# Достижения, которые выдаёт сервер (от них зависят аватары и теги)
SERVER_ACH = {"attend_10", "attend_100", "streak_10", "hw_10", "streak_3", "streak_7", "streak_30", "xp_1000",
              "scholar", "perfect_week", "night_owl", "early_bird", "league_podium", "first_friend"}
# Безобидные достижения, которые приложение отмечает само
CLIENT_ACH = {"first_mark", "important", "profile_done"}
# Что открывает теги и аватары (повторяет profile.js и avatars.js)
TAG_ACH = {"newbie": None, "diligent": "attend_10", "ironclad": "attend_100", "flow": "streak_10",
           "perfect": "perfect_week", "scholar": "scholar", "hwmaster": "hw_10", "strategist": "important",
           "owl": "night_owl", "lark": "early_bird", "person": "profile_done", "ogonek": "streak_7",
           "champion": "league_podium"}
AVATAR_ACH = {"owl": "night_owl", "rooster": "early_bird", "rhino": "attend_100", "fox": "streak_30",
              "raccoon": "hw_10", "raven": "scholar", "lion": "league_podium", "capy": "first_friend",
              "cat": "important"}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_utc() -> datetime:
    return datetime.utcnow()


def msk_today(now: Optional[datetime] = None) -> str:
    n = (now or now_utc()).replace(tzinfo=timezone.utc).astimezone(MSK)
    return n.date().isoformat()


def week_key_of(now: Optional[datetime] = None) -> str:
    n = (now or now_utc()).replace(tzinfo=timezone.utc).astimezone(MSK)
    y, w, _ = n.isocalendar()
    return f"{y}-W{w:02d}"


def _parse_end(value: str) -> Optional[datetime]:
    """ISO-время конца пары от приложения → UTC без часового пояса."""
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=MSK)
    return d.astimezone(timezone.utc).replace(tzinfo=None)


def touch_flags(db: Session, user_id: int, now: Optional[datetime] = None):
    """Ночной и ранний заход для секретных достижений — по московскому времени сервера."""
    h = (now or now_utc()).replace(tzinfo=timezone.utc).astimezone(MSK).hour
    if h >= 8:
        return
    f = db.get(models.UserFlags, user_id)
    if not f:
        f = models.UserFlags(user_id=user_id, night=False, early=False)
        db.add(f)
    if h < 5:
        f.night = True
    else:
        f.early = True


def _award(db: Session, user_id: int, xp: int, reason: str, ref: str, now: datetime):
    db.add(models.XpLedger(user_id=user_id, week_key=week_key_of(now), xp=xp, reason=reason, ref=ref, created_at=now))


def apply_events(db: Session, user: models.User, events: List[dict]) -> dict:
    """Принимает пачку событий из приложения. Возвращает, сколько очков начислено."""
    now = now_utc()
    today = msk_today(now)
    gained = 0
    pairs_today = db.query(func.count()).select_from(models.MarkEvent).filter(
        models.MarkEvent.user_id == user.id, models.MarkEvent.counted_on == today).scalar() or 0
    tasks_today = db.query(func.count()).select_from(models.TaskEvent).filter(
        models.TaskEvent.user_id == user.id, models.TaskEvent.counted_on == today).scalar() or 0
    created = user.created_at or now

    for ev in events[:200]:
        kind = ev.get("type")
        if kind == "mark":
            key = str(ev.get("key") or "")[:200]
            pdate = str(ev.get("date") or "")
            end = _parse_end(ev.get("end"))
            st = ev.get("status")
            status = "a" if st is True else "m" if st is False else None
            if not key or not DATE_RE.match(pdate) or not end:
                continue
            m = db.get(models.MarkEvent, (user.id, key))
            if not m:
                m = models.MarkEvent(user_id=user.id, pair_key=key, pair_date=pdate, pair_end=end,
                                     status=status, first_seen=now, counted=False)
                db.add(m)
                db.flush()      # чтобы тот же ключ дальше в этой пачке нашёлся, а не вставился второй раз
            else:
                m.status = status
            if m.counted or status is None:
                continue
            # Свежесть меряем по моменту, когда сервер ВПЕРВЫЕ узнал об отметке этой пары:
            # отметка задним числом (через сутки и позже) очков не даст и при повторных попытках
            if end > m.first_seen + FUTURE_SLACK or m.first_seen - end > MARK_WINDOW:
                continue
            if end < created or pairs_today >= DAILY_PAIRS:
                continue
            m.counted, m.counted_on = True, today
            pairs_today += 1
            _award(db, user.id, XP_PAIR, "pair", key, now); gained += XP_PAIR
            # Бонус за полностью отмеченный день (сколько пар в дне, сообщает приложение, не больше 8)
            total = max(1, min(int(ev.get("day_total") or 0), DAILY_PAIRS))
            db.flush()
            done = db.query(func.count()).select_from(models.MarkEvent).filter(
                models.MarkEvent.user_id == user.id, models.MarkEvent.pair_date == pdate,
                models.MarkEvent.counted == True).scalar() or 0  # noqa: E712
            ref = f"day:{pdate}"
            if done >= total and not db.query(models.XpLedger).filter(
                    models.XpLedger.user_id == user.id, models.XpLedger.ref == ref).first():
                _award(db, user.id, XP_FULL_DAY, "day", ref, now); gained += XP_FULL_DAY

        elif kind in ("task_add", "task_done"):
            key = str(ev.get("key") or "")[:80]
            if not key:
                continue
            t = db.get(models.TaskEvent, (user.id, key))
            if not t:
                t = models.TaskEvent(user_id=user.id, task_key=key, first_seen=now, counted=False)
                db.add(t)
                db.flush()
            if kind == "task_done" and not t.counted:
                t.done_at = now
                if now - t.first_seen >= TASK_MIN_AGE and tasks_today < DAILY_TASKS:
                    t.counted, t.counted_on = True, today
                    tasks_today += 1
                    _award(db, user.id, XP_TASK, "task", key, now); gained += XP_TASK

    touch_flags(db, user.id, now)
    db.commit()
    return {"gained": gained}


# ---------- Итоги ----------

def _streaks(dates: Set[str], today: str):
    """Серия — дни с засчитанными отметками; рвётся, если между ними 3+ пустых дня."""
    if not dates:
        return 0, 0
    ds = sorted(date.fromisoformat(d) for d in dates)
    best = run = 1
    for a, b in zip(ds, ds[1:]):
        run = run + 1 if (b - a).days <= STREAK_MAX_GAP else 1
        best = max(best, run)
    current = run if (date.fromisoformat(today) - ds[-1]).days <= STREAK_MAX_GAP else 0
    return current, best


def xp_by_week(db: Session, week: str, user_ids: Optional[List[int]] = None) -> dict:
    q = db.query(models.XpLedger.user_id, func.sum(models.XpLedger.xp)).filter(models.XpLedger.week_key == week)
    if user_ids is not None:
        q = q.filter(models.XpLedger.user_id.in_(user_ids))
    return {uid: int(x or 0) for uid, x in q.group_by(models.XpLedger.user_id).all()}


def compute_state(db: Session, user_id: int, podium: bool = False, friend: bool = False) -> dict:
    """Всё, что сервер знает о прогрессе пользователя. podium/friend считает social.py."""
    now = now_utc()
    today = msk_today(now)
    marks = db.query(models.MarkEvent).filter(models.MarkEvent.user_id == user_id, models.MarkEvent.counted == True).all()  # noqa: E712
    attended = sum(1 for m in marks if m.status == "a")
    missed = sum(1 for m in marks if m.status == "m")
    tasks = db.query(func.count()).select_from(models.TaskEvent).filter(
        models.TaskEvent.user_id == user_id, models.TaskEvent.counted == True).scalar() or 0  # noqa: E712
    xp_total = int(db.query(func.sum(models.XpLedger.xp)).filter(models.XpLedger.user_id == user_id).scalar() or 0)
    wk = week_key_of(now)
    week_xp = xp_by_week(db, wk, [user_id]).get(user_id, 0)
    streak, best = _streaks({m.pair_date for m in marks if m.status in ("a", "m")}, today)

    weeks = {}
    for m in marks:
        y, w, _ = date.fromisoformat(m.pair_date).isocalendar()
        st = weeks.setdefault((y, w), [0, 0])
        if m.status == "a":
            st[0] += 1
        elif m.status == "m":
            st[1] += 1
    perfect_weeks = sum(1 for a, b in weeks.values() if a >= 3 and b == 0)
    # Посещённых пар подряд без пропуска (по времени пар)
    run = best_run = 0
    for m in sorted(marks, key=lambda m: m.pair_end):
        if m.status == "a":
            run += 1; best_run = max(best_run, run)
        elif m.status == "m":
            run = 0
    total = attended + missed
    pct = round(attended / total * 100) if total else 0
    flags = db.get(models.UserFlags, user_id)

    earned = set()
    if attended >= 10: earned.add("attend_10")
    if attended >= 100: earned.add("attend_100")
    if best_run >= 10: earned.add("streak_10")
    if tasks >= 10: earned.add("hw_10")
    if best >= 3: earned.add("streak_3")
    if best >= 7: earned.add("streak_7")
    if best >= 30: earned.add("streak_30")
    if xp_total >= 1000: earned.add("xp_1000")
    if total >= 30 and pct >= 80: earned.add("scholar")
    if perfect_weeks >= 1: earned.add("perfect_week")
    if flags and flags.night: earned.add("night_owl")
    if flags and flags.early: earned.add("early_bird")
    if podium: earned.add("league_podium")
    if friend: earned.add("first_friend")

    return {
        "week_key": wk, "week_xp": week_xp, "xp_total": xp_total,
        "streak": streak, "best_streak": best,
        "counters": {"attended": attended, "missed": missed, "marks": total, "tasks": tasks, "pair_run": best_run,
                     "perfect_weeks": perfect_weeks, "pct": pct},
        "achievements": sorted(earned),
        "rules": {"pair": XP_PAIR, "task": XP_TASK, "day": XP_FULL_DAY, "window_hours": 24,
                  "daily_pairs": DAILY_PAIRS, "daily_tasks": DAILY_TASKS, "task_minutes": 30},
    }


def allowed_achievements(server_earned: List[str], client_list: List[str]) -> Set[str]:
    return set(server_earned) | {a for a in client_list if a in CLIENT_ACH}


def tag_allowed(tag: str, achievements: Set[str]) -> bool:
    if tag not in TAG_ACH:
        return False
    need = TAG_ACH[tag]
    return need is None or need in achievements


def avatar_allowed(avatar: str, achievements: Set[str]) -> bool:
    need = AVATAR_ACH.get(avatar)
    return bool(need) and need in achievements
