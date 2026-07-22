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

_CHALLENGE_MARKERS = (
    "login is required",
    "hit a login wall",
    "saw a login wall",
    "login wall appears",
    "encountered a login wall",
    "checkpoint required",
    "security checkpoint",
    "suspicious activity",
    "try again later",
    "rate limit",
    "too many requests",
    "too many actions",
    "confirm it's you",
    "confirm it is you",
    "temporarily locked",
    "we restrict",
    "action blocked",
    "account locked",
)

# Agent success reports often say "without encountering a login wall" — that must
# never trip the throttle circuit.
_SUCCESS_MARKERS = (
    "successfully completed",
    "posted the exact comment",
    "comment was posted",
    "comment posted",
    "dm was sent",
    "message was sent",
    "message sent",
    "like succeeded",
    "follow succeeded",
    "already following",
)

_NEGATED_THROTTLE_PHRASES = (
    "without encountering a login wall",
    "without a login wall",
    "no login wall",
    "not a login wall",
    "didn't hit a login wall",
    "did not hit a login wall",
    "no login required",
    "login not required",
)


def _looks_like_ig_throttle(text: str) -> bool:
    low = (text or "").lower()
    if any(s in low for s in _SUCCESS_MARKERS):
        return False
    cleaned = low
    for phrase in _NEGATED_THROTTLE_PHRASES:
        cleaned = cleaned.replace(phrase, " ")
    return any(m in cleaned for m in _CHALLENGE_MARKERS)


def _circuit_file(settings: Settings):
    from pathlib import Path

    return Path(settings.browser_user_data_dir).parent / "engage_circuit.json"


