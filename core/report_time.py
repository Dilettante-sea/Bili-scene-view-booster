from __future__ import annotations

from datetime import date, datetime, timedelta


def daily_due(
    now: datetime, hour: int, minute: int, last_iso: str
) -> tuple[date | None, bool]:
    """今天/昨天该发的那份日报：返回 (日期, 是否补发)。

    - 从今天往回找「应发时刻已过、且晚于上次已发日期」的最近一天；
    - 只回看 7 天，且只返回**一条**（不按离线天数刷屏）；
    - 从未发过（last 为空）时只认今天，不补历史。
    """
    today = now.date()
    today_target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if not str(last_iso or "").strip():
        return (today, False) if now >= today_target else (None, False)
    try:
        last = date.fromisoformat(str(last_iso)[:10])
    except ValueError:
        last = None
    for back in range(0, 8):
        day = today - timedelta(days=back)
        target = today_target - timedelta(days=back)
        if now < target:
            continue
        if last is not None and day <= last:
            return None, False
        return day, day != today
    return None, False


def weekly_due(
    now: datetime, target_weekday: int, hour: int, minute: int
) -> tuple[datetime, str, bool]:
    """本周期那期周报：返回 (本期目标时刻, 本期 week key, 是否补发)。

    「本期」= 最近一个已经到达的 target_weekday 那天；周一开机时算的是
    昨天（周日）那期，所以不会因为错过周日就整周丢掉。
    """
    days_back = (now.weekday() - target_weekday) % 7
    target_day = now.date() - timedelta(days=days_back)
    target_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(
        days=days_back
    )
    iso = target_day.isocalendar()
    week_key = f"{iso.year}-{iso.week}"
    return target_dt, week_key, target_day != now.date()
