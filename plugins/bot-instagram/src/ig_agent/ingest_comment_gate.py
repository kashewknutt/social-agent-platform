"""Pause ingest on each reel and wait for operator comment approve/skip."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Callable

logger = logging.getLogger("ig_agent.ingest_comment_gate")

_pending: dict[str, Any] | None = None
_decision: dict[str, Any] | None = None
_lock = asyncio.Lock()


def get_pending() -> dict[str, Any] | None:
    return dict(_pending) if _pending else None


def clear_pending() -> None:
    global _pending, _decision
    _pending = None
    _decision = None


def submit_decision(*, approve: bool, text: str | None = None) -> bool:
    """Called from API when operator approves or skips the ingest comment popup."""
    global _decision
    if _pending is None:
        return False
    _decision = {
        "action": "approve" if approve else "skip",
        "text": (text or _pending.get("draft_text") or "").strip() if approve else "",
    }
    return True


def _clear_pending_from_live(controller: Any) -> None:
    if controller is None:
        return
    live = dict(controller.live or {})
    live.pop("pending_comment", None)
    controller.set_live(live)


async def wait_for_ingest_comment(
    post: dict[str, Any],
    draft_text: str,
    *,
    controller: Any | None = None,
    run_id: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    timeout_s: float = 180.0,
) -> dict[str, Any]:
    """Block until operator approves/skips via Fleet popup, times out, or stop requested."""
    global _pending, _decision

    async with _lock:
        _decision = None
        token = uuid.uuid4().hex[:12]
        _pending = {
            "token": token,
            "run_id": run_id,
            "post_url": post.get("post_url"),
            "caption": (post.get("caption") or "")[:400],
            "username": post.get("username"),
            "draft_text": (draft_text or "").strip(),
            "requested_at": datetime.now().isoformat(),
        }

    if controller is not None:
        live = dict(controller.live or {})
        live["pending_comment"] = dict(_pending)
        controller.set_live(live)
        controller.set_step("ingest", "Waiting for your comment approval…")

    cap = (post.get("caption") or post.get("post_url") or "reel")[:56]
    msg = f"Comment approval needed — {cap}"
    if on_progress:
        on_progress(msg)
    logger.info(msg)

    deadline = asyncio.get_event_loop().time() + timeout_s
    try:
        while asyncio.get_event_loop().time() < deadline:
            if should_stop and should_stop():
                return {"action": "skip", "reason": "stop"}
            if controller is not None and controller._stop.is_set():
                return {"action": "skip", "reason": "stop"}
            if _decision is not None:
                result = dict(_decision)
                return result
            await asyncio.sleep(0.25)
        logger.info("Ingest comment approval timed out — skipping")
        return {"action": "skip", "reason": "timeout"}
    finally:
        clear_pending()
        _clear_pending_from_live(controller)


async def prompt_and_post_ingest_comment(
    browser: Any,
    post: dict[str, Any],
    *,
    controller: Any | None = None,
    run_id: str | None = None,
    settings: Any | None = None,
    on_progress: Any | None = None,
    should_stop: Any | None = None,
) -> dict[str, Any]:
    """Draft comment, wait for Fleet popup approval, post immediately on current reel."""
    from ig_agent.config import get_settings
    from ig_agent.filter import load_agency_context
    from ig_agent.persist import create_interaction
    from ig_agent.propose import draft_comment
    from ig_agent.safety import can_perform, record_action
    from ig_agent.scripted_actions import scripted_comment_current

    cfg = settings or get_settings()
    if not cfg.ingest_live_comment_prompt or not can_perform("comment", cfg):
        return post

    agency = load_agency_context()
    draft = draft_comment(post, agency, cfg)
    decision = await wait_for_ingest_comment(
        post,
        draft,
        controller=controller,
        run_id=run_id,
        on_progress=on_progress,
        should_stop=should_stop,
        timeout_s=float(cfg.ingest_comment_timeout_s),
    )
    if decision.get("action") != "approve":
        post["comment_skipped"] = True
        return post

    text = (decision.get("text") or draft).strip()
    if not text:
        post["comment_skipped"] = True
        return post

    try:
        res = await scripted_comment_current(browser, text, settings=cfg)
        post["commented"] = res.ok
        if res.ok:
            create_interaction(
                kind="comment",
                status="done",
                run_id=run_id,
                post_url=post.get("post_url"),
                username=post.get("username"),
                profile_url=post.get("profile_url"),
                auto=False,
                final_text=text,
                draft_text=draft,
                payload={"source": "ingest_live", "executor": "scripted"},
            )
            record_action("comment")
            if on_progress:
                on_progress(f"Comment posted · {(post.get('caption') or '')[:48]}")
    except Exception as exc:
        logger.warning("Ingest live comment failed: %s", exc)
        post["comment_error"] = str(exc)
    return post
