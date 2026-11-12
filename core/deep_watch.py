from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .prompts import judge_prompt
from .wake import wake_session


def _extract_json(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    fenced = raw.find("```")
    if fenced >= 0:
        after = raw.find("\n", fenced)
        end = raw.find("```", after + 1)
        if after >= 0 and end > after:
            raw = raw[after + 1 : end].strip()
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except Exception:
                    return None
    return None


def _normalize_frames(value: Any) -> list[dict[str, Any]]:
    out = []
    for item in value or []:
        if isinstance(item, str):
            out.append({"path": item, "t": None, "note": ""})
        elif isinstance(item, dict):
            out.append(
                {
                    "path": str(item.get("path") or item.get("file") or ""),
                    "t": item.get("t"),
                    "note": str(item.get("note") or ""),
                }
            )
    return [x for x in out if x["path"]]


class DeepWatcher:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.db = plugin.db
        self.cfg = plugin.cfg
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="whale-sense-deep-watch")

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
        await asyncio.sleep(3)
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] deep watch 失败: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    def _cache(self) -> Path:
        raw = str(self.cfg.get("gateway_cache") or "").strip()
        # 没配置就按 dsh 侧默认的项目目录推：~/dsh-astrbot-gateway/cache
        if not raw:
            return Path.home() / "dsh-astrbot-gateway" / "cache"
        return Path(raw).expanduser()

    def _timeout_sec(self) -> int:
        return max(30, int(self.cfg.get("dsh_timeout_sec", 300) or 300))

    async def poll_once(self) -> None:
        for task in self.db.running_deep_tasks():
            if task["state"] == "judging":
                continue
            task_id = str(task.get("dsh_task_id") or "")
            if not task_id:
                continue
            outbox = self._cache() / "outbox" / f"{task_id}.json"
            if not outbox.is_file():
                # dsh_status 读取结果后会把 outbox/<id>.json 挪进 done/；两边都认。
                outbox = self._cache() / "outbox" / "done" / f"{task_id}.json"
            if not outbox.is_file():
                self._check_timeout(task)
                continue
            try:
                envelope = json.loads(outbox.read_text(encoding="utf-8"))
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] outbox 解析失败 {task_id}: {exc}")
                continue
            if str(envelope.get("status") or "") not in {"done", "ok", "success"}:
                self.plugin.logger.info(f"[whale-sense] outbox {task_id} 状态 {envelope.get('status')}，继续等")
                continue
            payload = _extract_json(str(envelope.get("content") or ""))
            if payload is None:
                self.plugin.logger.warning(f"[whale-sense] outbox {task_id} 没有 JSON 结果")
                self.db.update_deep_task(task_id, state="failed", deep_finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                continue
            bvid = str(payload.get("bvid") or task.get("bvid") or "")
            transcript = str(payload.get("transcript") or "")
            keyframes = _normalize_frames(payload.get("keyframes"))
            if not transcript.strip() and not keyframes:
                self.plugin.logger.warning(f"[whale-sense] {bvid} 既无转录也无关键帧，无法终评")
                self.db.update_deep_task(task_id, state="failed", deep_finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                continue
            artifacts = {
                "bvid": bvid,
                "transcript": transcript,
                "transcript_source": payload.get("transcript_source"),
                "keyframes": keyframes,
                "scene_notes": payload.get("scene_notes") or [],
                "has_subtitle": payload.get("has_subtitle"),
                "errors": payload.get("errors") or [],
            }
            self.db.update_deep_task(
                task_id,
                state="judging",
                deep_finished_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                artifacts_json=json.dumps(artifacts, ensure_ascii=False),
            )
            meta_row = self.db.candidate(bvid) or {}
            meta = {
                "bvid": bvid,
                "title": meta_row.get("title"),
                "up": meta_row.get("up"),
                "partition": meta_row.get("partition"),
                "tags": json.loads(meta_row.get("tags_json") or "[]"),
                "stats": json.loads(meta_row.get("stats_json") or "{}"),
            }
            is_admin = str(task.get("origin") or "") == "admin_chat"
            note = judge_prompt(task, meta, transcript, keyframes, admin=is_admin)
            images = [k["path"] for k in keyframes if Path(k["path"]).is_file()][:6]
            wake_umo = str(task.get("wake_umo") or "")
            if not wake_umo:
                wake_umo = self.plugin.cfg.admin_umo if is_admin else self.plugin.cfg.work_umo
            try:
                await wake_session(
                    self.plugin,
                    wake_umo,
                    note,
                    images=images,
                    origin="deep-judge-admin" if is_admin else "deep-judge",
                )
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] 唤醒终评失败 {task_id}: {exc}")
                self.db.update_deep_task(task_id, state="running")
            # 一次只唤醒一条终评，避免两个 deep 同时占用 work 会话。
            return

    def _check_timeout(self, task: dict) -> None:
        started = str(task.get("deep_started_at") or "")
        if not started:
            return
        try:
            dt = datetime.fromisoformat(started)
        except Exception:
            return
        age = (datetime.now(dt.tzinfo) - dt).total_seconds()
        if age <= self._timeout_sec():
            return
        task_id = str(task.get("dsh_task_id") or "")
        self.plugin.logger.warning(f"[whale-sense] deep 任务超时 {task_id}（{int(age)}s）")
        self.db.update_deep_task(task_id, state="failed")
