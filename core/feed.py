from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from . import taste


class FeedError(RuntimeError):
    pass


async def _run_cli(plugin, args: list[str], timeout: int = 120, env: dict | None = None) -> dict[str, Any]:
    node = plugin.resolve_node()
    raw_cli = str(plugin.cfg.get("feed_cli") or "").strip()
    cli = Path(raw_cli).expanduser() if raw_cli else Path("")
    if not node:
        raise FeedError("找不到 node 运行时（配置 node_path 或 DSH_NODE）")
    if not raw_cli:
        raise FeedError("未配置 feed_cli：填 bili-sense.mjs 的绝对路径（面板 → 插件配置）")
    if not cli.is_file():
        raise FeedError(f"找不到 bili-sense CLI：{cli}")
    proc = await asyncio.create_subprocess_exec(
        node,
        str(cli),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cli.parent),
        env=env,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise FeedError(f"bili-sense 超时（{timeout}s）")
    text = out.decode("utf-8", "replace").strip()
    stderr = err.decode("utf-8", "replace").strip()
    if stderr:
        for line in stderr.splitlines()[-3:]:
            plugin.logger.info(f"[whale-sense] bili-sense: {line}")
    try:
        data = json.loads(text)
    except Exception as exc:
        raise FeedError(f"bili-sense 输出不是 JSON（exit={proc.returncode}）：{text[:300]}") from exc
    if not data.get("ok"):
        raise FeedError(str(data.get("error") or "bili-sense 返回失败"))
    return data


def _cookie_env(plugin) -> dict:
    env = os.environ.copy()
    sessdata = str(plugin.cfg.get("bili_sessdata") or "").strip()
    bili_jct = str(plugin.cfg.get("bili_jct") or "").strip()
    if sessdata:
        env["BILI_SESSDATA"] = sessdata
        env["BILI_JCT"] = bili_jct
    return env


async def fetch_candidates(plugin) -> dict[str, Any]:
    seen = sorted(plugin.db.recent_bvids())
    try:
        queries = taste.select_queries(plugin, limit=5)
    except Exception as exc:
        plugin.logger.warning(f"[whale-sense] 选搜索词失败，回退默认 seed: {exc}")
        queries = []
    if not queries:
        queries = list(taste.DEFAULT_LIKE_TAGS[:5])
    try:
        block = taste.block_list(plugin)
    except Exception:
        block = list(taste.DEFAULT_DISLIKE_TAGS)

    # 时长上下限：0 表示该项不限。注意别写成 `int(x or 默认值)`，否则填 0（不限）会被顶回默认。
    def _sec(key: str, fallback: int) -> int:
        raw = plugin.cfg.get(key, fallback)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return fallback

    min_sec = _sec("min_duration_sec", 180)
    max_sec = _sec("max_duration_sec", 1800)
    args = [
        "feed",
        "--json",
        "--n",
        str(max(1, int(plugin.cfg.get("feed_n", 5) or 5))),
        "--min-duration",
        str(min_sec),
        "--max-duration",
        str(max_sec),
        "--like-tags",
        ",".join(queries),
        "--dislike-tags",
        ",".join(block),
    ]
    plugin.logger.info(f"[whale-sense] 时长区间 {min_sec}s–{max_sec or '不限'}s")
    if seen:
        args += ["--seen", ",".join(seen[-200:])]
    plugin.logger.info(f"[whale-sense] 本轮搜索词：{queries}")
    return await _run_cli(plugin, args, env=_cookie_env(plugin))


async def fetch_meta(plugin, bvid: str) -> dict[str, Any]:
    return await _run_cli(plugin, ["meta", bvid, "--json"], timeout=60, env=_cookie_env(plugin))
