from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any


def new_id() -> str:
    now = datetime.now().astimezone()
    return f"m-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def production_quota(plugin) -> int:
    primary = plugin.db.count(
        "musts", "queue='primary' AND state IN ('pending','pushing','pushed')"
    )
    if primary > 0:
        return max(1, int(plugin.cfg.get("must_backlog_per_day", 1) or 1))
    return max(1, int(plugin.cfg.get("must_per_day", 2) or 2))


def enqueue(plugin, *, bvid: str, memory_id: int, one_line: str, why: str, push_text: str = "") -> dict[str, Any]:
    must_id = new_id()
    plugin.db.add_must(must_id, bvid, int(memory_id or 0), one_line, why, push_text)
    plugin.db.bump_quota(today(), "must_produced")
    plugin.db.add_event("must_produced", bvid=bvid, must_id=must_id, memory_id=memory_id)
    return {"id": must_id, "bvid": bvid, "memory_id": int(memory_id or 0)}


def classify(plugin, must_id: str, outcome: str, note: str = "") -> dict[str, Any]:
    must = plugin.db.get_must(must_id)
    if not must:
        return {"ok": False, "error": f"找不到 must {must_id}"}
    outcome = str(outcome or "").strip()
    if outcome not in {"archived_done", "discarded_not_interested", "later_shallow"}:
        return {"ok": False, "error": "outcome 必须是 archived_done / discarded_not_interested / later_shallow"}
    if must["state"] not in {"pushing", "pushed", "admin_discussing", "archived_shallow"}:
        return {"ok": False, "error": f"must 当前状态 {must['state']} 不能分类"}
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    if outcome == "archived_done":
        plugin.db.update_must(
            must_id, state="archived_done", discussion_status="done", classified_at=now, note=note
        )
    elif outcome == "discarded_not_interested":
        plugin.db.update_must(
            must_id,
            state="discarded_not_interested",
            discussion_status="not_interested",
            classified_at=now,
            note=note,
        )
    else:
        attempts = int(must.get("attempts") or 0) + 1
        max_tries = max(1, int(plugin.cfg.get("max_shallow_attempts", 3) or 3))
        if attempts >= max_tries:
            plugin.db.update_must(
                must_id,
                state="archived_shallow",
                discussion_status="not_fully_discussed",
                attempts=attempts,
                classified_at=now,
                note=note,
            )
        else:
            plugin.db.update_must(
                must_id,
                state="pending",
                queue="secondary",
                attempts=attempts,
                discussion_status="not_fully_discussed",
                classified_at=now,
                note=note,
            )
    plugin.db.add_event("must_classified", must_id=must_id, outcome=outcome, note=note)
    return {"ok": True, "id": must_id, "outcome": outcome}


def next_pending(plugin) -> dict | None:
    item = plugin.db.first_must("primary")
    if item:
        return item
    return plugin.db.first_must("secondary")


def mark_pushing(plugin, must_id: str) -> None:
    plugin.db.update_must(
        must_id,
        state="pushing",
        pushed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def confirm_pushed(plugin, must_id: str) -> None:
    plugin.db.update_must(
        must_id,
        state="pushed",
        pushed_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def push_prompt(plugin, must: dict) -> str:
    return (
        "你在管理员私聊里。现在要把一条你自己想给他看的 must 用自己的话说出来。\n\n"
        f"must_id = {must.get('id')}\n"
        f"bvid = {must.get('bvid')}\n"
        f"一句话：{must.get('one_line')}\n"
        f"为什么想给他看：{must.get('why')}\n"
        f"链接：https://www.bilibili.com/video/{must.get('bvid')}\n\n"
        "要求：\n"
        "1) 用你自己的话，2–4 句，像聊天，不要写报告；\n"
        "2) 带上链接；\n"
        "3) 不要复述字幕，不要贴长段分析；\n"
        "4) 这一轮直接回复这条消息，不要调用 send_message_to_user 或任何发消息工具——你的回复会被发到管理员私聊；\n"
        "5) 这一轮只发这条消息，不要调用 whale_classify_must；等他回复、聊过之后再分类。"
    )


def pending_summary(plugin) -> str:
    rows = []
    for item in plugin.db.list_musts():
        rows.append(
            f"- {item['id']} [{item['queue']}/{item['state']}] "
            f"{'admin' if item.get('source') == 'admin' else 'auto'} {item['bvid']} {item.get('one_line') or ''}"
        )
    return "\n".join(rows) if rows else "（must 队列为空）"
