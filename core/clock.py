from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
except Exception:  # pragma: no cover
    TZ = None


def _now() -> datetime:
    return datetime.now(TZ) if TZ else datetime.now().astimezone()


def _parse_hhmm(text: str) -> int:
    h, m = str(text).strip().split(":")
    h, m = int(h), int(m)
    if h > 24 or m > 59 or (h == 24 and m != 0):
        raise ValueError(f"invalid time: {text}")
    return h * 60 + m


def parse_windows(text: str) -> list[dict[str, Any]]:
    windows = []
    for part in str(text or "").split(","):
        part = part.strip()
        if not part or "-" not in part:
            continue
        start_s, end_s = part.split("-", 1)
        start = _parse_hhmm(start_s)
        raw_end = _parse_hhmm(end_s)
        end = raw_end if raw_end > start else raw_end + 1440
        windows.append({"start": start, "end": end, "label": part})
    return windows


def parse_quiet(text: str) -> tuple[int, int]:
    windows = parse_windows(text)
    if not windows:
        return 0, 0
    return windows[0]["start"], windows[0]["end"]


class Clock:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.db = plugin.db
        self.cfg = plugin.cfg
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="whale-sense-clock")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        # Give AstrBot a moment to finish loading other plugins.
        await asyncio.sleep(2)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] clock tick 失败: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    # ---- planning ------------------------------------------------------
    def paused(self) -> bool:
        return bool(self.db.get_kv("paused", False)) or not bool(self.cfg.get("enabled", True))

    def in_quiet(self, now: datetime) -> bool:
        start, end = parse_quiet(str(self.cfg.get("quiet_hours", "")))
        minutes = now.hour * 60 + now.minute
        if end <= start:
            end += 1440
            if minutes < start:
                minutes += 1440
        return start <= minutes < end

    def _used(self) -> dict[str, int]:
        return dict(self.db.get_kv("clock_used", {}) or {})

    def _bump_used(self, key: str) -> None:
        used = self._used()
        used[key] = int(used.get(key, 0)) + 1
        # keep only recent 14 days
        cutoff = (_now() - timedelta(days=14)).date().isoformat()
        used = {k: v for k, v in used.items() if k[:10] >= cutoff}
        self.db.set_kv("clock_used", used)

    def next_plan(self, now: datetime | None = None) -> dict[str, Any] | None:
        now = now or _now()
        day = now.date().isoformat()
        windows = parse_windows(str(self.cfg.get("clock_windows", "")))
        wakes = max(1, min(12, int(self.cfg.get("clock_wakes", 2) or 2)))
        used = self._used()
        last_fire = self.db.get_kv("clock_last_fire", "")
        try:
            last_dt = datetime.fromisoformat(last_fire) if last_fire else None
        except Exception:
            last_dt = None
        min_gap = timedelta(minutes=max(1, int(self.cfg.get("min_gap_min", 10) or 10)))

        for w in windows:
            key = f"{day}|{w['label']}"
            if int(used.get(key, 0)) >= wakes:
                continue
            start_min = w["start"]
            end_min = w["end"]
            if end_min <= start_min:
                continue
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_dt = day_start + timedelta(minutes=start_min)
            end_dt = day_start + timedelta(minutes=end_min)
            if end_dt <= now:
                continue
            earliest = max(now, start_dt)
            if last_dt and earliest < last_dt + min_gap:
                earliest = last_dt + min_gap
            if earliest >= end_dt - timedelta(seconds=30):
                continue
            span = (end_dt - earliest).total_seconds()
            at = earliest + timedelta(seconds=random.random() * max(1, span))
            skip = random.random() < float(self.cfg.get("skip_chance", 0.3) or 0)
            return {
                "date": day,
                "window": w["label"],
                "key": key,
                "at": at.isoformat(timespec="seconds"),
                "at_text": at.strftime("%Y-%m-%d %H:%M"),
                "skip": skip,
                "created_at": now.isoformat(timespec="seconds"),
            }
        return None

    async def tick(self) -> None:
        if self.paused():
            return
        now = _now()
        plan = self.db.get_kv("clock_plan")
        if plan:
            try:
                at = datetime.fromisoformat(plan["at"])
            except Exception:
                at = None
            if at and now < at:
                return
            await self.fire(plan)
            return
        plan = self.next_plan(now)
        if plan:
            self.db.set_kv("clock_plan", plan)
            self.plugin.logger.info(
                f"[whale-sense] 下一次醒来：{plan['at_text']}（窗口 {plan['window']}，skip={plan['skip']}）"
            )

    async def fire(self, plan: dict[str, Any]) -> None:
        now = _now().isoformat(timespec="seconds")
        self.db.set_kv("clock_last_fire", now)
        self._bump_used(str(plan.get("key", "")))
        self.db.set_kv("clock_plan", None)
        self.db.add_event(
            "clock_fire",
            skip=bool(plan.get("skip")),
            window=plan.get("window"),
            at=plan.get("at"),
        )
        self.plugin.logger.info(
            f"[whale-sense] 时钟触发（skip={plan.get('skip')}，窗口 {plan.get('window')}）"
        )
        handler = getattr(self.plugin, "on_clock_fire", None)
        if handler:
            try:
                await handler(plan)
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] on_clock_fire 失败: {exc}")

    # ---- status --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "paused": self.paused(),
            "plan": self.db.get_kv("clock_plan"),
            "last_fire": self.db.get_kv("clock_last_fire"),
            "used": self._used(),
        }
