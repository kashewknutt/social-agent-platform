"""Wire Instagram pipeline into the shared BotController."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_sdk.control import BotController
from agent_sdk.models import ArtifactInfo, Direction, RunMode, RunRequest

from ig_agent.config import (
    AGENCY_CONTEXT_PATH,
    DATA_DIR,
    FILTERED_DIR,
    PROJECT_ROOT,
    RAW_DIR,
    REPORTS_DIR,
    get_settings,
)
from ig_agent.engage import execute_auto_interactions
from ig_agent.filter import filter_raw_file, load_agency_context
from ig_agent.ingest import capture_trends_with_delays
from ig_agent.multimodal import analyze_from_filtered_file
from ig_agent.persist import init_db, list_interactions
from ig_agent.posts import normalize_posts
from ig_agent.propose import propose_interactions
from ig_agent.safety import can_start_scroll_session, session_cooldown_seconds, usage_snapshot
from ig_agent.synthesize import synthesize_dashboard

DEFAULT_CONSTRAINTS = (
    "While browsing research, like and follow relevant creator posts live. "
    "Comments/DMs/posts still require human approval (HITL)."
)


def _direction_from_context(ctx: dict[str, Any]) -> Direction:
    return Direction(
        brand_name=ctx.get("brand_name", ""),
        business_type=ctx.get("business_type", ""),
        website=ctx.get("website", ""),
        region=ctx.get("region", ""),
        target_audience=list(ctx.get("target_audience", [])),
        content_pillars=list(ctx.get("content_pillars", [])),
        brand_voice=ctx.get("brand_voice", ""),
        competitor_hashtags=list(ctx.get("competitor_hashtags", [])),
        competitor_profiles=list(ctx.get("competitor_profiles", [])),
        goals=ctx.get("goals", ""),
        constraints=ctx.get("constraints", DEFAULT_CONSTRAINTS),
    )


def load_direction() -> Direction:
    if not AGENCY_CONTEXT_PATH.exists():
        return Direction(constraints=DEFAULT_CONSTRAINTS)
    return _direction_from_context(json.loads(AGENCY_CONTEXT_PATH.read_text(encoding="utf-8")))


def save_direction(direction: Direction) -> None:
    AGENCY_CONTEXT_PATH.write_text(
        json.dumps(direction.model_dump(), indent=2) + "\n",
        encoding="utf-8",
    )


def load_usage() -> dict[str, Any]:
    return usage_snapshot()


def _mtime_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat()


def load_artifacts() -> list[ArtifactInfo]:
    artifacts: list[ArtifactInfo] = []
    raw_files = sorted(
        (p for p in RAW_DIR.glob("scraped_*.json") if "sample" not in p.name.lower()),
        key=lambda p: p.stat().st_mtime,
    )
    if raw_files:
        p = raw_files[-1]
        artifacts.append(ArtifactInfo(kind="raw", path=str(p), modified_at=_mtime_iso(p)))
    filtered = sorted(
        (
            p
            for p in FILTERED_DIR.glob("filtered_*.json")
            if not p.name.endswith("_with_media.json") and "sample" not in p.name.lower()
        ),
        key=lambda p: p.stat().st_mtime,
    )
    if filtered:
        p = filtered[-1]
        artifacts.append(ArtifactInfo(kind="filtered", path=str(p), modified_at=_mtime_iso(p)))
    reports = sorted(REPORTS_DIR.glob("Daily_Social_Dashboard_*.md"), key=lambda p: p.stat().st_mtime)
    if reports:
        p = reports[-1]
        artifacts.append(ArtifactInfo(kind="report", path=str(p), modified_at=_mtime_iso(p)))
    db = DATA_DIR / "interactions.db"
    if db.exists():
        artifacts.append(ArtifactInfo(kind="interactions", path=str(db), modified_at=_mtime_iso(db)))
    return artifacts


def _should_engage(request: RunRequest, settings: Any) -> bool:
    if not getattr(request, "engage", True):
        return False
    if not settings.engage_after_research:
        return False
    return True


def _is_offline_or_sample(request: RunRequest) -> bool:
    return bool(request.sample or request.offline)


async def run_pipeline(controller: BotController, request: RunRequest) -> None:
    settings = get_settings()
    init_db()
    direction = controller.get_direction()
    from ig_agent.hashtag_rotation import pick_hashtags_for_session, prune_history

    prune_history(keep_days=14.0)
    hashtags, hashtag_note = pick_hashtags_for_session(
        direction.competitor_hashtags,
        max_pick=1,
        within_days=settings.hashtag_cooldown_days,
    )

    async def one_pass() -> None:
        await controller.checkpoint()
        controller.set_step("ingest", "Starting Instagram ingestion")

        if request.sample:
            sample_path = RAW_DIR / "sample_scraped.json"
            if not sample_path.exists():
                raise FileNotFoundError(f"Sample file missing: {sample_path}")
            raw_path = sample_path
            sample_posts = json.loads(sample_path.read_text(encoding="utf-8")).get("posts") or []
            controller.set_live(
                {
                    "stage": "ingest",
                    "threshold": settings.relevance_threshold,
                    "caught": len(sample_posts),
                    "kept": 0,
                    "rejected": 0,
                    "posts": sample_posts,
                }
            )
            controller.set_step("ingest", f"Using sample data: {raw_path.name}")
        else:
            if not can_start_scroll_session(settings):
                raise RuntimeError(
                    f"Daily scroll session limit ({settings.max_scroll_sessions_per_day}) reached."
                )
            await controller.checkpoint()
            # Live like/follow happens inside the browse pass (not a later engage step).
            if hashtag_note and not request.sample:
                controller.set_step("ingest", hashtag_note)

            def on_progress(msg: str) -> None:
                controller.set_step("ingest", msg)

            def on_posts(posts: list[dict[str, Any]]) -> None:
                controller.set_live(
                    {
                        "stage": "ingest",
                        "threshold": settings.relevance_threshold,
                        "caught": len(posts),
                        "kept": 0,
                        "rejected": 0,
                        "posts": [
                            {
                                "post_url": p.get("post_url"),
                                "caption": (p.get("caption") or "")[:240]
                                or (f"Caught {p.get('post_url')}" if p.get("post_url") else ""),
                                "likes": p.get("likes"),
                                "views": p.get("views"),
                                "comments_count": p.get("comments_count"),
                                "post_type": p.get("post_type") or "post",
                                "username": p.get("username"),
                                "relevance_score": p.get("relevance_score"),
                                "reason": (
                                    ("liked · " if p.get("liked") else "")
                                    + ("followed · " if p.get("followed") else "")
                                    + (p.get("reason") or "awaiting filter score")
                                ),
                                "kept": p.get("kept"),
                                "liked": p.get("liked"),
                                "followed": p.get("followed"),
                            }
                            for p in posts
                            if p.get("post_url")
                        ],
                    }
                )

            controller.set_live(
                {
                    "stage": "ingest",
                    "threshold": settings.relevance_threshold,
                    "caught": 0,
                    "kept": 0,
                    "rejected": 0,
                    "posts": [],
                }
            )
            engage_live = _should_engage(request, settings) and not _is_offline_or_sample(request)
            raw_path = await capture_trends_with_delays(
                settings,
                hashtags,
                on_progress=on_progress,
                should_stop=lambda: controller._stop.is_set(),
                on_posts=on_posts,
                engage_live=engage_live,
                run_id=controller.run_id,
                controller=controller,
            )
            controller.set_step("ingest", f"Ingested → {raw_path.name}")

        def _post_row(p: dict[str, Any]) -> dict[str, Any]:
            return {
                "post_url": p.get("post_url"),
                "caption": (p.get("caption") or p.get("raw_text") or "")[:240],
                "likes": p.get("likes"),
                "views": p.get("views"),
                "comments_count": p.get("comments_count"),
                "post_type": p.get("post_type"),
                "username": p.get("username"),
                "relevance_score": p.get("relevance_score"),
                "reason": p.get("reason"),
                "kept": bool(p.get("kept", (p.get("relevance_score") or 0) >= settings.relevance_threshold)),
                "adaptable_hook": p.get("adaptable_hook"),
            }

        # Full placeholder row list so the Caught-reels table keeps a stable
        # row count during filtering and scores fill in top-down, instead of
        # the table looking "stuck" with nothing changing until the whole
        # batch finishes.
        try:
            raw_json = json.loads(raw_path.read_text(encoding="utf-8"))
            placeholder_posts = normalize_posts(raw_json.get("posts") or [])
        except Exception:
            placeholder_posts = []

        def on_filter_progress(scored_so_far: list[dict[str, Any]], total: int) -> None:
            kept_so_far = [p for p in scored_so_far if p.get("kept")]
            rows = [_post_row(p) for p in scored_so_far]
            remaining = placeholder_posts[len(scored_so_far) : total]
            rows.extend(
                _post_row({**p, "reason": p.get("reason") or "awaiting filter score"})
                for p in remaining
            )
            controller.set_live(
                {
                    "stage": "filter",
                    "threshold": settings.relevance_threshold,
                    "caught": total,
                    "kept": len(kept_so_far),
                    "rejected": max(0, len(scored_so_far) - len(kept_so_far)),
                    "posts": rows,
                }
            )
            controller.set_step(
                "filter",
                f"Filtering for relevance ({len(scored_so_far)}/{total} scored)",
            )

        await controller.checkpoint()
        controller.set_step("filter", "Filtering for relevance")
        try:
            filtered_path = await asyncio.to_thread(
                filter_raw_file,
                raw_path,
                offline=request.offline,
                on_progress=on_filter_progress,
            )
        except Exception as exc:
            controller.last_error = f"Filter step failed: {exc}"
            controller.set_step("filter", f"Filtering failed ({exc}) — falling back to offline scoring")
            filtered_path = await asyncio.to_thread(
                filter_raw_file,
                raw_path,
                offline=True,
                on_progress=on_filter_progress,
            )
        filtered_data = json.loads(filtered_path.read_text(encoding="utf-8"))
        all_scored = list(filtered_data.get("all_scored") or filtered_data.get("posts") or [])
        filtered_count = int(filtered_data.get("post_count") or 0)
        kept_posts = [p for p in all_scored if p.get("kept")] or list(filtered_data.get("posts") or [])
        controller.set_live(
            {
                "stage": "filter",
                "threshold": filtered_data.get("threshold", settings.relevance_threshold),
                "caught": len(all_scored) or int(filtered_data.get("normalized_input_count") or 0),
                "kept": len(kept_posts),
                "rejected": max(0, len(all_scored) - len(kept_posts)),
                "posts": [_post_row(p) for p in (all_scored or kept_posts)],
            }
        )
        controller.set_step(
            "filter",
            f"Filtered → {filtered_path.name} ({filtered_count} kept / {len(all_scored)} scored)",
        )
        if filtered_count == 0:
            controller.set_step(
                "filter",
                f"Filtered → 0 kept / {len(all_scored)} scored "
                f"(threshold {filtered_data.get('threshold', settings.relevance_threshold)}). "
                "Will still propose HITL from top-scored catches.",
            )
            if not all_scored:
                raise RuntimeError(
                    "Filter kept 0 posts and scored 0. "
                    "Ingest returned nothing usable — try Run research again."
                )

        multimodal_notes = None
        if request.multimodal or settings.enable_multimodal:
            await controller.checkpoint()
            controller.set_step("multimodal", "Running multimodal analysis")
            multimodal_notes = await asyncio.to_thread(
                analyze_from_filtered_file, filtered_path, settings
            )
            controller.set_step("multimodal", f"{len(multimodal_notes)} notes")

        await controller.checkpoint()
        controller.set_step("synthesize", "Synthesizing daily dashboard")
        report = await asyncio.to_thread(
            synthesize_dashboard,
            multimodal_notes=multimodal_notes,
            offline=request.offline,
            filtered_path=filtered_path,
        )
        controller.set_step("synthesize", f"Dashboard → {report.name}")

        # Propose engagement from filtered shortlist (always when engage enabled).
        if _should_engage(request, settings):
            await controller.checkpoint()
            controller.set_step("propose", "Proposing engagement interactions")
            agency = load_agency_context()
            proposed = await asyncio.to_thread(
                propose_interactions,
                run_id=controller.run_id,
                filtered_path=filtered_path,
                agency_context=agency,
                settings=settings,
            )
            auto_count = sum(1 for p in proposed if p.get("auto"))
            hitl_count = len(proposed) - auto_count
            controller.set_step(
                "propose",
                f"Proposed {len(proposed)} interactions ({auto_count} auto, {hitl_count} HITL)",
            )

            # Sample/offline: propose only — NEVER browser-engage.
            if _is_offline_or_sample(request):
                controller.set_step(
                    "engage",
                    "Sample/offline mode — skipped browser engagement (HITL left proposed)",
                )
            else:
                # Likes/follows already attempted live during browse ingest.
                # Only backfill any remaining auto likes/follows that were proposed
                # but not marked done (e.g. agent forgot to set liked/followed flags).
                await controller.checkpoint()
                controller.set_step("engage", "Backfilling any missed auto likes/follows")
                results = await execute_auto_interactions(
                    run_id=controller.run_id,
                    settings=settings,
                    checkpoint=controller.checkpoint,
                    dry_run=False,
                )
                done = sum(1 for r in results if r.get("status") == "done")
                failed = sum(1 for r in results if r.get("status") == "failed")
                pending_hitl = list_interactions(
                    run_id=controller.run_id, status="proposed", limit=500
                )
                hitl_left = sum(1 for r in pending_hitl if not r.get("auto"))
                live_done = list_interactions(run_id=controller.run_id, status="done", limit=500)
                live_likes = sum(
                    1
                    for r in live_done
                    if r.get("kind") == "like"
                    and (r.get("payload") or {}).get("source") == "ingest_live"
                )
                live_follows = sum(
                    1
                    for r in live_done
                    if r.get("kind") == "follow"
                    and (r.get("payload") or {}).get("source") == "ingest_live"
                )
                controller.set_step(
                    "engage",
                    f"Live liked={live_likes} followed={live_follows}; "
                    f"backfill done={done} failed={failed}; {hitl_left} HITL awaiting approval",
                )

    if request.mode == RunMode.ONCE:
        await one_pass()
        return

    # Daemon mode with cooperative pause/stop between sessions.
    session_timeout = settings.session_max_minutes * 60
    while not controller._stop.is_set():
        await controller.checkpoint()
        if not can_start_scroll_session(settings) and not request.sample:
            controller.set_step("cooldown", "Daily session limit reached — sleeping 8h")
            for _ in range(8 * 3600):
                if controller._stop.is_set():
                    return
                await controller.checkpoint()
                await asyncio.sleep(1)
            continue

        try:
            await asyncio.wait_for(one_pass(), timeout=session_timeout)
        except asyncio.TimeoutError:
            controller.set_step("timeout", "Session exceeded time cap")
        except Exception as exc:
            controller.set_step("error", str(exc))
            controller.last_error = str(exc)

        wait = session_cooldown_seconds()
        controller.set_step("cooldown", f"Sleeping {wait // 3600}h until next session")
        for _ in range(wait):
            if controller._stop.is_set():
                return
            await controller.checkpoint()
            await asyncio.sleep(1)


def build_controller() -> BotController:
    get_settings()  # ensure dirs
    init_db()
    return BotController(
        bot_id="instagram",
        name="Instagram Trend Bot",
        network="instagram",
        root=PROJECT_ROOT,
        pipeline=run_pipeline,
        load_direction=load_direction,
        save_direction=save_direction,
        load_usage=load_usage,
        load_artifacts=load_artifacts,
    )
