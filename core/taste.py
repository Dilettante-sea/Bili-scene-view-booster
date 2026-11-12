from __future__ import annotations

import json
import math
import random
import sqlite3
import statistics
import unicodedata
from datetime import datetime
from typing import Any


SIGMA = 0.5
PRIOR_MEAN = 0.2
PRIOR_PRECISION = 1.0
DECAY_PER_WEEK = 0.98
GENERIC_TAGS = {
    "生活",
    "中国大陆",
    "自制",
    "原创",
    "日常",
    "娱乐",
    "视频",
    "投稿",
    "bilibili",
}

SEARCH_QUERIES = {
    "DSH/AI 工具与插件": "AI工具",
    "鬼畜与怀旧向剪辑": "鬼畜",
    "Live2D/模型": "Live2D",
    "B站生态八卦": "B站",
    "知识分享": "知识",
}

DEFAULT_LIKE_TAGS = [
    "写作技巧",
    "文学解读",
    "深度解析",
    "知识分享",
    "历史",
    "DSH/AI 工具与插件",
]
DEFAULT_DISLIKE_TAGS = ["带货", "擦边", "标题党", "纯搬运"]

ALIASES = {
    "人工智能": "AI",
    "大模型": "AI",
    "ai": "AI",
    "猫": "猫咪",
    "喵星人": "猫咪",
}


def _now() -> datetime:
    return datetime.now().astimezone()


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _clean(tag: str) -> str:
    return unicodedata.normalize("NFKC", str(tag or "")).strip().lstrip("#").strip()


def _norm(tag: str) -> str:
    return _clean(tag).lower()


def _canonical(tag: str) -> str:
    raw = _clean(tag)
    if not raw:
        return ""
    return ALIASES.get(raw.lower(), raw)


def _search_query(tag: str) -> str:
    return SEARCH_QUERIES.get(tag, tag)


