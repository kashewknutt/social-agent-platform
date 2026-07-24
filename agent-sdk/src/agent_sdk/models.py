"""Shared schemas for bot control and status."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class BotState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"
    STOPPED = "stopped"


class RunMode(str, Enum):
    ONCE = "once"
    DAEMON = "daemon"


class RunRequest(BaseModel):
    mode: RunMode = RunMode.ONCE
    sample: bool = False
    multimodal: bool = False
    offline: bool = False
    engage: bool = True
    # Ingest source for this run. Instagram uses:
    # "people_first" (niche hashtags/phrases/profiles first — preferred),
    # "posts" (hashtag grid first), or "reels" (scroll IG Reels feed).
    # Empty string falls back to Direction.research_mode.
    content_mode: str = ""


class Direction(BaseModel):
    brand_name: str = ""
    business_type: str = ""
    website: str = ""
    region: str = ""
    target_audience: list[str] = Field(default_factory=list)
    content_pillars: list[str] = Field(default_factory=list)
    brand_voice: str = ""
    competitor_hashtags: list[str] = Field(default_factory=list)
    competitor_profiles: list[str] = Field(default_factory=list)
    # Intent-rich Instagram search phrases (one concept per entry).
    discovery_phrases: list[str] = Field(default_factory=list)
    # Preferred media formats, e.g. talking_head, microphone, podcast_interview.
    preferred_formats: list[str] = Field(default_factory=list)
    # Persisted ingest strategy: "people_first" | "posts" | "reels".
    research_mode: str = "people_first"
    goals: str = ""
    constraints: str = (
        "Discover via niche hashtags/phrases/profiles. Like only gated content. "
        "Follows require human approval (HITL); comment/DM/post also HITL."
    )


class UsageInfo(BaseModel):
    date: str = ""
    scroll_sessions: int = 0
    max_scroll_sessions_per_day: int = 0
    sessions_remaining: int = 0
    last_session_at: str | None = None
    likes: int = 0
    follows: int = 0
    comments: int = 0
    dms: int = 0
    posts: int = 0
    max_likes_per_day: int = 0
    max_follows_per_day: int = 0
    max_comments_per_day: int = 0
    max_dms_per_day: int = 0
    max_posts_per_day: int = 0
    likes_remaining: int = 0
    follows_remaining: int = 0
    comments_remaining: int = 0
    dms_remaining: int = 0
    posts_remaining: int = 0


class ArtifactInfo(BaseModel):
    kind: str
    path: str
    modified_at: str | None = None


class StatusPayload(BaseModel):
    bot_id: str
    name: str
    network: str
    state: BotState
    current_step: str | None = None
    last_action: str | None = None
    last_error: str | None = None
    run_id: str | None = None
    mode: RunMode | None = None
    usage: UsageInfo | None = None
    artifacts: list[ArtifactInfo] = Field(default_factory=list)
    live: dict[str, Any] | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class RunEvent(BaseModel):
    ts: str = Field(default_factory=lambda: datetime.now().isoformat())
    run_id: str
    level: str = "info"
    step: str | None = None
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    ok: bool = True
    bot_id: str
    state: BotState


class MessageResponse(BaseModel):
    ok: bool = True
    message: str
    state: BotState
