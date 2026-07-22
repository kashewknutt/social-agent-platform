"""Humanized delays and daily rate limits for Instagram safety."""

from __future__ import annotations

import asyncio
import json
import random
from datetime import date, datetime
from typing import Any

from ig_agent.config import DATA_DIR, Settings, get_settings

USAGE_LOG = DATA_DIR / "usage_log.json"

ACTION_KEYS = ("likes", "follows", "comments", "dms", "posts", "scroll_sessions")

ACTION_TO_USAGE_KEY = {
    "like": "likes",
    "follow": "follows",
    "comment": "comments",
    "dm": "dms",
    "post": "posts",
    "scroll": "scroll_sessions",
}


def human_delay(min_seconds: float, max_seconds: float) -> float:
    """Return a randomized delay within bounds."""
    return random.uniform(min_seconds, max_seconds)


async def async_sleep(min_seconds: float, max_seconds: float) -> None:
    delay = human_delay(min_seconds, max_seconds)
    await asyncio.sleep(delay)


def _empty_usage(today: str | None = None) -> dict[str, Any]:
    return {
        "date": today or str(date.today()),
        "scroll_sessions": 0,
        "likes": 0,
        "follows": 0,
        "comments": 0,
        "dms": 0,
        "posts": 0,
    }


def _load_usage() -> dict[str, Any]:
    if USAGE_LOG.exists():
        try:
            data = json.loads(USAGE_LOG.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    today = str(date.today())
    if data.get("date") != today:
        data = _empty_usage(today)
        _save_usage(data)
        return data
    for key in ACTION_KEYS:
        data.setdefault(key, 0)
    return data


def _save_usage(data: dict[str, Any]) -> None:
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    USAGE_LOG.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _cap_for(action: str, settings: Settings) -> int:
    mapping = {
        "like": settings.max_likes_per_day,
        "follow": settings.max_follows_per_day,
        "comment": settings.max_comments_per_day,
        "dm": settings.max_dms_per_day,
        "post": settings.max_posts_per_day,
        "scroll": settings.max_scroll_sessions_per_day,
    }
    if action not in mapping:
        raise ValueError(f"Unknown action: {action}")
    return mapping[action]


def remaining_cap(action: str, settings: Settings | None = None) -> int:
    """How many more of this action are allowed today."""
    cfg = settings or get_settings()
    usage = _load_usage()
    key = ACTION_TO_USAGE_KEY.get(action)
    if key is None:
        raise ValueError(f"Unknown action: {action}")
    used = int(usage.get(key, 0))
    return max(0, _cap_for(action, cfg) - used)


def can_perform(action: str, settings: Settings | None = None) -> bool:
    return remaining_cap(action, settings) > 0


def record_action(action: str, count: int = 1) -> dict[str, Any]:
    """Increment today's usage counter for an engagement/scroll action."""
    key = ACTION_TO_USAGE_KEY.get(action)
    if key is None:
        raise ValueError(f"Unknown action: {action}")
    usage = _load_usage()
    usage[key] = int(usage.get(key, 0)) + count
    usage["last_session_at"] = datetime.now().isoformat()
    _save_usage(usage)
    return usage


def can_start_scroll_session(settings: Settings | None = None) -> bool:
    """Check if another scroll session is allowed today."""
    return can_perform("scroll", settings)


def record_scroll_session() -> None:
    record_action("scroll")


def scroll_delay() -> float:
    """Delay between scroll sessions."""
    return human_delay(0.8, 2.0)


def engagement_delay(kind: str) -> float:
    """Short pause between engagement actions (keeps a light human rhythm).

    HITL execute used to wait 25–90s per comment/DM which felt frozen.
    These are intentionally snappy; daily caps still limit volume.
    """
    ranges = {
        "like": (0.8, 2.5),
        "follow": (1.5, 4.0),
        "comment": (2.0, 6.0),
        "dm": (3.0, 8.0),
        "post": (8.0, 20.0),
    }
    lo, hi = ranges.get(kind, (1.0, 3.0))
    return human_delay(lo, hi)


async def async_engagement_delay(kind: str, *, scale: float = 1.0) -> None:
    delay = engagement_delay(kind) * max(0.0, scale)
    if delay > 0:
        await asyncio.sleep(delay)


def profile_query_delay() -> float:
    """Delay between profile/hashtag queries."""
    return human_delay(60.0, 120.0)


def session_cooldown_seconds() -> int:
    """Idle time between sessions (3–6 hours)."""
    return random.randint(3 * 3600, 6 * 3600)


def usage_snapshot(settings: Settings | None = None) -> dict[str, Any]:
    """Structured usage payload for status APIs."""
    cfg = settings or get_settings()
    usage = _load_usage()
    return {
        "date": usage.get("date", ""),
        "scroll_sessions": int(usage.get("scroll_sessions", 0)),
        "max_scroll_sessions_per_day": cfg.max_scroll_sessions_per_day,
        "sessions_remaining": remaining_cap("scroll", cfg),
        "last_session_at": usage.get("last_session_at"),
        "likes": int(usage.get("likes", 0)),
        "follows": int(usage.get("follows", 0)),
        "comments": int(usage.get("comments", 0)),
        "dms": int(usage.get("dms", 0)),
        "posts": int(usage.get("posts", 0)),
        "max_likes_per_day": cfg.max_likes_per_day,
        "max_follows_per_day": cfg.max_follows_per_day,
        "max_comments_per_day": cfg.max_comments_per_day,
        "max_dms_per_day": cfg.max_dms_per_day,
        "max_posts_per_day": cfg.max_posts_per_day,
        "likes_remaining": remaining_cap("like", cfg),
        "follows_remaining": remaining_cap("follow", cfg),
        "comments_remaining": remaining_cap("comment", cfg),
        "dms_remaining": remaining_cap("dm", cfg),
        "posts_remaining": remaining_cap("post", cfg),
    }
