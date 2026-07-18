"""Instagram data ingestion via browser-use and Kimi."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ig_agent.config import RAW_DIR, Settings, get_settings
from ig_agent.llm import get_browser_llm
from ig_agent.safety import (
    async_sleep,
    can_start_scroll_session,
    record_scroll_session,
    scroll_delay,
)


def _build_ingest_task(settings: Settings, hashtags: list[str] | None = None) -> str:
    tags = hashtags or []
    tag_clause = ""
    if tags:
        tag_clause = f" Also search these hashtags: {', '.join(tags)}."

    return (
        f"Go to https://www.instagram.com/explore/ and scroll down slowly. "
        f"Observe trending posts and video Reels. Identify up to {settings.max_posts_per_session} posts "
        f"related to technology, software development, SaaS, developer workflows, or corporate office life. "
        f"{tag_clause}"
        f"For each post extract: post_url, caption, likes, views (if visible), "
        f"comments_count (if visible), post_type (reel/carousel/image), timestamp (if visible). "
        f"Do NOT like, comment, follow, or interact with any posts. Only observe and extract data. "
        f"Output the collected data as structured JSON with a top-level 'posts' array."
    )


def _parse_agent_result(result: Any) -> list[dict[str, Any]]:
    """Best-effort parse of browser agent output into post list."""
    text = str(result)
    # Try to find JSON in the result
    json_match = re.search(r"\{[\s\S]*\"posts\"[\s\S]*\}", text)
    if json_match:
        try:
            data = json.loads(json_match.group())
            posts = data.get("posts", [])
            if isinstance(posts, list):
                return posts
        except json.JSONDecodeError:
            pass

    # Fallback: wrap raw text
    return [{"raw_text": text, "post_url": None, "caption": text[:500]}]


async def capture_trends(
    settings: Settings | None = None,
    hashtags: list[str] | None = None,
) -> Path:
    """Run a single browser-use ingestion pass and save raw JSON."""
    from browser_use import Agent, BrowserSession

    cfg = settings or get_settings()
    if not can_start_scroll_session(cfg):
        raise RuntimeError(
            f"Daily scroll session limit ({cfg.max_scroll_sessions_per_day}) reached."
        )

    llm = get_browser_llm(cfg)
    # Do not pass system Chrome executable_path: browser-use copies Chrome
    # profiles to a temp dir and login cookies are lost between runs.
    # Bundled Chromium + a stable user_data_dir keeps the Instagram session.
    browser = BrowserSession(
        headless=False,
        user_data_dir=str(cfg.browser_user_data_dir),
    )
    task = _build_ingest_task(cfg, hashtags)

    try:
        agent = Agent(task=task, llm=llm, browser=browser)
        history = await agent.run()
        result = history.final_result() if hasattr(history, "final_result") else str(history)
        posts = _parse_agent_result(result)
    finally:
        await browser.kill()

    record_scroll_session()
    output = {
        "timestamp": datetime.now().isoformat(),
        "post_count": len(posts),
        "posts": posts,
    }
    filename = f"scraped_{int(datetime.now().timestamp())}.json"
    out_path = RAW_DIR / filename
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return out_path


async def capture_trends_with_delays(
    settings: Settings | None = None,
    hashtags: list[str] | None = None,
) -> Path:
    """Ingest with pre-session humanized delay."""
    await async_sleep(1.0, 3.0)
    path = await capture_trends(settings, hashtags)
    await asyncio.sleep(scroll_delay())
    return path
