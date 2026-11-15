from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any


def new_id() -> str:
    now = datetime.now().astimezone()
    return f"b-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def cap(plugin) -> int:
    try:
        return max(1, int(plugin.cfg.get("backlog_max", 10) or 10))
    except (TypeError, ValueError):
        return 10


def size(plugin) -> int:
    return int(plugin.db.count_backlog("queued"))


def enqueue(
    plugin,
    *,
    bvid: str,
    title: str,
    source: str,
    one_line: str,
    why: str,
) -> dict[str, Any]:
    """进「攒着」队列：上限 cap 条，FIFO 挤掉最旧的（留档不删）。"""
    source = "maybe-light" if str(source) == "maybe-light" else "deep"
    bid = new_id()
    limit = cap(plugin)
    plugin.db.add_backlog(
        bid,
        bvid,
        title or bvid,
        source,
        (one_line or "").strip(),
        (why or "").strip(),
        limit=limit,
    )
    plugin.db.add_event("backlog_added", bvid=bvid)
    return {
        "id": bid,
        "bvid": bvid,
        "source": source,
        "size": size(plugin),
        "cap": limit,
    }


def listing(plugin, limit: int | None = None) -> list[dict]:
    rows = plugin.db.list_backlog(limit or cap(plugin))
    for index, row in enumerate(rows, 1):
        row["n"] = index
    return rows


def promote(plugin, bid: str) -> dict[str, Any]:
    """把攒着的一条升级成 must（走 must 额度）。"""
    from . import must as must_mod

    item = plugin.db.get_backlog(bid)
    if not item or item.get("state") != "queued":
        return {"ok": False, "error": f"backlog 里没有 {bid}（可能已升级或已挤掉）"}
    source = "light" if item.get("source") == "maybe-light" else "deep"
    if must_mod.remaining_today(plugin, source) <= 0:
        caps = must_mod.caps(plugin)
        return {
            "ok": False,
            "error": (
                f"今日 {source} must 额度已用完（上限 {caps.get(source)} 条）；"
                "可以明天再升，或直接把链接给他看"
            ),
        }
    plugin.db.close_backlog(item["id"], "promoted")
    must = must_mod.enqueue(
        plugin,
        bvid=item["bvid"],
        memory_id=0,
        one_line=item.get("one_line") or "",
        why=item.get("why") or "",
        push_text=f"{item.get('one_line') or ''}\nhttps://www.bilibili.com/video/{item['bvid']}",
        queue="primary",
        source=source,
    )
    plugin.db.add_event("backlog_promoted", bvid=item["bvid"], must_id=must["id"])
    return {"ok": True, "must_id": must["id"], "bvid": item["bvid"], "source": source}


def drop(plugin, bid: str) -> dict[str, Any]:
    item = plugin.db.get_backlog(bid)
    if not item or item.get("state") != "queued":
        return {"ok": False, "error": f"backlog 里没有 {bid}"}
    plugin.db.close_backlog(item["id"], "dropped")
    plugin.db.add_event("backlog_dropped", bvid=item["bvid"])
    return {"ok": True, "id": item["id"], "bvid": item["bvid"]}


def summary(plugin) -> str:
    return f"backlog {size(plugin)}/{cap(plugin)}"
