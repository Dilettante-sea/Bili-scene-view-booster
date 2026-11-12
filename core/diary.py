from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def _diary_path(plugin) -> Path:
    configured = str(plugin.cfg.get("diary_dir") or "").strip()
    base = Path(configured) if configured else Path(plugin.data_dir) / "diary"
    base.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    return base / f"{now.strftime('%Y-%m')}.md"


def append_entry(
    plugin,
    *,
    bvid: str,
    tier: str,
    title: str,
    tags: list[str],
    one_line: str,
    evaluation: str,
    diary_entry: str,
    key_moments: list[str] | None = None,
) -> Path:
    now = datetime.now().astimezone()
    day = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    path = _diary_path(plugin)
    tier = (tier or "maybe").lower()
    tags_text = " / ".join(tags[:6]) if tags else "-"
    body = diary_entry.strip() or evaluation.strip() or one_line.strip()
    moments = [str(x) for x in (key_moments or []) if str(x).strip()]

    with _LOCK:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        chunks = []
        if f"## {day}" not in text:
            chunks.append(f"\n## {day}\n")

        if tier == "must":
            if "### 看过（must）" not in text:
                chunks.append("\n### 看过（must）\n")
                chunks.append("| 时间 | BV | 标题 | 标签 | 一句话 |\n|---|---|---|---|---|\n")
            chunks.append(f"| {hhmm} | {bvid} | {title} | {tags_text} | {one_line} |\n")
            chunks.append(f"\n#### {hhmm} {title}\n\n{body}\n")
            if moments:
                chunks.append("\n关键瞬间：\n" + "\n".join(f"- {m}" for m in moments) + "\n")
            chunks.append(f"\n来源：https://www.bilibili.com/video/{bvid}\n")
        elif tier == "maybe":
            if "### 略看（maybe）" not in text:
                chunks.append("\n### 略看（maybe）\n")
            chunks.append(f"- {hhmm} 《{title}》{bvid} —— {one_line}。标签：{tags_text}\n")
        else:
            if "### 深读后没采纳（deep-no）" not in text:
                chunks.append("\n### 深读后没采纳（deep-no）\n")
            chunks.append(f"- {hhmm} 《{title}》{bvid} —— {body}\n")

        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write("".join(chunks))
    return path
