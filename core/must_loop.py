from __future__ import annotations

import asyncio
from datetime import datetime

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from . import must as must_mod
from .wake import wake_session


class MustDispatcher:
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
        self._task = asyncio.create_task(self.run(), name="whale-sense-must")

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
        await asyncio.sleep(4)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] must tick 失败: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    def _idle_ok(self) -> bool:
        if self.db.get_kv("force_next", False):
            return True
        now = datetime.now().astimezone()
        last = self.db.get_kv("last_assistant_message_at") or self.db.get_kv("started_at")
        if not last:
            return True
        try:
            dt = datetime.fromisoformat(str(last))
        except Exception:
            return True
        minutes = max(1, int(self.cfg.get("idle_minutes", 60) or 60))
        return (now - dt).total_seconds() >= minutes * 60

    async def tick(self) -> None:
        if self.plugin.clock.paused():
            return
        now = datetime.now().astimezone()
        if self.plugin.clock.in_quiet(now):
            return
        pushed = self.db.pushed_must()
        if pushed:
            if pushed["state"] == "pushing":
                try:
                    dt = datetime.fromisoformat(str(pushed.get("pushed_at") or ""))
                except Exception:
                    dt = now
                if (now - dt).total_seconds() > 600:
                    self.db.update_must(pushed["id"], state="pending")
                    self.plugin.logger.info(f"[whale-sense] must {pushed['id']} 发送超时，退回 pending")
            return
        item = must_mod.next_pending(self.plugin)
        if not item:
            return
        if not self._idle_ok():
            return
        # 优先用终评时助手自己写好的 push_text 直接发；这比再唤醒一轮更可靠，
        # 也不会和 work 会话的 deep-judge 抢同一个 active job 队列。
        push_text = str(item.get("push_text") or "").strip()
        if push_text:
            try:
                ok = await self.plugin.context.send_message(
                    self.cfg.admin_umo, MessageChain([Plain(push_text)])
                )
                if ok:
                    must_mod.confirm_pushed(self.plugin, item["id"])
                    self.plugin.db.set_kv("last_assistant_message_at", datetime.now().astimezone().isoformat(timespec="seconds"))
                    self.plugin.db.set_kv("force_next", False)
                    self.plugin.db.add_event(
                        "must_pushed", must_id=item["id"], bvid=item.get("bvid"), direct=True
                    )
                    self.plugin.logger.info(f"[whale-sense] 已直接推送 must {item['id']}")
                else:
                    self.plugin.logger.warning(f"[whale-sense] 直接推送 must {item['id']} 失败")
            except Exception as exc:
                self.plugin.logger.warning(f"[whale-sense] 直接推送 must 失败: {exc}")
            return
        try:
            await wake_session(
                self.plugin,
                self.cfg.admin_umo,
                must_mod.push_prompt(self.plugin, item),
                origin="must-push",
            )
            must_mod.mark_pushing(self.plugin, item["id"])
            self.db.set_kv("force_next", False)
            self.plugin.logger.info(f"[whale-sense] 已叫醒 admin 推 must {item['id']}")
        except Exception as exc:
            self.plugin.logger.warning(f"[whale-sense] 叫醒 admin 推 must 失败: {exc}")
