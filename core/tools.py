from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool


@dataclass
class WhaleScreenResultTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_screen_result"
    description: str = (
        "提交本轮 5 条候选的初筛结果。只接受内部 work 会话。"
        "results 里每条包含 bvid、screen(no|maybe|deep)、one_line、reason、hypothesis。"
        "提交后返回每个 deep 的 DSH 派单模板。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "本轮批次 id"},
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "bvid": {"type": "string"},
                            "screen": {"type": "string", "enum": ["no", "maybe", "deep"]},
                            "one_line": {"type": "string"},
                            "reason": {"type": "string"},
                            "hypothesis": {"type": "string"},
                        },
                        "required": ["bvid", "screen"],
                    },
                },
            },
            "required": ["batch_id", "results"],
        }
    )

    async def run(self, event, batch_id: str, results: list[dict[str, Any]]) -> str:
        return await self.plugin.handle_screen_result(event, batch_id, results)


@dataclass
class WhaleTrackDeepTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_track_deep"
    description: str = (
        "登记一条已经通过 dsh_dispatch 派出的深读任务。只接受内部 work 会话。"
        "task_id 是 dsh_dispatch 返回的任务 id；插件会轮询 outbox，结果回来后再次唤醒 work 会话。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "bvid": {"type": "string"},
                "hypothesis": {"type": "string"},
                "must_id": {"type": "string", "description": "管理员主动视频的 must_id；普通自动 deep 留空。"},
            },
            "required": ["task_id", "bvid"],
        }
    )

    async def run(self, event, task_id: str, bvid: str, hypothesis: str = "", must_id: str = "") -> str:
        return await self.plugin.handle_track_deep(event, task_id, bvid, hypothesis, must_id)


@dataclass
class WhaleJudgeResultTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_judge_result"
    description: str = (
        "提交一条深读视频的终评。只接受内部 work 会话。"
        "tier 为 must/maybe/no；memory_id 是 memorize_long_term_memory 返回的 id（失败填 0）。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "bvid": {"type": "string"},
                "tier": {"type": "string", "enum": ["must", "maybe", "no"]},
                "one_line": {"type": "string"},
                "evaluation": {"type": "string"},
                "key_moments": {"type": "array", "items": {"type": "string"}},
                "why_deep": {"type": "string"},
                "memory_id": {"type": "integer", "default": 0},
                "diary_entry": {"type": "string"},
                "push_text": {
                    "type": "string",
                    "description": "轮到你主动分享时，直接发给管理员的话：2-4 句、带链接、不要报告。",
                    "default": "",
                },
                "taste_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "这条视频让你形成的 3-8 个 canonical 标签；可留空。",
                },
                "confidence": {"type": "number", "default": 0.5},
            },
            "required": ["bvid", "tier", "one_line", "evaluation"],
        }
    )

    async def run(
        self,
        event,
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
        return await self.plugin.handle_judge_result(
            event,
            bvid=bvid,
            tier=tier,
            one_line=one_line,
            evaluation=evaluation,
            key_moments=key_moments or [],
            why_deep=why_deep,
            memory_id=memory_id,
            diary_entry=diary_entry,
            push_text=push_text,
            taste_tags=taste_tags or [],
            confidence=confidence,
        )


@dataclass
class WhaleClassifyMustTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_classify_must"
    description: str = (
        "管理员私聊里，聊完一条 must 后调用。只接受 admin 会话。"
        "archived_done=聊透；discarded_not_interested=不感兴趣，丢弃；later_shallow=没聊透，进副队列（最多 3 次）。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "must_id": {"type": "string"},
                "outcome": {
                    "type": "string",
                    "enum": ["archived_done", "discarded_not_interested", "later_shallow"],
                },
                "note": {"type": "string", "default": ""},
            },
            "required": ["must_id", "outcome"],
        }
    )

    async def run(self, event, must_id: str, outcome: str, note: str = "") -> str:
        return await self.plugin.handle_classify_must(event, must_id, outcome, note)


@dataclass
class WhalePushNextTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_push_next"
    description: str = (
        "用户说“下一条/继续/还有吗”时调用。只接受 admin 会话。"
        "会跳过 1 小时空闲限制，立即推下一条 must；当前 pushed 未分类时按 classify_current 处理。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "classify_current": {
                    "type": "string",
                    "enum": ["later_shallow", "archived_done", "discarded_not_interested"],
                    "default": "later_shallow",
                }
            },
        }
    )

    async def run(self, event, classify_current: str = "later_shallow") -> str:
        return await self.plugin.handle_push_next(event, classify_current)


@dataclass
class WhaleRecallUndiscussedTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_recall_undiscussed"
    description: str = (
        "检索“还没聊透、已出队”的 must，用于用户主动说“上次那条没聊透的再聊聊”。只接受 admin 会话。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "k": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        }
    )

    async def run(self, event, query: str, k: int = 5) -> str:
        return await self.plugin.handle_recall_undiscussed(event, query, k)


@dataclass
class WhaleTasteUpdateTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_taste_update"
    description: str = (
        "表达/修改助手自己的 taste（不是管理员 like tags，也不是 block）。只接受 work 会话。"
        "action=add/adjust/remove，单次权重调整不超过 0.5；所有修改写审计。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "adjust", "remove"]},
                "tag": {"type": "string"},
                "weight": {"type": "number", "default": 0.5},
                "reason": {"type": "string", "default": ""},
            },
            "required": ["action", "tag"],
        }
    )

    async def run(self, event, action: str, tag: str, weight: float = 0.5, reason: str = "") -> str:
        return await self.plugin.handle_taste_update(event, action, tag, weight, reason)


@dataclass
class WhaleAddMustTool(FunctionTool):
    plugin: Any = field(repr=False, default=None)

    name: str = "whale_add_must"
    description: str = (
        "管理员在对话里发来 B 站视频/BV 号时调用：立即建一条 admin must，返回 DSH 只读任务模板。"
        "只接受 admin 会话；不占自动 deep/must 配额；后续用 dsh_dispatch 和 whale_track_deep(..., must_id)。"
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url_or_bvid": {"type": "string", "description": "B 站链接或 BV 号"},
                "note": {"type": "string", "default": "", "description": "管理员为什么发它；可空"},
            },
            "required": ["url_or_bvid"],
        }
    )

    async def run(self, event, url_or_bvid: str, note: str = "") -> str:
        return await self.plugin.handle_add_must(event, url_or_bvid, note)


TOOL_CLASSES = (
    WhaleScreenResultTool,
    WhaleTrackDeepTool,
    WhaleJudgeResultTool,
    WhaleClassifyMustTool,
    WhalePushNextTool,
    WhaleRecallUndiscussedTool,
    WhaleTasteUpdateTool,
    WhaleAddMustTool,
)
