"""Browser-use executor for Instagram engagement actions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ig_agent.config import Settings, get_settings
from ig_agent.llm import get_browser_llm
from ig_agent.persist import (
    get_interaction,
    list_interactions,
    mark_done,
    mark_executing,
    mark_failed,
    update_interaction,
)
from ig_agent.safety import async_engagement_delay, can_perform, record_action

CheckpointFn = Callable[[], Awaitable[None]]


def _task_for(interaction: dict[str, Any]) -> str:
    kind = interaction["kind"]
    post_url = interaction.get("post_url") or ""
    profile_url = interaction.get("profile_url") or ""
    username = interaction.get("username") or ""
    text = (interaction.get("final_text") or interaction.get("draft_text") or "").strip()

    if kind == "like":
        return (
            f"Go to {post_url} on Instagram. "
            f"If a login wall appears, stop and report that login is required. "
            f"Like the post (tap the heart). Do not comment, follow, or send a DM. "
            f"Confirm the like succeeded or report why it failed."
        )
    if kind == "follow":
        target = profile_url or (f"https://www.instagram.com/{username}/" if username else post_url)
        return (
            f"Go to {target} on Instagram. "
            f"If a login wall appears, stop and report that login is required. "
            f"Follow the account if not already following. Do not like posts or comment. "
            f"Confirm follow succeeded or report the current follow state."
        )
    if kind == "comment":
        return (
            f"Go to {post_url} on Instagram. "
            f"If a login wall appears, stop and report that login is required. "
            f"Open the comment box and post exactly this comment (no edits):\n{text}\n"
            f"Do not like or follow. Confirm the comment was posted."
        )
    if kind == "dm":
        target = profile_url or (f"https://www.instagram.com/{username}/" if username else "")
        return (
            f"Go to {target} on Instagram. "
            f"If a login wall appears, stop and report that login is required. "
            f"Open the message/DM thread with @{username} and send exactly this message:\n{text}\n"
            f"Do not follow or comment on posts. Confirm the DM was sent."
        )
    if kind == "post":
        return (
            f"Go to https://www.instagram.com/ on Instagram. "
            f"If a login wall appears, stop and report that login is required. "
            f"Create a new post (image optional — use a simple solid-color placeholder if needed) "
            f"with this caption exactly:\n{text}\n"
            f"Publish the post. Confirm success and return the new post URL if visible."
        )
    raise ValueError(f"Unsupported interaction kind: {kind}")


async def _run_browser_task(task: str, settings: Settings) -> str:
    """Execute one engagement task with the persistent browser profile."""
    from browser_use import Agent, BrowserSession

    llm = get_browser_llm(settings)
    browser = BrowserSession(
        headless=False,
        user_data_dir=str(settings.browser_user_data_dir),
    )
    try:
        agent = Agent(task=task, llm=llm, browser=browser)
        history = await agent.run()
        if hasattr(history, "final_result"):
            result = history.final_result()
            return str(result) if result is not None else str(history)
        return str(history)
    finally:
        await browser.kill()


async def execute_interaction(
    interaction_id: str,
    *,
    settings: Settings | None = None,
    checkpoint: CheckpointFn | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Execute a single interaction via browser-use.

    dry_run=True skips the browser (used for offline/sample safeguards and tests).
    """
    cfg = settings or get_settings()
    row = get_interaction(interaction_id)
    if row is None:
        raise KeyError(f"Unknown interaction {interaction_id}")

    kind = row["kind"]
    status = row["status"]
    if status in {"done", "rejected", "skipped"}:
        return row
    if status == "proposed" and not row.get("auto"):
        raise RuntimeError("HITL interaction must be approved before execute")
    if status not in {"proposed", "approved", "failed"}:
        if status != "executing":
            raise RuntimeError(f"Cannot execute from status {status}")

    if not can_perform(kind, cfg):
        failed = mark_failed(interaction_id, f"Daily {kind} cap reached")
        assert failed is not None
        return failed

    if checkpoint:
        await checkpoint()

    mark_executing(interaction_id)
    try:
        if dry_run:
            result_text = f"dry_run:{kind}"
        else:
            task = _task_for(row)
            result_text = await _run_browser_task(task, cfg)

        if checkpoint:
            await checkpoint()

        payload = dict(row.get("payload") or {})
        payload["browser_result"] = result_text[:4000]
        update_interaction(interaction_id, payload=payload)
        record_action(kind)
        done = mark_done(interaction_id)
        assert done is not None
        return done
    except asyncio.CancelledError:
        mark_failed(interaction_id, "cancelled")
        raise
    except Exception as exc:
        failed = mark_failed(interaction_id, str(exc))
        assert failed is not None
        return failed


async def execute_auto_interactions(
    *,
    run_id: str | None = None,
    settings: Settings | None = None,
    checkpoint: CheckpointFn | None = None,
    dry_run: bool = False,
    kinds: tuple[str, ...] = ("like", "follow"),
) -> list[dict[str, Any]]:
    """Execute proposed auto interactions (likes/follows) with humanized delays."""
    cfg = settings or get_settings()
    rows = list_interactions(run_id=run_id, status="proposed", limit=500)
    auto_rows = [r for r in rows if r.get("auto") and r.get("kind") in kinds]
    # Deterministic order: likes first, then follows.
    auto_rows.sort(key=lambda r: (0 if r["kind"] == "like" else 1, r.get("created_at") or ""))

    results: list[dict[str, Any]] = []
    for row in auto_rows:
        if checkpoint:
            await checkpoint()
        if not can_perform(row["kind"], cfg):
            mark_failed(row["id"], f"Daily {row['kind']} cap reached")
            continue
        result = await execute_interaction(
            row["id"],
            settings=cfg,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
        results.append(result)
        if result.get("status") == "done":
            await async_engagement_delay(row["kind"])
    return results


async def execute_approved_interactions(
    *,
    run_id: str | None = None,
    settings: Settings | None = None,
    checkpoint: CheckpointFn | None = None,
    dry_run: bool = False,
    interaction_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Execute HITL interactions that have been approved."""
    cfg = settings or get_settings()
    if interaction_ids:
        rows = [get_interaction(i) for i in interaction_ids]
        rows = [r for r in rows if r is not None]
    else:
        rows = list_interactions(run_id=run_id, status="approved", limit=200)

    results: list[dict[str, Any]] = []
    for row in rows:
        if checkpoint:
            await checkpoint()
        result = await execute_interaction(
            row["id"],
            settings=cfg,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
        results.append(result)
        if result.get("status") == "done":
            await async_engagement_delay(row["kind"])
    return results