def _read_circuit(settings: Settings) -> dict[str, Any]:
    import json

    path = _circuit_file(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_circuit(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    import json

    path = _circuit_file(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def active_profile_slot(settings: Settings) -> int:
    data = _read_circuit(settings)
    try:
        slot = int(data.get("profile_slot") or 0)
    except (TypeError, ValueError):
        slot = 0
    slots = max(1, int(settings.engage_profile_slots))
    return max(0, min(slot, slots - 1))


def active_profile_dir(settings: Settings):
    return settings.profile_dir_for_slot(active_profile_slot(settings))


def circuit_status(settings: Settings | None = None) -> dict[str, Any]:
    """Public status for Fleet / API."""
    from datetime import datetime

    cfg = settings or get_settings()
    data = _read_circuit(cfg)
    slot = active_profile_slot(cfg)
    profile = str(active_profile_dir(cfg))
    until_raw = data.get("until")
    open_now = False
    remaining_sec = 0
    if until_raw:
        try:
            until = datetime.fromisoformat(str(until_raw))
            remaining_sec = max(0, int((until - datetime.now()).total_seconds()))
            open_now = remaining_sec > 0
        except Exception:
            open_now = False
    return {
        "open": open_now,
        "remaining_seconds": remaining_sec,
        "until": until_raw,
        "reason": data.get("reason"),
        "strikes": int(data.get("strikes") or 0),
        "profile_slot": slot,
        "profile_dir": profile,
        "profile_slots": cfg.engage_profile_slots,
        "pause_minutes_default": cfg.engage_circuit_minutes,
        "pause_minutes_max": cfg.engage_circuit_max_minutes,
    }


def clear_circuit(
    settings: Settings | None = None,
    *,
    rotate_profile: bool = False,
) -> dict[str, Any]:
    """Clear soft pause; optionally rotate to the next browser profile slot."""
    from datetime import datetime

    cfg = settings or get_settings()
    data = _read_circuit(cfg)
    slot = active_profile_slot(cfg)
    if rotate_profile and cfg.engage_profile_slots > 1:
        slot = (slot + 1) % cfg.engage_profile_slots
    payload = {
        "cleared_at": datetime.now().isoformat(),
        "until": None,
        "reason": None,
        "strikes": 0,
        "profile_slot": slot,
        "last_trip": data.get("tripped_at"),
    }
    _write_circuit(cfg, payload)
    return circuit_status(cfg)


def _trip_circuit(settings: Settings, reason: str) -> dict[str, Any]:
    """Short escalating pause (default 15m → 30m → max 45m), not multi-hour."""
    from datetime import datetime, timedelta

    data = _read_circuit(settings)
    strikes = int(data.get("strikes") or 0) + 1
    base = max(5, int(settings.engage_circuit_minutes))
    cap = max(base, int(settings.engage_circuit_max_minutes))
    # 15, 30, 45… capped
    minutes = min(cap, base * strikes)
    until = datetime.now() + timedelta(minutes=minutes)
    payload = {
        "tripped_at": datetime.now().isoformat(),
        "until": until.isoformat(),
        "reason": reason[:500],
        "strikes": strikes,
        "pause_minutes": minutes,
        "profile_slot": active_profile_slot(settings),
    }
    return _write_circuit(settings, payload)


def _circuit_blocked(settings: Settings) -> str | None:
    from datetime import datetime

    data = _read_circuit(settings)
    until_raw = data.get("until")
    if not until_raw:
        return None
    try:
        until = datetime.fromisoformat(str(until_raw))
    except Exception:
        return None
    if datetime.now() >= until:
        return None
    mins = max(1, int((until - datetime.now()).total_seconds() // 60) + 1)
    reason = str(data.get("reason") or "Instagram throttle")
    return f"{reason} (resume in ~{mins}m, or clear circuit)"


def _task_for(interaction: dict[str, Any]) -> str:
    kind = interaction["kind"]
    post_url = interaction.get("post_url") or ""
    profile_url = interaction.get("profile_url") or ""
    username = interaction.get("username") or ""
    text = (interaction.get("final_text") or interaction.get("draft_text") or "").strip()
    speed = (
        "Work as fast as possible. Do not scroll the feed, do not explore related posts, "
        "do not wait around. Finish in the fewest steps. "
    )

    if kind == "like":
        return (
            f"{speed}"
            f"Go to {post_url}. If a login wall appears, stop and say login is required. "
            f"Like the post (heart). Do not comment, follow, or DM. Then done."
        )
    if kind == "follow":
        target = profile_url or (f"https://www.instagram.com/{username}/" if username else post_url)
        return (
            f"{speed}"
            f"Go to {target}. If a login wall appears, stop and say login is required. "
            f"Follow if not already following. Do not like or comment. Then done."
        )
    if kind == "comment":
        return (
            f"{speed}"
            f"Go directly to {post_url}. If a login wall appears, stop and say login is required. "
            f"Open the comment box immediately and post exactly this text (no edits):\n{text}\n"
            f"Submit. Do not like or follow. Confirm posted, then done."
        )
    if kind == "dm":
        if profile_url:
            target = profile_url
        elif username:
            target = f"https://www.instagram.com/{username}/"
        elif post_url:
            target = post_url
        else:
            target = "https://www.instagram.com/"
        who = f"@{username}" if username else "the post author"
        via_post = (
            f"From the post, open the author's profile, then Message. "
            if (not profile_url and not username and post_url)
            else ""
        )
        return (
            f"{speed}"
            f"Go to {target}. {via_post}"
            f"If a login wall appears, stop and say login is required. "
            f"Open DM with {who} and send exactly:\n{text}\n"
            f"Do not follow or comment. Confirm sent, then done."
        )
    if kind == "post":
        return (
            f"{speed}"
            f"Go to https://www.instagram.com/. If a login wall appears, stop and say login is required. "
            f"Create a new post with this caption exactly:\n{text}\n"
            f"Publish. Confirm success and return the new post URL if visible."
        )
    raise ValueError(f"Unsupported interaction kind: {kind}")


def _is_browser_start_timeout(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "browserstartevent" in msg or (
        "timed out" in msg and "browser" in msg and "start" in msg
    )


async def _run_scripted_for_row(
    row: dict[str, Any],
    browser: Any,
    settings: Settings,
) -> Any:
    """Try scripted automation; return ActionResult or None if kind unsupported."""
    from ig_agent.scripted_actions import (
        ScriptedActionError,
        scripted_comment,
        scripted_dm,
        scripted_follow,
        scripted_like,
    )

    kind = row["kind"]
    text = (row.get("final_text") or row.get("draft_text") or "").strip()
    post_url = row.get("post_url") or ""
    profile_url = row.get("profile_url") or ""
    username = row.get("username") or ""

    try:
        if kind == "like":
            if not post_url:
                raise ScriptedActionError("selector_not_found", "Missing post_url")
            return await scripted_like(browser, post_url, settings=settings)
        if kind == "follow":
            return await scripted_follow(
                browser,
                profile_url=profile_url or None,
                username=username or None,
                settings=settings,
            )
        if kind == "comment":
            if not post_url:
                raise ScriptedActionError("selector_not_found", "Missing post_url")
            return await scripted_comment(browser, post_url, text, settings=settings)
        if kind == "dm":
            return await scripted_dm(
                browser,
                text,
                profile_url=profile_url or None,
                username=username or None,
                settings=settings,
            )
    except ScriptedActionError:
        raise
    return None


async def _execute_with_scripted_or_llm(
    row: dict[str, Any],
    settings: Settings,
    *,
    browser: Any | None = None,
    max_steps: int = 12,
) -> tuple[str, str, str | None]:
    """Returns (result_text, executor, fallback_reason)."""
    from ig_agent.scripted_actions import (
        FALLBACK_NOTE,
        ScriptedActionError,
        record_scripted_fallback,
    )

    kind = row["kind"]
    cfg = settings

    if cfg.use_scripted_engagement and kind in {"like", "follow", "comment", "dm"}:
        if browser is not None and hasattr(browser, "start"):
            try:
                await browser.start()
            except Exception:
                pass
        try:
            result = await _run_scripted_for_row(row, browser, cfg)
            if result is not None:
                return result.detail, "scripted", None
        except ScriptedActionError as exc:
            if exc.reason in {"login_wall", "checkpoint"}:
                raise RuntimeError(exc.detail) from exc
            record_scripted_fallback(kind, exc.reason)
            task = _task_for(row)
            llm_text = await _run_browser_task(task, cfg, browser=browser, max_steps=max_steps)
            return (
                f"{llm_text}\n{FALLBACK_NOTE} ({exc.reason}: {exc.detail})",
                "llm_fallback",
                exc.reason,
            )

    task = _task_for(row)
    llm_text = await _run_browser_task(task, cfg, browser=browser, max_steps=max_steps)
    return llm_text, "llm", None


async def _run_browser_task(
    task: str,
    settings: Settings,
    *,
    browser: Any | None = None,
    max_steps: int = 12,
) -> str:
    """Execute one engagement task with the active browser profile slot.

    When ``browser`` is provided (shared batch session), it must already be
    started with ``keep_alive=True``. Otherwise a fresh session is created,
    prepared (locks cleared), and killed after the run.
    """
    from browser_use import Agent

    from ig_agent.browser_factory import make_browser_session, safe_kill

    llm = get_browser_llm(settings)
    owns_browser = browser is None
    if owns_browser:
        profile = active_profile_dir(settings)
        browser = make_browser_session(settings, user_data_dir=profile, headless=False)

    last_exc: BaseException | None = None
    try:
        attempts = 2 if owns_browser else 1
        for attempt in range(1, attempts + 1):
            try:
                # flash_mode + no vision: fewer tokens / faster steps for simple IG actions
                agent = Agent(
                    task=task,
                    llm=llm,
                    browser=browser,
                    flash_mode=True,
                    use_vision=False,
                    max_actions_per_step=4,
                )
                history = await agent.run(max_steps=max_steps)
                if hasattr(history, "final_result"):
                    result = history.final_result()
                    return str(result) if result is not None else str(history)
                return str(history)
            except BaseException as exc:
                last_exc = exc
                if owns_browser and attempt < attempts and _is_browser_start_timeout(exc):
                    # Orphan Chrome often left holding the profile after a start timeout.
                    await safe_kill(browser)
                    browser = make_browser_session(
                        settings,
                        user_data_dir=active_profile_dir(settings),
                        headless=False,
                    )
                    continue
                raise
        assert last_exc is not None
        raise last_exc
    finally:
        if owns_browser:
            await safe_kill(browser)


async def execute_interaction(
    interaction_id: str,
    *,
    settings: Settings | None = None,
    checkpoint: CheckpointFn | None = None,
    dry_run: bool = False,
    browser: Any | None = None,
) -> dict[str, Any]:
    """
    Execute a single interaction via browser-use.

    dry_run=True skips the browser (used for offline/sample safeguards and tests).
    Pass ``browser`` to reuse an already-started shared session (batch execute).
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

    blocked = _circuit_blocked(cfg)
    if blocked and not dry_run:
        failed = mark_failed(
            interaction_id,
            f"Paused briefly to avoid Instagram flags: {blocked}. "
            f"Use Resume engage (or wait).",
        )
        assert failed is not None
        return failed

    if checkpoint:
        await checkpoint()

    mark_executing(interaction_id)
    try:
        if dry_run:
            result_text = f"dry_run:{kind}"
        else:
            # Tiny settle only — long humanized waits made HITL feel frozen.
            await async_engagement_delay(kind, scale=0.35)
            max_steps = 14 if kind in {"comment", "dm", "post"} else 8
            result_text, executor, fallback_reason = await _execute_with_scripted_or_llm(
                row, cfg, browser=browser, max_steps=max_steps
            )
            if _looks_like_ig_throttle(result_text):
                _trip_circuit(cfg, result_text)
                failed = mark_failed(
                    interaction_id,
                    "Instagram challenged/throttled this action — short pause applied "
                    f"({circuit_status(cfg).get('remaining_seconds', 0)}s left, or Resume engage). "
                    f"Detail: {result_text[:240]}",
                )
                assert failed is not None
                return failed

        if checkpoint:
            await checkpoint()

        payload = dict(row.get("payload") or {})
        payload["browser_result"] = result_text[:4000]
        if not dry_run:
            payload["executor"] = executor
            if fallback_reason:
                payload["fallback_reason"] = fallback_reason
        update_interaction(interaction_id, payload=payload)
        record_action(kind)
        done = mark_done(interaction_id)
        assert done is not None
        return done
    except asyncio.CancelledError:
        mark_failed(interaction_id, "cancelled")
        raise
    except Exception as exc:
        if _looks_like_ig_throttle(str(exc)):
            _trip_circuit(cfg, str(exc))
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
        blocked = _circuit_blocked(cfg)
        if blocked and not dry_run:
            mark_failed(
                row["id"],
                f"Skipped — Instagram throttle circuit open: {blocked}",
            )
            continue
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
        # Stop the batch early if Instagram challenged us
        if result.get("status") == "failed" and _looks_like_ig_throttle(
            str(result.get("error") or "")
        ):
            break
        if not dry_run:
            await async_engagement_delay(row["kind"], scale=0.5)
    return results


async def execute_approved_interactions(
    *,
    run_id: str | None = None,
    settings: Settings | None = None,
    checkpoint: CheckpointFn | None = None,
    dry_run: bool = False,
    interaction_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Execute HITL interactions that have been approved.

    Opens one Chrome window for the whole batch and reuses its CDP endpoint
    so we don't pay BrowserStart (and SingletonLock) costs per DM/comment.
    """
    from browser_use import BrowserSession

    from ig_agent.browser_factory import make_browser_session, safe_kill

    cfg = settings or get_settings()
    if interaction_ids:
        rows = [get_interaction(i) for i in interaction_ids]
        rows = [r for r in rows if r is not None]
    else:
        rows = list_interactions(run_id=run_id, status="approved", limit=200)

    if not rows:
        return []

    owner = None
    cdp_url: str | None = None
    if not dry_run:
        owner = make_browser_session(
            cfg,
            user_data_dir=active_profile_dir(cfg),
            headless=False,
            keep_alive=True,
        )
        try:
            await owner.start()
            cdp_url = owner.cdp_url
            if not cdp_url:
                raise RuntimeError("Browser started but CDP URL was empty")
        except Exception:
            await safe_kill(owner)
            owner = None
            raise

    results: list[dict[str, Any]] = []
    try:
        for row in rows:
            if checkpoint:
                await checkpoint()
            blocked = _circuit_blocked(cfg)
            if blocked and not dry_run:
                mark_failed(
                    row["id"],
                    f"Skipped — Instagram throttle circuit open: {blocked}",
                )
                continue

            shared = None
            if cdp_url and not dry_run:
                # Connect to the already-running Chrome; do not launch another.
                shared = BrowserSession(
                    cdp_url=cdp_url,
                    is_local=False,
                    keep_alive=True,
                )
                await shared.start()

            result = await execute_interaction(
                row["id"],
                settings=cfg,
                checkpoint=checkpoint,
                dry_run=dry_run,
                browser=shared,
            )
            results.append(result)
            if result.get("status") == "failed" and _looks_like_ig_throttle(
                str(result.get("error") or "")
            ):
                break
            if result.get("status") == "done":
                await async_engagement_delay(row["kind"], scale=0.5)
        return results
    finally:
        if owner is not None:
            await safe_kill(owner)
