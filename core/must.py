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
    """今天 must 的总上限（展示用）：无积压 3（2 deep + 1 light），有积压 1。"""
    return sum(caps(plugin).values())


def has_backlog(plugin) -> bool:
    """主队列里还有没聊完的 must 就算积压。"""
    return (
        plugin.db.count(
            "musts", "queue='primary' AND state IN ('pending','pushing','pushed')"
        )
        > 0
    )


def caps(plugin) -> dict[str, int]:
    """按来源分的今日上限。

    无积压：deep 2 + light 1 = 3；
    有积压：deep 1 + light 0 = 1（只留最值得的那一条 deep-must）。
    """
    if has_backlog(plugin):
        return {
            "deep": max(1, int(plugin.cfg.get("must_backlog_per_day", 1) or 1)),
            "light": 0,
        }
    return {
        "deep": max(1, int(plugin.cfg.get("must_per_day", 2) or 2)),
        "light": max(0, int(plugin.cfg.get("maybe_must_per_day", 1) or 1)),
    }


def produced_today(plugin, source: str) -> int:
    """今天已经产出的 must 数；deep 把历史的 'auto' 也算进去。"""
    day = today()
    if source == "light":
        return plugin.db.count("musts", "produced_at LIKE ? AND source='light'", (f"{day}%",))
    return plugin.db.count(
        "musts", "produced_at LIKE ? AND (source='deep' OR source='auto')", (f"{day}%",)
    )


def remaining_today(plugin, source: str = "deep") -> int:
    source = "light" if str(source) == "light" else "deep"
    return max(0, caps(plugin).get(source, 0) - produced_today(plugin, source))


def enqueue(
    plugin,
    *,
    bvid: str,
    memory_id: int,
    one_line: str,
    why: str,
    push_text: str = "",
    queue: str = "primary",
    source: str = "deep",
) -> dict[str, Any]:
    must_id = new_id()
    plugin.db.add_must(
        must_id, bvid, int(memory_id or 0), one_line, why, push_text, queue=queue, source=source
    )
    plugin.db.bump_quota(today(), "must_produced")
    plugin.db.add_event("must_produced", bvid=bvid, must_id=must_id, memory_id=memory_id)
    return {"id": must_id, "bvid": bvid, "memory_id": int(memory_id or 0), "queue": queue}


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
            # 回炉重推：清掉上一次的 push_text，下一次推送只能由助手重新写，
            # 避免「三天前那段总结」原样再弹一遍（must_loop 会改走唤醒 admin 的路径）。
            plugin.db.update_must(
                must_id,
                state="pending",
                queue="secondary",
                attempts=attempts,
                discussion_status="not_fully_discussed",
                classified_at=now,
                pushed_at=None,
                push_text="",
                note=note,
            )
    plugin.db.add_event("must_classified", must_id=must_id, outcome=outcome, note=note)
    return {"ok": True, "id": must_id, "outcome": outcome}


def skip(plugin, must_id: str, reason: str = "") -> dict[str, Any]:
    """推送时发现这条其实不用再推：归档掉，并让 must_loop 立刻拉下一条。

    场景：这条实际上已经聊透了，但助手当时没标记；脚本又把它推上来。
    助手在推送回合里调 whale_skip_must，这里负责收尾。
    """
    note = f"推送时跳过：{reason or '助手判断无需再推'}"
    must = plugin.db.get_must(must_id) or {}
    if must.get("state") in {"pushing", "pushed", "admin_discussing", "archived_shallow"}:
        result = classify(plugin, must_id, "archived_done", note=note)
    elif must:
        plugin.db.update_must(
            must_id,
            state="archived_done",
            discussion_status="done",
            note=note,
            push_text="",
        )
        plugin.db.add_event("must_classified", must_id=must_id, outcome="archived_done", note=note)
        result = {"ok": True, "id": must_id, "outcome": "archived_done"}
    else:
        return {"ok": False, "error": f"找不到 must {must_id}"}
    if result.get("ok"):
        # 让下一轮 tick 立刻开工，不用等一小时 idle
        plugin.db.set_kv("force_next", True)
        plugin.db.add_event("must_skipped", must_id=must_id, payload_json=json.dumps({"reason": reason}, ensure_ascii=False))
    return result


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
    attempts = int(must.get("attempts") or 0)
    again = ""
    if attempts > 0:
        again = (
            f"\n注意：这条之前已经推过 {attempts} 次，上次没聊透。"
            "这次请换一个说法（新的角度、新的问题），不要重复上次那段话。\n"
        )
    return (
        "你在管理员私聊里。现在要把一条你自己想给他看的 must 用自己的话说出来。\n"
        f"{again}\n"
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
        "5) 这一轮只发这条消息，不要调用 whale_classify_must；等他回复、聊过之后再分类。\n"
        "6) **例外**：如果你判断这条其实已经聊透了、或者现在不值得再推（比如上一轮已经说过同一件事），"
        "不要硬发——调 whale_skip_must(must_id, reason=\"为什么跳过\")，把它归档，系统会自动换下一条上来；"
        "这时只用一句话说明你跳过了哪条，不要贴链接。"
    )


def pending_summary(plugin) -> str:
    rows = []
    for item in plugin.db.list_musts():
        rows.append(
            f"- {item['id']} [{item['queue']}/{item['state']}] "
            f"{'admin' if item.get('source') == 'admin' else 'auto'} {item['bvid']} {item.get('one_line') or ''}"
        )
    return "\n".join(rows) if rows else "（must 队列为空）"
