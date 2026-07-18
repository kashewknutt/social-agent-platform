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
from ig_agent.propose import propose_interactions
from ig_agent.safety import can_start_scroll_session, session_cooldown_seconds, usage_snapshot
from ig_agent.synthesize import synthesize_dashboard

DEFAULT_CONSTRAINTS = (
    "Ingest is observation-only. Engagement (like/follow auto; "
    "comment/DM/post after human approval) runs in a separate browser pass."
)


def _direction_from_context(ctx: dict[str, Any]) -> Direction:
    return Direction(
        brand_name=ctx.get("brand_name", ""),
        business_type=ctx.get("business_type", ""),
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
    raw_files = sorted(RAW_DIR.glob("scraped_*.json"), key=lambda p: p.stat().st_mtime)
    if raw_files:
        p = raw_files[-1]
        artifacts.append(ArtifactInfo(kind="raw", path=str(p), modified_at=_mtime_iso(p)))
    filtered = sorted(FILTERED_DIR.glob("filtered_*.json"), key=lambda p: p.stat().st_mtime)
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
    hashtags = direction.competitor_hashtags

    async def one_pass() -> None:
        await controller.checkpoint()
        controller.set_step("ingest", "Starting Instagram ingestion")

        if request.sample:
            sample_path = RAW_DIR / "sample_scraped.json"
            if not sample_path.exists():
                raise FileNotFoundError(f"Sample file missing: {sample_path}")
            raw_path = sample_path
            controller.set_step("ingest", f"Using sample data: {raw_path.name}")
        else:
            if not can_start_scroll_session(settings):
                raise RuntimeError(
                    f"Daily scroll session limit ({settings.max_scroll_sessions_per_day}) reached."
                )
            await controller.checkpoint()
            # Ingest stays observation-only — never like/follow/comment here.
            raw_path = await capture_trends_with_delays(settings, hashtags)
            controller.set_step("ingest", f"Ingested → {raw_path.name}")

        await controller.checkpoint()
        controller.set_step("filter", "Filtering for relevance")
        filtered_path = filter_raw_file(raw_path, offline=request.offline)
        controller.set_step("filter", f"Filtered → {filtered_path.name}")

        multimodal_notes = None
        if request.multimodal or settings.enable_multimodal:
            await controller.checkpoint()
            controller.set_step("multimodal", "Running multimodal analysis")
            multimodal_notes = analyze_from_filtered_file(filtered_path, settings)
            controller.set_step("multimodal", f"{len(multimodal_notes)} notes")

        await controller.checkpoint()
        controller.set_step("synthesize", "Synthesizing daily dashboard")
        report = synthesize_dashboard(
            multimodal_notes=multimodal_notes,
            offline=request.offline,
        )
        controller.set_step("synthesize", f"Dashboard → {report.name}")

        # Propose engagement from filtered shortlist (always when engage enabled).
        if _should_engage(request, settings):
            await controller.checkpoint()
            controller.set_step("propose", "Proposing engagement interactions")
            agency = load_agency_context()
            proposed = propose_interactions(
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
                await controller.checkpoint()
                controller.set_step("engage", "Executing auto likes/follows")
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
                controller.set_step(
                    "engage",
                    f"Auto done={done} failed={failed}; {hitl_left} HITL awaiting approval",
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
