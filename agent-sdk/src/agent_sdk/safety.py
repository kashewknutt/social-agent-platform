"""Humanized delays and daily session caps."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import date, datetime
from pathlib import Path


def human_delay(min_seconds: float, max_seconds: float) -> float:
    return random.uniform(min_seconds, max_seconds)


async def async_sleep(min_seconds: float, max_seconds: float) -> None:
    await asyncio.sleep(human_delay(min_seconds, max_seconds))


def scroll_delay() -> float:
    return human_delay(2.0, 5.0)


def profile_query_delay() -> float:
    return human_delay(60.0, 120.0)


def session_cooldown_seconds() -> int:
    return random.randint(3 * 3600, 6 * 3600)


class SessionLimiter:
    """File-backed daily session counter."""

    def __init__(self, usage_log: Path, max_sessions_per_day: int = 4) -> None:
        self.usage_log = Path(usage_log)
        self.max_sessions_per_day = max_sessions_per_day

    def _load(self) -> dict:
        if self.usage_log.exists():
            return json.loads(self.usage_log.read_text(encoding="utf-8"))
        return {"date": str(date.today()), "scroll_sessions": 0}

    def _save(self, data: dict) -> None:
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        self.usage_log.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _today_usage(self) -> dict:
        usage = self._load()
        today = str(date.today())
        if usage.get("date") != today:
            usage = {"date": today, "scroll_sessions": 0}
            self._save(usage)
        return usage

    def can_start(self) -> bool:
        usage = self._today_usage()
        return usage["scroll_sessions"] < self.max_sessions_per_day

    def record(self) -> dict:
        usage = self._today_usage()
        usage["scroll_sessions"] += 1
        usage["last_session_at"] = datetime.now().isoformat()
        self._save(usage)
        return usage

    def info(self) -> dict:
        usage = self._today_usage()
        used = int(usage.get("scroll_sessions", 0))
        return {
            "date": usage.get("date", str(date.today())),
            "scroll_sessions": used,
            "max_scroll_sessions_per_day": self.max_sessions_per_day,
            "sessions_remaining": max(0, self.max_sessions_per_day - used),
            "last_session_at": usage.get("last_session_at"),
        }
