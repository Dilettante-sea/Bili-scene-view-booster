from __future__ import annotations

from typing import Any


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 会话、CLI 路径、gateway 缓存目录都留空：每个人机器不一样，装完在面板里填
    "work_umo": "",
    "admin_umo": "",
    "feed_cli": "",
    "feed_n": 5,
    "min_duration_sec": 180,
    "max_duration_sec": 1800,
    "bili_sessdata": "",
    "bili_jct": "",
    "node_path": "",
    "gateway_cache": "",
    "dsh_timeout_sec": 600,
    "persona_id": "default",
    "provider_id": "",
    "clock_windows": "20:00-24:00",
    "clock_wakes": 2,
    "skip_chance": 0.3,
    "min_gap_min": 10,
    "quiet_hours": "00:00-08:00",
    "deep_per_day": 5,
    "deep_per_wake": 3,
    "must_per_day": 2,
    "must_backlog_per_day": 1,
    "maybe_per_day": 1,
    "idle_minutes": 60,
    "max_shallow_attempts": 3,
    "diary_dir": "",
    "report_daily_at": "23:30",
    "report_weekly_at": "Sun 21:00",
    "dry_run": False,
}


def _coerce(key: str, value: Any) -> Any:
    default = DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, list):
        if isinstance(value, str):
            return [
                item.strip()
                for item in value.replace("，", ",").replace(";", ",").split(",")
                if item.strip()
            ]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return list(default)
    return value


class Config:
    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.raw = dict(raw or {})
        self._values = dict(DEFAULTS)
        for key, value in self.raw.items():
            if key in self._values:
                self._values[key] = _coerce(key, value)
            else:
                self._values[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    @property
    def work_umo(self) -> str:
        return str(self.get("work_umo") or "").strip()

    @property
    def admin_umo(self) -> str:
        return str(self.get("admin_umo") or "").strip()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)
