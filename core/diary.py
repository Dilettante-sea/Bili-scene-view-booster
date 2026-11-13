from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def diary_root(plugin) -> Path:
    """日记根目录。

    默认落在**插件自己所在的目录**下的 diary/（跟着插件走，备份插件就备份了日记）；
    想放别处就配 diary_dir。
    """
    configured = str(plugin.cfg.get("diary_dir") or "").strip()
    base = (
        Path(configured).expanduser()
        if configured
        else Path(__file__).resolve().parent.parent / "diary"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def daily_path(plugin, when: datetime | None = None) -> Path:
    """<root>/2026-11/11-13.md —— 一天一个文件。"""
    now = when or datetime.now().astimezone()
    path = diary_root(plugin) / f"{now.year}-{now.month:02d}" / f"{now.month}-{now.day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def weekly_path(plugin, start: date, end: date) -> Path:
    """<root>/2026-11/11-01~07.md —— 一期周报一个文件。"""
    path = (
        diary_root(plugin) / f"{start.year}-{start.month:02d}" / f"{start.month}-{start.day}~{end.day}.md"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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
    path = daily_path(plugin, now)
    tier = (tier or "maybe").lower()
    tags_text = " / ".join(tags[:6]) if tags else "-"
    body = diary_entry.strip() or evaluation.strip() or one_line.strip()
    moments = [str(x) for x in (key_moments or []) if str(x).strip()]

    with _LOCK:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        chunks = []
        if not text.strip():
            chunks.append(f"# {day}\n")

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


def append_report(
    plugin,
    kind: str,
    text: str,
    *,
    week: tuple[date, date] | None = None,
) -> Path:
    """把日报/周报正文写进对应的 md：日报进当天的 11-13.md，周报进 11-01~07.md。"""
    body = (text or "").strip()
    now = datetime.now().astimezone()
    if week:
        start, end = week
        path = weekly_path(plugin, start, end)
        head = f"# {start.isoformat()} ~ {end.isoformat()}\n"
    else:
        path = daily_path(plugin, now)
        head = f"# {now.date().isoformat()}\n"
    with _LOCK:
        fresh = not path.exists() or not path.read_text(encoding="utf-8").strip()
        with path.open("a", encoding="utf-8", newline="\n") as f:
            if fresh:
                f.write(head)
            f.write(f"\n## {kind}\n\n{body}\n")
    return path
