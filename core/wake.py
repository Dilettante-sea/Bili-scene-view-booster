from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any


class WakeError(RuntimeError):
    pass


async def wake_session(
    plugin,
    session: str,
    note: str,
    *,
    images: list[str] | None = None,
    delay_seconds: int = 2,
    origin: str = "whale-sense",
) -> str:
    if not session:
        raise WakeError("目标会话为空")
    if plugin.cfg.get("dry_run"):
        plugin.logger.info(f"[whale-sense] dry_run：本应唤醒 {session}，note={note[:120]}")
        return ""
    cron = getattr(plugin.context, "cron_manager", None)
    if cron is None:
        raise WakeError("context.cron_manager 不可用")
    target = str(session)
    payload: dict[str, Any] = {
        "session": target,
        "sender_id": target.split(":")[-1],
        "note": note,
        "origin": origin,
        "images": images or [],
    }
    job = await cron.add_active_job(
        name=f"whale-sense {origin}",
        cron_expression=None,
        payload=payload,
        description=note[:120],
        run_once=True,
        run_at=datetime.now().astimezone() + timedelta(seconds=max(1, delay_seconds)),
    )
    job_id = str(getattr(job, "job_id", "") or "")
    plugin.db.add_event("wake", session=target, job_id=job_id, origin=origin)
    plugin.logger.info(f"[whale-sense] 已排唤醒 {target}（job={job_id}）")
    return job_id