def _rows(plugin, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with plugin.db._lock:
        return list(plugin.db.conn.execute(sql, params).fetchall())


def _one(plugin, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with plugin.db._lock:
        return plugin.db.conn.execute(sql, params).fetchone()


def _exec(plugin, sql: str, params: tuple = ()) -> None:
    with plugin.db._lock:
        plugin.db.conn.execute(sql, params)
        plugin.db.conn.commit()


def ensure_seed(plugin) -> None:
    """Seed admin block/tags from config once; DB stays the live source."""
    admin_count = _one(plugin, "SELECT COUNT(*) AS n FROM admin_tags")
    if not admin_count or int(admin_count["n"]) == 0:
        likes = [_canonical(x) for x in DEFAULT_LIKE_TAGS]
        likes = [x for x in likes if x]
        now = _now_iso()
        for tag in likes:
            _exec(
                plugin,
                "INSERT OR IGNORE INTO admin_tags(tag,search_query,base_weight,locked,pinned,status,updated_at) "
                "VALUES(?,?,?,0,0,'active',?)",
                (tag, _search_query(tag), 1.0, now),
            )
    block_count = _one(plugin, "SELECT COUNT(*) AS n FROM block_tags")
    if not block_count or int(block_count["n"]) == 0:
        for tag in DEFAULT_DISLIKE_TAGS:
            tag = _canonical(tag)
            if not tag:
                continue
            _exec(
                plugin,
                "INSERT OR IGNORE INTO block_tags(tag,reason,created_by,created_at) VALUES(?,?,?,?)",
                (tag, "seed from config", "seed", _now_iso()),
            )
    for alias, tag in ALIASES.items():
        _exec(
            plugin,
            "INSERT OR IGNORE INTO tag_aliases(alias,tag) VALUES(?,?)",
            (alias, tag),
        )


def block_list(plugin) -> list[str]:
    ensure_seed(plugin)
    rows = _rows(plugin, "SELECT tag FROM block_tags")
    return [str(r["tag"]) for r in rows]


def _decay_row(plugin, row: sqlite3.Row) -> tuple[float, float]:
    n = float(row["n_eff"] or 0)
    total = float(row["sum_reward"] or 0)
    last = str(row["last_signal_at"] or "")
    if not last:
        return n, total
    try:
        then = datetime.fromisoformat(last)
    except Exception:
        return n, total
    weeks = max(0.0, (_now() - then).total_seconds() / (7 * 24 * 3600))
    factor = DECAY_PER_WEEK ** weeks
    return n * factor, total * factor


def _posterior(row: sqlite3.Row | None) -> tuple[float, float, float, float]:
    if row is None:
        return PRIOR_MEAN, PRIOR_PRECISION, 0.0, 0.0
    n, total = _decay_row(None, row)  # decay is time-only; caller passes no plugin
    precision = PRIOR_PRECISION + n / (SIGMA * SIGMA)
    mean = (PRIOR_PRECISION * PRIOR_MEAN + total / (SIGMA * SIGMA)) / precision
    confidence = max(0.0, min(1.0, 1.0 - 1.0 / precision))
    return mean, precision, n, confidence


def _taste_rows(plugin) -> dict[str, sqlite3.Row]:
    return {str(r["tag"]): r for r in _rows(plugin, "SELECT * FROM taste_tags")}


def _admin_rows(plugin) -> list[sqlite3.Row]:
    return _rows(plugin, "SELECT * FROM admin_tags WHERE status='active' ORDER BY pinned DESC, tag ASC")


def _query_to_tag(plugin, query: str) -> str:
    q = _norm(query)
    for r in _admin_rows(plugin):
        if _norm(r["search_query"] or r["tag"]) == q:
            return str(r["tag"])
    for r in _rows(plugin, "SELECT tag FROM taste_tags WHERE status='active'"):
        if _norm(r["tag"]) == q:
            return str(r["tag"])
    return ""


def select_queries(plugin, limit: int = 5) -> list[str]:
    """Normal-Thompson over tag deltas, with one explicit exploration slot."""
    ensure_seed(plugin)
    limit = max(1, min(8, int(limit or 5)))
    admin = _admin_rows(plugin)
    if not admin:
        return [str(x) for x in DEFAULT_LIKE_TAGS if str(x).strip()][:limit]

    blocked = {_norm(x) for x in block_list(plugin)}
    tastes = _taste_rows(plugin)
    items: list[dict[str, Any]] = []
    pinned: list[dict[str, Any]] = []
    for row in admin:
        tag = str(row["tag"])
        if _norm(tag) in blocked:
            continue
        query = str(row["search_query"] or tag).strip() or tag
        trow = tastes.get(tag)
        mean, precision, n, confidence = _posterior(trow)
        manual = float(trow["manual_delta"] or 0) if trow is not None else 0.0
        if bool(row["locked"]):
            delta = 0.0
        else:
            delta = random.gauss(mean + manual, math.sqrt(1.0 / precision))
        item = {
            "tag": tag,
            "query": query,
            "base": float(row["base_weight"] or 0),
            "effective": float(row["base_weight"] or 0) + delta,
            "n": n,
            "confidence": confidence,
            "pinned": bool(row["pinned"]),
            "locked": bool(row["locked"]),
        }
        items.append(item)
        if item["pinned"]:
            pinned.append(item)

    # taste-only tags: base 0, still eligible for exploration/learning
    admin_tags = {x["tag"] for x in items}
    for tag, row in tastes.items():
        if tag in admin_tags or _norm(tag) in blocked or str(row["status"]) != "active":
            continue
        mean, precision, n, confidence = _posterior(row)
        manual = float(row["manual_delta"] or 0)
        delta = random.gauss(mean + manual, math.sqrt(1.0 / precision))
        items.append(
            {
                "tag": tag,
                "query": tag,
                "base": 0.0,
                "effective": delta,
                "n": n,
                "confidence": confidence,
                "pinned": False,
                "locked": False,
            }
        )

    selected: list[dict[str, Any]] = []
    # Pinned is an inclusion promise, but cap it at 1 to protect exploration.
    for item in pinned[:1]:
        selected.append(item)

    remaining = limit - len(selected)
    explore_slots = 1 if remaining >= 2 else 0
    exploit_slots = max(0, remaining - explore_slots)
    rest = [x for x in items if x not in selected]
    rest.sort(key=lambda x: x["effective"], reverse=True)
    selected.extend(rest[:exploit_slots])

    selected_tags = {x["tag"] for x in selected}
    explore_pool = [x for x in items if x["tag"] not in selected_tags]
    if explore_slots and explore_pool:
        # Prefer under-observed tags, then high uncertainty.
        explore_pool.sort(key=lambda x: (x["n"], -x["confidence"], -x["effective"]))
        selected.append(explore_pool[0])

    # De-duplicate queries, keep order.
    queries: list[str] = []
    for item in selected:
        q = str(item["query"]).strip()
        if q and q not in queries:
            queries.append(q)
    plugin.db.add_event(
        "tag_select",
        limit=limit,
        selected=[
            {"tag": x["tag"], "query": x["query"], "effective": round(x["effective"], 4), "n": round(x["n"], 2)}
            for x in selected
        ],
        queries=queries,
    )
    return queries


def _apply_reward(plugin, tag: str, reward: float, share: float, signal: str, bvid: str, query_tag: str) -> None:
    tag = _canonical(tag)
    if not tag or tag in GENERIC_TAGS:
        return
    row = _one(plugin, "SELECT * FROM taste_tags WHERE tag=?", (tag,))
    if row is not None:
        n, total = _decay_row(plugin, row)
    else:
        n, total = 0.0, 0.0
    n += max(0.0, share)
    total += reward * share
    precision = PRIOR_PRECISION + n / (SIGMA * SIGMA)
    mean = (PRIOR_PRECISION * PRIOR_MEAN + total / (SIGMA * SIGMA)) / precision
    confidence = max(0.0, min(1.0, 1.0 - 1.0 / precision))
    now = _now_iso()
    _exec(
        plugin,
        "INSERT INTO taste_tags(tag,weight,n_eff,sum_reward,confidence,status,source,last_signal_at) "
        "VALUES(?,?,?,?,?,'active','learning',?) "
        "ON CONFLICT(tag) DO UPDATE SET weight=excluded.weight,n_eff=excluded.n_eff,"
        "sum_reward=excluded.sum_reward,confidence=excluded.confidence,"
        "source='learning',last_signal_at=excluded.last_signal_at",
        (tag, mean, n, total, confidence, now),
    )
    _exec(
        plugin,
        "INSERT INTO tag_events(at,tag,bvid,query_tag,signal,reward,share,note) VALUES(?,?,?,?,?,?,?,?)",
        (now, tag, bvid, query_tag, signal, reward, share, "auto"),
    )


def record_reward(
    plugin,
    bvid: str,
    signal: str,
    reward: float,
    *,
    taste_tags: list[str] | None = None,
) -> None:
    """Attribute a video-level reward to query + matched tags."""
    if reward == 0:
        return
    candidate = plugin.db.candidate(bvid) or {}
    try:
        official = json.loads(candidate.get("tags_json") or "[]")
    except Exception:
        official = []
    tags = {_canonical(x) for x in list(official) + list(taste_tags or []) if _canonical(x)}
    tags = {x for x in tags if x and x not in GENERIC_TAGS}
    query = str(candidate.get("source_query") or "")
    query_tag = _query_to_tag(plugin, query) if query else ""
    if query_tag and query_tag not in GENERIC_TAGS:
        tags.add(query_tag)
    if not tags:
        return
    shares: dict[str, float] = {}
    if query_tag and query_tag in tags:
        shares[query_tag] = 0.5
        others = [x for x in tags if x != query_tag]
        if others:
            per = 0.5 / len(others)
            for tag in others:
                shares[tag] = per
    else:
        per = 1.0 / len(tags)
        for tag in tags:
            shares[tag] = per
    for tag, share in shares.items():
        _apply_reward(plugin, tag, float(reward), float(share), signal, bvid, query_tag)
    plugin.db.add_event(
        "tag_reward",
        bvid=bvid,
        signal=signal,
        reward=reward,
        query_tag=query_tag,
        shares={k: round(v, 3) for k, v in shares.items()},
    )


def manual_update(
    plugin,
    *,
    actor: str,
    action: str,
    tag: str,
    value: float = 0.0,
    reason: str = "",
) -> dict[str, Any]:
    """Manual taste edit: assistant ±0.5, admin can set absolute value."""
    tag = _canonical(tag)
    if not tag:
        return {"ok": False, "error": "tag 为空"}
    action = str(action or "").strip().lower()
    if action not in {"add", "adjust", "set", "remove"}:
        return {"ok": False, "error": "action 必须是 add/adjust/set/remove"}
    row = _one(plugin, "SELECT * FROM taste_tags WHERE tag=?", (tag,))
    before = dict(row) if row is not None else None
    current = float(row["manual_delta"] or 0) if row is not None else 0.0
    if action == "remove":
        if row is None:
            return {"ok": False, "error": f"taste 不存在：{tag}"}
        _exec(plugin, "UPDATE taste_tags SET status='muted' WHERE tag=?", (tag,))
    else:
        if actor == "assistant":
            if abs(value) > 0.5 + 1e-9:
                return {"ok": False, "error": "助手单次调整幅度不能超过 0.5"}
            target = current + value if action in {"add", "adjust"} else value
            target = max(-5.0, min(5.0, target))
        else:
            target = value if action == "set" else current + value
            target = max(-5.0, min(5.0, target))
        if row is None:
            _exec(
                plugin,
                "INSERT INTO taste_tags(tag,weight,manual_delta,n_eff,sum_reward,confidence,status,source,last_signal_at,note) "
                "VALUES(?,0,?,0,0,0,'active',?,?,?)",
                (tag, target, actor, _now_iso(), reason),
            )
        else:
            _exec(
                plugin,
                "UPDATE taste_tags SET manual_delta=?,status='active',source=?,note=?,last_signal_at=? WHERE tag=?",
                (target, actor, reason, _now_iso(), tag),
            )
    after_row = _one(plugin, "SELECT * FROM taste_tags WHERE tag=?", (tag,))
    after = dict(after_row) if after_row is not None else None
    _exec(
        plugin,
        "INSERT INTO tag_audit(at,actor,action,owner,tag,before_json,after_json,reason) VALUES(?,?,?,?,?,?,?,?)",
        (
            _now_iso(),
            actor,
            action,
            "taste",
            tag,
            json.dumps(before, ensure_ascii=False, default=str) if before else None,
            json.dumps(after, ensure_ascii=False, default=str) if after else None,
            reason,
        ),
    )
    return {"ok": True, "tag": tag, "action": action, "manual_delta": after.get("manual_delta") if after else None}


def admin_update(
    plugin,
    *,
    tag: str,
    base_weight: float | None = None,
    search_query: str | None = None,
    locked: bool | None = None,
    pinned: bool | None = None,
    status: str | None = None,
    actor: str = "admin",
    reason: str = "",
) -> dict[str, Any]:
    tag = _canonical(tag)
    if not tag:
        return {"ok": False, "error": "tag 为空"}
    row = _one(plugin, "SELECT * FROM admin_tags WHERE tag=?", (tag,))
    before = dict(row) if row is not None else None
    current = dict(row) if row is not None else {
        "tag": tag,
        "search_query": _search_query(tag),
        "base_weight": 1.0,
        "locked": 0,
        "pinned": 0,
        "status": "active",
        "note": "",
    }
    if base_weight is not None:
        current["base_weight"] = max(0.0, min(5.0, float(base_weight)))
    if search_query is not None:
        current["search_query"] = str(search_query).strip() or tag
    if locked is not None:
        current["locked"] = 1 if locked else 0
    if pinned is not None:
        current["pinned"] = 1 if pinned else 0
    if status is not None:
        current["status"] = str(status).strip() or "active"
    if current["pinned"]:
        # Pin is an inclusion promise: only one tag may hold it.
        _exec(plugin, "UPDATE admin_tags SET pinned=0 WHERE tag<>?", (tag,))
    _exec(
        plugin,
        "INSERT INTO admin_tags(tag,search_query,base_weight,locked,pinned,status,note,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(tag) DO UPDATE SET search_query=excluded.search_query,base_weight=excluded.base_weight,"
        "locked=excluded.locked,pinned=excluded.pinned,status=excluded.status,note=excluded.note,updated_at=excluded.updated_at",
        (
            tag,
            current["search_query"],
            current["base_weight"],
            current["locked"],
            current["pinned"],
            current["status"],
            reason,
            _now_iso(),
        ),
    )
    after = dict(_one(plugin, "SELECT * FROM admin_tags WHERE tag=?", (tag,)) or {})
    _exec(
        plugin,
        "INSERT INTO tag_audit(at,actor,action,owner,tag,before_json,after_json,reason) VALUES(?,?,?,?,?,?,?,?)",
        (
            _now_iso(),
            actor,
            "update",
            "admin",
            tag,
            json.dumps(before, ensure_ascii=False, default=str) if before else None,
            json.dumps(after, ensure_ascii=False, default=str),
            reason,
        ),
    )
    return {"ok": True, "tag": tag, "admin": after}


def admin_delete(plugin, *, tag: str, actor: str = "admin", reason: str = "", mute_taste: bool = True) -> dict[str, Any]:
    tag = _canonical(tag)
    if not tag:
        return {"ok": False, "error": "tag 为空"}
    row = _one(plugin, "SELECT * FROM admin_tags WHERE tag=?", (tag,))
    if row is None:
        return {"ok": False, "error": f"Admin Like 不存在：{tag}"}
    before = dict(row)
    _exec(plugin, "DELETE FROM admin_tags WHERE tag=?", (tag,))
    if mute_taste:
        _exec(plugin, "UPDATE taste_tags SET status='muted' WHERE tag=?", (tag,))
    _exec(
        plugin,
        "INSERT INTO tag_audit(at,actor,action,owner,tag,before_json,after_json,reason) VALUES(?,?,?,?,?,?,?,?)",
        (
            _now_iso(),
            actor,
            "delete",
            "admin",
            tag,
            json.dumps(before, ensure_ascii=False, default=str),
            None,
            reason + ("; taste muted" if mute_taste else ""),
        ),
    )
    return {"ok": True, "tag": tag, "deleted": True, "taste_muted": bool(mute_taste)}


def block_update(plugin, *, tag: str, action: str, reason: str = "", actor: str = "admin") -> dict[str, Any]:
    tag = _canonical(tag)
    if not tag:
        return {"ok": False, "error": "tag 为空"}
    if action == "block":
        _exec(
            plugin,
            "INSERT OR REPLACE INTO block_tags(tag,reason,created_by,created_at) VALUES(?,?,?,?)",
            (tag, reason, actor, _now_iso()),
        )
    elif action == "unblock":
        _exec(plugin, "DELETE FROM block_tags WHERE tag=?", (tag,))
    else:
        return {"ok": False, "error": "action 必须是 block/unblock"}
    _exec(
        plugin,
        "INSERT INTO tag_audit(at,actor,action,owner,tag,reason) VALUES(?,?,?,?,?,?)",
        (_now_iso(), actor, action, "block", tag, reason),
    )
    return {"ok": True, "tag": tag, "action": action}


def audit(plugin, limit: int = 100) -> list[dict]:
    rows = _rows(plugin, "SELECT * FROM tag_audit ORDER BY id DESC LIMIT ?", (max(1, min(500, int(limit))),))
    return [dict(r) for r in rows]


def snapshot(plugin) -> dict[str, Any]:
    ensure_seed(plugin)
    blocked = set(block_list(plugin))
    tastes = _taste_rows(plugin)
    admin = []
    for row in _rows(plugin, "SELECT * FROM admin_tags ORDER BY tag"):
        tag = str(row["tag"])
        trow = tastes.get(tag)
        mean, precision, n, confidence = _posterior(trow)
        manual = float(trow["manual_delta"] or 0) if trow is not None else 0.0
        admin.append(
            {
                "tag": tag,
                "search_query": row["search_query"],
                "base_weight": row["base_weight"],
                "locked": bool(row["locked"]),
                "pinned": bool(row["pinned"]),
                "status": row["status"],
                "assistant_delta": round(mean + manual, 4),
                "effective": round(float(row["base_weight"] or 0) + (0 if row["locked"] else mean + manual), 4),
                "n": round(n, 2),
                "confidence": round(confidence, 3),
                "blocked": tag in blocked,
            }
        )
    taste = []
    for tag, row in tastes.items():
        mean, precision, n, confidence = _posterior(row)
        manual = float(row["manual_delta"] or 0)
        taste.append(
            {
                "tag": tag,
                "weight": round(mean + manual, 4),
                "learned_weight": round(mean, 4),
                "manual_delta": round(manual, 4),
                "n": round(n, 2),
                "confidence": round(confidence, 3),
                "status": row["status"],
                "source": row["source"],
                "last_signal_at": row["last_signal_at"],
            }
        )
    return {
        "admin": admin,
        "taste": sorted(taste, key=lambda x: x["weight"], reverse=True),
        "block": sorted(blocked),
    }
