from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from . import diary as diary_mod


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    try:
        h, m = str(value).strip().split(":")
        return int(h), int(m)
    except Exception:
        return None


def _rows(plugin, sql: str, params: tuple = ()) -> list[dict]:
    with plugin.db._lock:
        cur = plugin.db.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def daily_text(plugin, date: str | None = None) -> str:
    date = date or _today()
    quota = plugin.db.quota(date)
    cand = _rows(
        plugin,
        "SELECT screen, COUNT(*) AS n FROM candidates WHERE created_at LIKE ? GROUP BY screen",
        (f"{date}%",),
    )
    screens = {str(r["screen"] or "none"): int(r["n"]) for r in cand}
    deep = _rows(
        plugin,
        "SELECT state, COUNT(*) AS n FROM deep_tasks WHERE deep_started_at LIKE ? GROUP BY state",
        (f"{date}%",),
    )
    deep_map = {str(r["state"]): int(r["n"]) for r in deep}
    musts = _rows(
        plugin,
        "SELECT id,bvid,state,queue,one_line,memory_id FROM musts WHERE produced_at LIKE ? ORDER BY produced_at",
        (f"{date}%",),
    )
    judges = _rows(
        plugin,
        "SELECT payload_json FROM events WHERE kind='judge' AND at LIKE ?",
        (f"{date}%",),
    )
    tiers = {"must": 0, "maybe": 0, "no": 0}
    for row in judges:
        try:
            tier = json.loads(row["payload_json"]).get("tier")
        except Exception:
            tier = None
        if tier in tiers:
            tiers[tier] += 1

    lines = [
        f"[鲸·审计] {date}",
        f"候选：{sum(screens.values())} 条（no {screens.get('no', 0)} / maybe {screens.get('maybe', 0)} / deep {screens.get('deep', 0)}）",
        f"深读：done {deep_map.get('done', 0)} / failed {deep_map.get('failed', 0)} / running {deep_map.get('running', 0) + deep_map.get('judging', 0)}",
        f"终评：must {tiers['must']} / maybe {tiers['maybe']} / no {tiers['no']}",
        f"额度：deep {quota['deep_used']}/天；must 产出 {quota['must_produced']}；maybe {quota['maybe_shared']}",
        f"must 队列：{len(musts)} 条",
    ]
    for m in musts:
        lines.append(
            f"  - {m['id']} [{m['queue']}/{m['state']}] {m['bvid']} {str(m.get('one_line') or '')[:60]}"
        )
    if not musts:
        lines.append("  （今天没有产出 must）")
    lines.append(f"日记：{diary_mod.daily_path(plugin)}")
    return "\n".join(lines)[:3800]


def weekly_text(plugin) -> str:
    now = datetime.now().astimezone()
    start = (now - timedelta(days=6)).date().isoformat()
    rows = _rows(
        plugin,
        "SELECT payload_json FROM events WHERE kind='judge' AND at >= ?",
        (start,),
    )
    tiers = {"must": 0, "maybe": 0, "no": 0}
    topics: dict[str, int] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except Exception:
            continue
        tier = payload.get("tier")
        if tier in tiers:
            tiers[tier] += 1
    musts = _rows(
        plugin,
        "SELECT bvid,memory_id FROM musts WHERE produced_at >= ?",
        (f"{start}%",),
    )
    return (
        f"[鲸·周报] {start} ~ {now.date().isoformat()}\n"
        f"终评：must {tiers['must']} / maybe {tiers['maybe']} / no {tiers['no']}\n"
        f"must 产出：{len(musts)} 条\n"
        f"数据源：whale.db + whale-audit.jsonl；详情 /whale report"
    )[:3800]


async def send_report(plugin, text: str) -> bool:
    if plugin.cfg.get("dry_run"):
        plugin.logger.info(f"[whale-sense] dry_run 报告：{text[:200]}")
        return True
    try:
        return await plugin.context.send_message(
            plugin.cfg.admin_umo, MessageChain([Plain(text)])
        )
    except Exception as exc:
        plugin.logger.warning(f"[whale-sense] 发送报告失败: {exc}")
        return False


class ReportScheduler:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="whale-sense-report")

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
        await asyncio.sleep(10)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] report tick 失败: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    async def tick(self) -> None:
        if self.plugin.clock.paused():
            return
        now = datetime.now().astimezone()
        daily = _parse_hhmm(str(self.plugin.cfg.get("report_daily_at", "23:30")))
        if daily:
            target = now.replace(hour=daily[0], minute=daily[1], second=0, microsecond=0)
            today = now.date().isoformat()
            if now >= target and self.plugin.db.get_kv("last_report_daily") != today:
                text = daily_text(self.plugin)
                ok = await send_report(self.plugin, text)
                if ok:
                    try:
                        diary_mod.append_report(self.plugin, "日报", text)
                    except Exception as exc:
                        self.plugin.logger.warning(f"[whale-sense] 写日报文件失败: {exc}")
                    self.plugin.db.set_kv("last_report_daily", today)
        weekly_raw = str(self.plugin.cfg.get("report_weekly_at", "Sun 21:00"))
        try:
            weekday_s, time_s = weekly_raw.split()
            target_wd = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}[weekday_s]
            hh, mm = _parse_hhmm(time_s) or (21, 0)
        except Exception:
            target_wd, hh, mm = 6, 21, 0
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        week = f"{now.isocalendar().year}-{now.isocalendar().week}"
        if now.weekday() == target_wd and now >= target and self.plugin.db.get_kv("last_report_weekly") != week:
            text = weekly_text(self.plugin)
            ok = await send_report(self.plugin, text)
            if ok:
                try:
                    end_day = now.date()
                    diary_mod.append_report(
                        self.plugin,
                        "周报",
                        text,
                        week=(end_day - timedelta(days=6), end_day),
                    )
                except Exception as exc:
                    self.plugin.logger.warning(f"[whale-sense] 写周报文件失败: {exc}")
                self.plugin.db.set_kv("last_report_weekly", week)
