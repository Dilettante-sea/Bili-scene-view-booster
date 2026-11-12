from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr
from quart import jsonify, request

from .core.clock import Clock
from .core.config import Config
from .core.db import DB, now_iso
from .core.deep_watch import DeepWatcher
from .core.diary import append_entry
from .core.feed import fetch_candidates, fetch_meta
from .core import must as must_mod
from .core import taste
from .core.must_loop import MustDispatcher
from .core.prompts import deep_dispatch_content, screen_prompt
from .core.report import ReportScheduler, daily_text, send_report
from .core.tools import TOOL_CLASSES
from .core.wake import wake_session


PLUGIN_NAME = "astrbot_plugin_whale_sense"
VERSION = "0.1.0"


class WhaleSensePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.cfg = Config(dict(config or {}))
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.db = DB(self.data_dir / "whale.db", self.data_dir / "whale-audit.jsonl")
        self.db.init()
        self.clock = Clock(self)
        self.deep_watcher = DeepWatcher(self)
        self.must_dispatcher = MustDispatcher(self)
        self.report_scheduler = ReportScheduler(self)
        self.tools = []
        self._started = False
        try:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/status", self.api_status, ["GET"], "鲸的感官状态"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/run", self.api_run, ["POST"], "手动触发一轮抓取"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/page/tags", self.api_tags, ["GET"], "标签画像"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/page/tags/admin", self.api_tags_admin, ["POST"], "管理员标签"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/page/tags/taste", self.api_tags_taste, ["POST"], "助手 taste"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/page/tags/block", self.api_tags_block, ["POST"], "屏蔽标签"
            )
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/page/tags/audit", self.api_tags_audit, ["GET"], "标签审计"
            )
        except Exception as exc:
            logger.warning(f"[whale-sense] 注册 HTTP 路由失败: {exc}")

    async def initialize(self) -> None:
        self._started = True
        self.db.set_kv("started_at", now_iso())
        self.tools = [cls(plugin=self) for cls in TOOL_CLASSES]
        self.context.add_llm_tools(*self.tools)
        self.clock.start()
        self.deep_watcher.start()
        self.must_dispatcher.start()
        self.report_scheduler.start()
        self.logger.info(
            f"[whale-sense] {VERSION} 启动；work={self.cfg.work_umo or '未配置'}；"
            f"admin={self.cfg.admin_umo or '未配置'}；data={self.data_dir}；tools={len(self.tools)}"
        )

    async def terminate(self) -> None:
        self._started = False
        await self.report_scheduler.stop()
        await self.must_dispatcher.stop()
        await self.deep_watcher.stop()
        await self.clock.stop()
        try:
            mgr = self.context.provider_manager.llm_tools
            for tool in self.tools:
                mgr.remove_func(tool.name)
        except Exception:
            pass
        # 不要在 terminate 里 close：AstrBot 重载插件时，上一轮 work 会话的
        # whale_* 工具调用可能还在飞；提前关连接会让它报 "closed database"。
        # 旧实例的 SQLite 连接随进程回收，保留着让在途工具调用能落库。
        self.logger.info("[whale-sense] 已停止")

    # ---- hooks ---------------------------------------------------------
    @filter.after_message_sent()
    async def on_after_message_sent(self, event: AstrMessageEvent) -> None:
        """Record when the assistant last spoke in the admin room.

        The must dispatcher uses this timestamp: a must push waits until one hour
        after the assistant's last outbound message, not the user's last message.
        """
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            return
        if umo != self.cfg.admin_umo:
            return
        self.db.set_kv("last_assistant_message_at", now_iso())
        self.db.set_kv("last_assistant_message_umo", umo)
        pending = self.db.pushed_must()
        if pending and pending.get("state") == "pushing":
            must_mod.confirm_pushed(self, pending["id"])
            self.db.add_event("must_pushed", must_id=pending["id"], bvid=pending.get("bvid"))

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """Remind the assistant to classify a pushed must after the user replies."""
        try:
            if str(event.unified_msg_origin) != self.cfg.admin_umo:
                return
            pending = self.db.pushed_must()
            if not pending:
                return
            payload = {}
            getter = getattr(event, "get_extra", None)
            if callable(getter):
                payload = getter("cron_payload") or {}
            if str(payload.get("origin") or "") == "must-push":
                return
            text = (
                f"【must 提醒】你有一条 must #{pending['id']}（{pending.get('bvid')}）"
                f"处于 {pending.get('state')}。如果这一轮用户回复/聊到了它，聊完后调用 "
                f"whale_classify_must(must_id='{pending['id']}', outcome=...)。"
                "outcome 三选一：archived_done（聊透）/ discarded_not_interested（不感兴趣）/ "
                "later_shallow（没聊透）。如果用户说下一条，调用 whale_push_next。"
            )
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is None:
                req.extra_user_content_parts = []
                parts = req.extra_user_content_parts
            parts.append(TextPart(text=text).mark_as_temp())
        except Exception:
            return

    async def on_clock_fire(self, plan: dict) -> None:
        if plan.get("skip"):
            self.logger.info("[whale-sense] 今天不想看（skip），按计划不抓候选")
            return
        self.logger.info(f"[whale-sense] 到点醒来：{plan.get('at')}，开始抓候选")
        try:
            await self.run_cycle()
        except Exception as exc:
            self.logger.warning(f"[whale-sense] run_cycle 失败: {exc}")

    # ---- M2 pipeline ---------------------------------------------------
    def _validate_work(self, event) -> bool:
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            return False
        return umo == self.cfg.work_umo

    async def run_cycle(self, dry: bool = False) -> str:
        if self.clock.paused():
            return "已暂停，不抓候选。"
        data = await fetch_candidates(self)
        candidates = data.get("candidates") or []
        min_duration = max(0, int(self.cfg.get("min_duration_sec", 180) or 180))
        if min_duration > 0:
            before = len(candidates)
            candidates = [
                c for c in candidates
                if int(c.get("duration") or 0) >= min_duration
            ]
            if len(candidates) != before:
                self.logger.info(
                    f"[whale-sense] 时长过滤：{before} → {len(candidates)}（≥{min_duration}s）"
                )
        if not candidates:
            return f"bili-sense 没有返回 ≥{min_duration}s 的候选。"
        batch_id = f"batch-{now_iso().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:4]}"
        self.db.add_candidates(batch_id, candidates)
        quota = {
            "deep_left": int(self.cfg.get("deep_per_day", 5) or 5),
            "must_left": int(self.cfg.get("must_per_day", 2) or 2),
        }
        prompt = screen_prompt(batch_id, candidates, quota)
        if dry or self.cfg.get("dry_run"):
            self.logger.info(f"[whale-sense] dry_run：批次 {batch_id}，{len(candidates)} 条候选，未唤醒")
            return f"dry_run：已抓 {len(candidates)} 条候选（批次 {batch_id}），未唤醒 work 会话。"
        await wake_session(self, self.cfg.work_umo, prompt, origin="screen")
        self.db.add_event("batch", batch_id=batch_id, count=len(candidates))
        return f"已抓 {len(candidates)} 条候选（批次 {batch_id}），已唤醒 work 会话。"

    async def handle_screen_result(self, event, batch_id: str, results: list[dict]) -> str:
        if not self._validate_work(event):
            return "错误：whale_screen_result 只接受内部 work 会话。"
        rows = {r["bvid"]: r for r in self.db.candidates_by_batch(batch_id)}
        if not rows:
            return f"错误：找不到批次 {batch_id} 的候选。"
        counts = {"no": 0, "maybe": 0, "deep": 0}
        deep = []
        for item in results or []:
            bvid = str(item.get("bvid") or "")
            if bvid not in rows:
                continue
            screen = str(item.get("screen") or "").lower()
            if screen not in counts:
                continue
            one_line = str(item.get("one_line") or "")
            reason = str(item.get("reason") or "")
            hypothesis = str(item.get("hypothesis") or "")
            self.db.update_candidate_screen(batch_id, bvid, screen, one_line, reason, hypothesis)
            counts[screen] += 1
            if screen == "deep":
                deep.append({"bvid": bvid, "content": deep_dispatch_content(bvid, hypothesis)})
        self.db.add_event("screen", batch_id=batch_id, counts=counts)
        return json.dumps(
            {
                "ok": True,
                "batch_id": batch_id,
                "counts": counts,
                "deep": deep,
                "next": "对每个 deep：调 dsh_dispatch(content=上面给出的 content)，再调 whale_track_deep(task_id, bvid, hypothesis)。",
            },
            ensure_ascii=False,
        )

    async def handle_add_must(self, event, url_or_bvid: str, note: str = "") -> str:
        if not self._validate_admin(event):
            return "错误：whale_add_must 只接受管理员私聊会话。"
        match = re.search(r"BV[0-9A-Za-z]{10}", str(url_or_bvid or ""))
        if not match:
            return "错误：没有识别到 BV 号。请发 B 站视频链接或 BV 号。"
        bvid = match.group(0)
        try:
            meta = await fetch_meta(self, bvid)
        except Exception as exc:
            return f"错误：读取视频信息失败：{exc}"
        title = str(meta.get("title") or bvid)
        candidate = {
            "bvid": bvid,
            "title": title,
            "up": meta.get("up") or "",
            "partition": meta.get("partition") or "",
            "tags": meta.get("tags") or [],
            "duration": meta.get("duration") or 0,
            "desc": meta.get("desc") or "",
            "stat": meta.get("stat") or {},
            "rates": meta.get("rates") or {},
            "source": "admin",
        }
        self.db.add_candidates("admin", [candidate])
        must_id = f"m-admin-{now_iso().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:4]}"
        self.db.add_admin_must(must_id, bvid, title, note)
        self.db.add_event("admin_must_added", bvid=bvid, must_id=must_id, title=title)
        content = deep_dispatch_content(bvid, note or "管理员主动发来的视频")
        return json.dumps(
            {
                "ok": True,
                "must_id": must_id,
                "bvid": bvid,
                "title": title,
                "duration": meta.get("duration"),
                "deep_content": content,
                "next": "调 dsh_dispatch(content=deep_content)，再调 whale_track_deep(task_id, bvid, hypothesis, must_id)。",
            },
            ensure_ascii=False,
        )

    async def handle_track_deep(
        self, event, task_id: str, bvid: str, hypothesis: str = "", must_id: str = ""
    ) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        is_work = umo == self.cfg.work_umo
        is_admin = umo == self.cfg.admin_umo
        admin_must = self.db.get_must(must_id) if must_id else None
        if not is_work:
            if not is_admin or not admin_must or admin_must.get("source") != "admin":
                return "错误：whale_track_deep 只接受 work，或 admin 会话里的管理员 must。"
        if not task_id or not bvid:
            return "错误：task_id 和 bvid 都不能为空。"
        candidate = self.db.candidate(bvid) or {}
        origin = "admin_chat" if is_admin and admin_must else "auto"
        wake_umo = self.cfg.admin_umo if origin == "admin_chat" else self.cfg.work_umo
        self.db.add_deep_task(
            bvid,
            task_id,
            str(candidate.get("batch_id") or ""),
            hypothesis,
            must_id=must_id,
            origin=origin,
            wake_umo=wake_umo,
        )
        if admin_must:
            self.db.update_must(must_id, state="admin_reading")
        self.db.add_event(
            "deep_track", bvid=bvid, task_id=task_id, must_id=must_id or None, origin=origin
        )
        return f"已登记 deep 任务 {task_id}（{bvid}，{origin}）；插件会轮询 outbox，结果回来后唤醒对应会话终评。"

    async def handle_judge_result(
        self,
        event,
        *,
        bvid: str,
        tier: str,
        one_line: str,
        evaluation: str,
        key_moments: list[str] | None = None,
        why_deep: str = "",
        memory_id: int = 0,
        diary_entry: str = "",
        push_text: str = "",
        taste_tags: list[str] | None = None,
        confidence: float = 0.5,
    ) -> str:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        is_work = umo == self.cfg.work_umo
        is_admin = umo == self.cfg.admin_umo
        admin_must = self.db.admin_must_for_bvid(bvid)
        if not is_work:
            if not is_admin or not admin_must:
                return "错误：whale_judge_result 只接受 work，或 admin 会话里的管理员 must。"
        tier = str(tier or "").lower()
        if tier not in {"must", "maybe", "no"}:
            return "错误：tier 必须是 must/maybe/no。"
        self.db.update_deep_by_bvid(bvid, state="done", deep_finished_at=now_iso())
        if not admin_must:
            self.db.bump_quota(must_mod.today(), "deep_used")
        candidate = self.db.candidate(bvid) or {}
        try:
            title = str(candidate.get("title") or bvid)
            tags = json.loads(candidate.get("tags_json") or "[]")
            append_entry(
                self,
                bvid=bvid,
                tier=tier,
                title=title,
                tags=tags,
                one_line=one_line,
                evaluation=evaluation,
                diary_entry=diary_entry,
                key_moments=key_moments or [],
            )
        except Exception as exc:
            self.logger.warning(f"[whale-sense] 写日记失败 {bvid}: {exc}")

        must_id = ""
        if admin_must:
            must_id = str(admin_must["id"])
            try:
                self.db.mark_admin_judged(
                    must_id, one_line, why_deep or evaluation, int(memory_id or 0)
                )
                self.db.add_event(
                    "admin_must_judged", bvid=bvid, must_id=must_id, tier=tier
                )
            except Exception as exc:
                self.logger.warning(f"[whale-sense] 更新管理员 must 失败 {bvid}: {exc}")
        elif tier == "must":
            if not str(push_text or "").strip():
                push_text = f"{one_line}\nhttps://www.bilibili.com/video/{bvid}"
            try:
                item = must_mod.enqueue(
                    self,
                    bvid=bvid,
                    memory_id=int(memory_id or 0),
                    one_line=one_line,
                    why=why_deep or evaluation,
                    push_text=push_text,
                )
                must_id = item["id"]
            except Exception as exc:
                self.logger.warning(f"[whale-sense] must 入队失败 {bvid}: {exc}")

        try:
            taste.record_reward(
                self,
                bvid,
                f"judge_{tier}",
                {"must": 0.3, "maybe": 0.1, "no": -0.5}.get(tier, 0.0),
                taste_tags=taste_tags,
            )
        except Exception as exc:
            self.logger.warning(f"[whale-sense] 更新 taste 失败 {bvid}: {exc}")

        self.db.add_event(
            "judge",
            bvid=bvid,
            tier=tier,
            memory_id=int(memory_id or 0),
            confidence=float(confidence or 0),
            one_line=one_line,
            why_deep=why_deep,
            key_moments=key_moments or [],
            must_id=must_id or None,
            source="admin" if admin_must else "auto",
        )
        return json.dumps(
            {
                "ok": True,
                "bvid": bvid,
                "tier": tier,
                "memory_id": int(memory_id or 0),
                "must_id": must_id,
                "source": "admin" if admin_must else "auto",
            },
            ensure_ascii=False,
        )

    # ---- M3 must / recall tools ---------------------------------------
    def _validate_admin(self, event) -> bool:
        try:
            umo = str(event.unified_msg_origin)
        except Exception:
            return False
        return umo == self.cfg.admin_umo

    async def handle_classify_must(self, event, must_id: str, outcome: str, note: str = "") -> str:
        if not self._validate_admin(event):
            return "错误：whale_classify_must 只接受管理员私聊会话。"
        item = self.db.get_must(must_id) or {}
        result = must_mod.classify(self, must_id, outcome, note)
        if result.get("ok") and item.get("bvid"):
            reward = {
                "archived_done": 1.0,
                "discarded_not_interested": -0.6,
                "later_shallow": 0.0,
            }.get(str(outcome), 0.0)
            try:
                taste.record_reward(self, str(item["bvid"]), f"classify_{outcome}", reward)
            except Exception as exc:
                self.logger.warning(f"[whale-sense] 分类更新 taste 失败 {must_id}: {exc}")
        return json.dumps(result, ensure_ascii=False)

    async def handle_taste_update(
        self, event, action: str, tag: str, weight: float = 0.5, reason: str = ""
    ) -> str:
        if not self._validate_work(event):
            return "错误：whale_taste_update 只接受内部 work 会话。"
        return json.dumps(
            taste.manual_update(
                self, actor="assistant", action=action, tag=tag, value=float(weight or 0), reason=reason
            ),
            ensure_ascii=False,
        )

    async def handle_push_next(self, event, classify_current: str = "later_shallow") -> str:
        if not self._validate_admin(event):
            return "错误：whale_push_next 只接受管理员私聊会话。"
        pushed = self.db.pushed_must()
        if pushed and pushed.get("state") in {"pushing", "pushed"}:
            result = must_mod.classify(self, pushed["id"], classify_current, "user asked next")
            if not result.get("ok"):
                return json.dumps(result, ensure_ascii=False)
        self.db.set_kv("force_next", True)
        return json.dumps({"ok": True, "message": "已排队，下一 tick 立即推下一条 must。"}, ensure_ascii=False)

    async def handle_recall_undiscussed(self, event, query: str, k: int = 5) -> str:
        if not self._validate_admin(event):
            return "错误：whale_recall_undiscussed 只接受管理员私聊会话。"
        rows = self.db.search_undiscussed(query, max(1, min(20, int(k or 5))))
        return json.dumps(
            {
                "ok": True,
                "query": query,
                "items": [
                    {
                        "must_id": r["id"],
                        "bvid": r["bvid"],
                        "memory_id": r["memory_id"],
                        "one_line": r["one_line"],
                        "why": r["why"],
                    }
                    for r in rows
                ],
            },
            ensure_ascii=False,
        )

    # ---- helpers -------------------------------------------------------
    def resolve_node(self) -> str:
        configured = str(self.cfg.get("node_path") or "").strip()
        if configured and Path(configured).is_file():
            return configured
        env = str(os.environ.get("DSH_NODE") or "").strip()
        if env and Path(env).is_file():
            return env
        candidates: list[Path] = []
        # AstrBot 启动器自带的 node：<launcher>/components/nodejs/node.exe
        try:
            launcher = Path(sys.executable).parents[4]
            candidates.append(launcher / "components" / "nodejs" / "node.exe")
            candidates.append(launcher / "components" / "nodejs" / "node")
        except Exception:
            pass
        # 装了 Node.js 的机器一般也在这里
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(Path(base) / "nodejs" / "node.exe")
        for p in candidates:
            if p.is_file() and p.suffix.lower() in {"", ".exe"}:
                return str(p)
        which = shutil.which("node")
        if which and Path(which).suffix.lower() == ".exe":
            return which
        return ""

    def _idle_text(self) -> str:
        last = self.db.get_kv("last_assistant_message_at")
        if not last:
            return "还没有记录（视为空闲）"
        return str(last)

    async def api_status(self):
        return jsonify({"ok": True, "text": self._status_text(), "clock": self.clock.status()})

    async def api_run(self):
        data = await request.get_json(silent=True) or {}
        dry = bool(data.get("dry", False))
        try:
            msg = await self.run_cycle(dry=dry)
            return jsonify({"ok": True, "message": msg})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 500

    async def api_tags(self):
        return jsonify({"ok": True, **taste.snapshot(self)})

    async def api_tags_admin(self):
        data = await request.get_json(silent=True) or {}
        action = str(data.get("action") or "update").strip().lower()
        if action == "delete":
            result = taste.admin_delete(
                self,
                tag=str(data.get("tag") or ""),
                actor="admin",
                reason=str(data.get("reason") or ""),
                mute_taste=bool(data.get("mute_taste", True)),
            )
            return jsonify(result)
        result = taste.admin_update(
            self,
            tag=str(data.get("tag") or ""),
            base_weight=data.get("base_weight"),
            search_query=data.get("search_query"),
            locked=data.get("locked"),
            pinned=data.get("pinned"),
            status=data.get("status"),
            actor="admin",
            reason=str(data.get("reason") or ""),
        )
        return jsonify(result)

    async def api_tags_taste(self):
        data = await request.get_json(silent=True) or {}
        result = taste.manual_update(
            self,
            actor="admin",
            action=str(data.get("action") or "set"),
            tag=str(data.get("tag") or ""),
            value=float(data.get("weight") or 0),
            reason=str(data.get("reason") or "admin edit"),
        )
        return jsonify(result)

    async def api_tags_block(self):
        data = await request.get_json(silent=True) or {}
        result = taste.block_update(
            self,
            tag=str(data.get("tag") or ""),
            action=str(data.get("action") or "block"),
            reason=str(data.get("reason") or ""),
            actor="admin",
        )
        return jsonify(result)

    async def api_tags_audit(self):
        return jsonify({"ok": True, "items": taste.audit(self, 200)})

    def _status_text(self) -> str:
        cfg = self.cfg
        stats = self.db.stats()
        clock = self.clock.status()
        plan = clock.get("plan") or {}
        feed = Path(str(cfg.get("feed_cli") or ""))
        cache = Path(str(cfg.get("gateway_cache") or ""))
        node = self.resolve_node()
        lines = [
            f"鲸的感官 v{VERSION}",
            f"状态：{'暂停' if clock.get('paused') else '运行中'}",
            f"work_umo：{cfg.work_umo or '（未配置）'}",
            f"admin_umo：{cfg.admin_umo or '（未配置）'}",
            f"feed CLI：{feed} {'✓' if feed.is_file() else '✗ 不存在'}",
            f"node：{node or '✗ 未找到'}",
            f"gateway cache：{cache} {'✓' if cache.is_dir() else '✗ 不存在'}",
            f"数据库：{self.db.path}",
            f"下一次醒来：{plan.get('at_text') or '未排计划'}"
            + (f"（窗口 {plan.get('window')}）" if plan.get("window") else ""),
            f"上次醒来：{clock.get('last_fire') or '从未'}",
            f"上次助手发言（admin）：{self._idle_text()}",
            f"工具：{len(self.tools)} 个；deep 轮询：5s",
            f"must：主 {stats['must_primary']} / 副 {stats['must_secondary']} / 已推 {stats['must_pushed']}",
            f"今日：deep {self.db.quota(must_mod.today())['deep_used']}/{cfg.get('deep_per_day')}；"
            f"must 产出 {self.db.quota(must_mod.today())['must_produced']}/{must_mod.production_quota(self)}",
            f"deep 待处理：{stats['deep_pending']}；候选累计：{stats['candidates']}",
            f"deep 额度：{cfg.get('deep_per_day')}/天；must 产出：{cfg.get('must_per_day')}/天"
            f"（积压日 {cfg.get('must_backlog_per_day')}）",
        ]
        return "\n".join(lines)

    # ---- commands ------------------------------------------------------
    @filter.command_group("whale")
    def whale(self):
        """鲸的感官管理命令：/whale status|ping|pause|resume|clock|help"""

    @permission_type(PermissionType.ADMIN)
    @whale.command("status")
    async def cmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(self._status_text())

    @permission_type(PermissionType.ADMIN)
    @whale.command("ping")
    async def cmd_ping(self, event: AstrMessageEvent, target: str = "work"):
        target = (target or "work").strip().lower()
        if target in {"work", "w"}:
            umo, label = self.cfg.work_umo, "work"
        elif target in {"admin", "a"}:
            umo, label = self.cfg.admin_umo, "admin"
        else:
            yield event.plain_result("用法：/whale ping work|admin")
            return
        if not umo:
            yield event.plain_result(f"{label} 的 UMO 未配置")
            return
        text = f"[whale-sense] ping {label} @ {now_iso()}"
        try:
            ok = await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:
            yield event.plain_result(f"发送失败：{exc}")
            return
        yield event.plain_result(f"{'✓ 已发送' if ok else '✗ 平台未找到'} → {umo}")

    @permission_type(PermissionType.ADMIN)
    @whale.command("pause")
    async def cmd_pause(self, event: AstrMessageEvent):
        self.db.set_kv("paused", True)
        yield event.plain_result("已暂停：时钟不再产生新观看；must 队列保留。")

    @permission_type(PermissionType.ADMIN)
    @whale.command("resume")
    async def cmd_resume(self, event: AstrMessageEvent):
        self.db.set_kv("paused", False)
        yield event.plain_result("已恢复：下一次时钟 tick 会重新排计划。")

    @permission_type(PermissionType.ADMIN)
    @whale.command("clock")
    async def cmd_clock(self, event: AstrMessageEvent, action: str = ""):
        action = (action or "").strip().lower()
        if action in {"reset", "clear"}:
            self.db.set_kv("clock_plan", None)
            yield event.plain_result("已清除当前计划；下一 tick 重排。")
            return
        status = self.clock.status()
        plan = status.get("plan") or {}
        yield event.plain_result(
            "时钟：\n"
            f"  暂停：{status.get('paused')}\n"
            f"  计划：{plan.get('at_text') or '无'}（窗口 {plan.get('window') or '-'}，skip={plan.get('skip')}）\n"
            f"  上次触发：{status.get('last_fire') or '从未'}\n"
            f"  今日计数：{status.get('used')}"
        )

    @permission_type(PermissionType.ADMIN)
    @whale.command("run")
    async def cmd_run(self, event: AstrMessageEvent, mode: str = ""):
        try:
            msg = await self.run_cycle(dry=(mode or "").strip().lower() == "dry")
        except Exception as exc:
            msg = f"抓取失败：{exc}"
        yield event.plain_result(msg)

    @permission_type(PermissionType.ADMIN)
    @whale.command("pending")
    async def cmd_pending(self, event: AstrMessageEvent):
        yield event.plain_result(must_mod.pending_summary(self))

    @permission_type(PermissionType.ADMIN)
    @whale.command("must")
    async def cmd_must(self, event: AstrMessageEvent, action: str = "", must_id: str = ""):
        action = (action or "").strip().lower()
        if action == "next":
            self.db.set_kv("force_next", True)
            yield event.plain_result("已排队：下一 tick 立即推下一条 must（若当前 pushed 未分类，会按 later_shallow 处理）。")
            return
        mapping = {"done": "archived_done", "drop": "discarded_not_interested", "later": "later_shallow"}
        if action not in mapping or not must_id:
            yield event.plain_result("用法：/whale must done|drop|later <must_id>，或 /whale must next")
            return
        result = must_mod.classify(self, must_id, mapping[action], "admin command")
        yield event.plain_result(json.dumps(result, ensure_ascii=False))

    @permission_type(PermissionType.ADMIN)
    @whale.command("recall")
    async def cmd_recall(self, event: AstrMessageEvent, query: GreedyStr):
        rows = self.db.search_undiscussed(str(query), 10)
        if not rows:
            yield event.plain_result("没有找到“还没聊透”的记录。")
            return
        lines = ["还没聊透、已出队的内容："]
        for r in rows:
            lines.append(f"- {r['id']} {r['bvid']} {r.get('one_line') or ''}")
        yield event.plain_result("\n".join(lines))

    @permission_type(PermissionType.ADMIN)
    @whale.command("report")
    async def cmd_report(self, event: AstrMessageEvent, date: str = ""):
        yield event.plain_result(daily_text(self, date.strip() or None))

    @permission_type(PermissionType.ADMIN)
    @whale.command("show")
    async def cmd_show(self, event: AstrMessageEvent, target: str = ""):
        target = (target or "").strip()
        item = self.db.get_must(target)
        if not item and target.isdigit():
            item = self.db.find_must_by_memory(int(target))
        if not item:
            yield event.plain_result("找不到这条 must（用 must_id 或 memory_id）")
            return
        yield event.plain_result(
            f"{item['id']}  {item['bvid']}  [{item['queue']}/{item['state']}]  "
            f"source={item.get('source') or 'auto'}\n"
            f"memory_id：{item['memory_id']}\n"
            f"一句话：{item.get('one_line')}\n"
            f"push_text：{item.get('push_text') or '（无）'}\n"
            f"why：{item.get('why')}\n"
            f"attempts：{item.get('attempts')}  discussion：{item.get('discussion_status')}"
        )

    @permission_type(PermissionType.ADMIN)
    @whale.command("forget")
    async def cmd_forget(self, event: AstrMessageEvent, memory_id: str = ""):
        if not str(memory_id).isdigit():
            yield event.plain_result("用法：/whale forget <memory_id>")
            return
        ok, msg = await self._delete_memory(int(memory_id))
        if ok:
            item = self.db.find_must_by_memory(int(memory_id))
            if item:
                self.db.update_must(item["id"], state="discarded_not_interested", note="admin forget")
        yield event.plain_result(msg)

    @permission_type(PermissionType.ADMIN)
    @whale.command("why")
    async def cmd_why(self, event: AstrMessageEvent, bvid: str = ""):
        cand = self.db.candidate(bvid)
        if not cand:
            yield event.plain_result("没有这条候选记录。")
            return
        lines = [
            f"{bvid} {cand.get('title')}",
            f"screen：{cand.get('screen')}  {cand.get('one_line') or ''}",
            f"reason：{cand.get('reason') or ''}",
        ]
        for ev in self.db.events_for(bvid, 5):
            lines.append(f"- {ev['at']} {ev['kind']} {str(ev.get('payload_json'))[:160]}")
        yield event.plain_result("\n".join(lines)[:3500])

    @permission_type(PermissionType.ADMIN)
    @whale.command("deep")
    async def cmd_deep(self, event: AstrMessageEvent, limit: str = "10"):
        try:
            n = max(1, min(50, int(limit)))
        except Exception:
            n = 10
        rows = self.db.list_deep(n)
        if not rows:
            yield event.plain_result("没有 deep 记录。")
            return
        lines = ["最近 deep："]
        for r in rows:
            lines.append(f"- {r['dsh_task_id']} {r['bvid']} [{r['state']}]")
        yield event.plain_result("\n".join(lines)[:3500])

    async def _delete_memory(self, memory_id: int) -> tuple[bool, str]:
        try:
            meta = self.context.get_registered_star("astrbot_plugin_livingmemory")
            engine = meta.star_cls.initializer.memory_engine if meta and meta.star_cls else None
            if engine is None:
                return False, "LivingMemory 未加载，暂时删不了。"
            await engine.delete_memory(int(memory_id))
            self.db.add_event("memory_forgotten", memory_id=int(memory_id))
            return True, f"已删除记忆 {memory_id}（审计已记）。"
        except Exception as exc:
            return False, f"删除记忆失败：{exc}"

    @permission_type(PermissionType.ADMIN)
    @whale.command("tags")
    async def cmd_tags(self, event: AstrMessageEvent):
        snap = taste.snapshot(self)
        lines = ["标签画像："]
        for r in snap.get("admin", [])[:12]:
            lines.append(
                f"- {r['tag']}  base={r['base_weight']}  Δ={r['assistant_delta']}  "
                f"effective={r['effective']}  {'locked' if r['locked'] else ''}{' pinned' if r['pinned'] else ''}"
            )
        if snap.get("taste"):
            lines.append("助手 taste：")
            for r in snap["taste"][:12]:
                lines.append(
                    f"- {r['tag']}  weight={r['weight']}  n={r['n']}  conf={r['confidence']}  {r['status']}"
                )
        if snap.get("block"):
            lines.append("屏蔽：" + " / ".join(snap["block"]))
        lines.append("完整权重/审计请打开插件页面 → tags")
        yield event.plain_result("\n".join(lines)[:3500])

    @permission_type(PermissionType.ADMIN)
    @whale.command("help")
    async def cmd_help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "用法：\n"
            "/whale status — 查看运行状态\n"
            "/whale run [dry] — 立刻抓一批候选并唤醒 work 会话\n"
            "/whale pending — 查看 must 主/副队列\n"
            "/whale tags — 查看 Admin Like / 助手 taste / 屏蔽权重\n"
            "/whale must done|drop|later <id> / next — 手动处理 must\n"
            "/whale recall <词> — 检索“还没聊透、已出队”的内容\n"
            "/whale report [date] — 日报/审计摘要\n"
            "/whale show <must_id|memory_id> — 看一条 must 的详情\n"
            "/whale forget <memory_id> — 删除一条记忆（写审计）\n"
            "/whale why <bvid> — 初筛/深读/终评链路\n"
            "/whale deep [n] — 最近 deep 任务\n"
            "/whale ping work|admin — 测试两条 Matrix 会话\n"
            "/whale pause / resume — 暂停/恢复自主观看\n"
            "/whale clock [reset] — 查看/重置时钟计划"
        )
