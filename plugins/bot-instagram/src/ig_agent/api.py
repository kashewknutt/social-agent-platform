"""HTTP control API for the Instagram bot."""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from agent_sdk.api import create_control_app
from agent_sdk.models import BotState

from ig_agent.engage import (
    circuit_status,
    clear_circuit,
    execute_approved_interactions,
    execute_interaction,
)
from ig_agent.scripted_actions import scripted_health_snapshot
from ig_agent.config import FILTERED_DIR, RAW_DIR, REPORTS_DIR, get_settings
from ig_agent.persist import (
    approve_interaction,
    get_interaction,
    list_interactions,
    reject_interaction,
    update_interaction,
)
from ig_agent.propose import propose_interactions
from ig_agent.runtime import build_controller
from ig_agent.safety import usage_snapshot

controller = build_controller()
app = create_control_app(controller, title="Instagram Bot Control API")


def _file_outputs(run_id: str | None = None) -> dict[str, Any]:
    """Build an Outputs payload from latest on-disk live artifacts (never sample_*)."""
    import json
    from datetime import datetime

    def _live(paths: list) -> list:
        return [p for p in paths if "sample" not in p.name.lower()]

    raw_files = _live(sorted(RAW_DIR.glob("scraped_*.json"), key=lambda p: p.stat().st_mtime))
    filtered_files = _live(
        sorted(
            (
                p
                for p in FILTERED_DIR.glob("filtered_*.json")
                if not p.name.endswith("_with_media.json")
            ),
            key=lambda p: p.stat().st_mtime,
        )
    )
    reports = sorted(REPORTS_DIR.glob("Daily_Social_Dashboard_*.md"), key=lambda p: p.stat().st_mtime)

    raw_path = raw_files[-1] if raw_files else None
    filtered_path = filtered_files[-1] if filtered_files else None
    # `stale_filtered` = the filtered file we're showing belongs to an OLDER
    # raw scrape, not the latest one — i.e. the latest scrape hasn't finished
    # filtering yet (or filtering failed). Callers should not present this as
    # "the score for what you just ingested".
    stale_filtered = False
    if raw_path is not None:
        match = FILTERED_DIR / f"filtered_{raw_path.stem}.json"
        if match.exists() and "sample" not in match.name.lower():
            filtered_path = match
        else:
            stale_filtered = filtered_path is not None
    report_path = reports[-1] if reports else None

    raw_posts: list[dict[str, Any]] = []
    filtered_posts: list[dict[str, Any]] = []
    all_scored: list[dict[str, Any]] = []
    threshold = None
    if raw_path and raw_path.exists():
        try:
            raw_posts = list(json.loads(raw_path.read_text(encoding="utf-8")).get("posts") or [])
        except Exception:
            raw_posts = []
    if filtered_path and filtered_path.exists():
        try:
            fdata = json.loads(filtered_path.read_text(encoding="utf-8"))
            filtered_posts = list(fdata.get("posts") or [])
            all_scored = list(fdata.get("all_scored") or filtered_posts)
            threshold = fdata.get("threshold")
        except Exception:
            filtered_posts = []

    report = None
    if report_path and report_path.exists():
        body = report_path.read_text(encoding="utf-8")
        if "Apex Software" not in body:
            report = {
                "path": str(report_path),
                "generated_at": datetime.fromtimestamp(report_path.stat().st_mtime).isoformat(),
                "body": body,
            }

    bot_running = getattr(controller, "state", None) == BotState.RUNNING
    if stale_filtered and bot_running:
        status = "filtering"
    elif stale_filtered:
        status = "stale"
    elif filtered_path:
        status = "completed"
    else:
        status = "partial"

    run = None
    if filtered_path or report_path or raw_path:
        run = {
            "run_id": run_id or (raw_path.stem if (stale_filtered and raw_path) else (filtered_path.stem if filtered_path else (raw_path.stem if raw_path else "latest"))),
            "status": status,
            "stale_filtered": stale_filtered,
            "started_at": None,
            "finished_at": report["generated_at"] if report else None,
            "raw_path": str(raw_path) if raw_path else None,
            "filtered_path": str(filtered_path) if filtered_path else None,
            "report_path": str(report_path) if report_path else None,
            "threshold": threshold,
        }

    # Prefer in-memory live catch when a run is active
    live = getattr(controller, "live", None)

    return {
        "ok": True,
        "run": run,
        "runs": [run] if run else [],
        "live": live,
        "posts": {
            "raw": raw_posts,
            "filtered": filtered_posts,
            "all_scored": all_scored,
            "media": [],
        },
        "notes": [],
        "report": report,
    }


class InteractionUpdate(BaseModel):
    draft_text: str | None = None
    final_text: str | None = None


class ApproveBody(BaseModel):
    final_text: str | None = None


class RejectBody(BaseModel):
    reason: str | None = None


class ProposeBody(BaseModel):
    run_id: str | None = None
    include_post: bool = True


class ExecuteBody(BaseModel):
    dry_run: bool = False
    ids: list[str] = Field(default_factory=list)


@app.get("/outputs")
def outputs(run_id: str | None = None) -> dict[str, Any]:
    """Latest pipeline artifacts for the Fleet Outputs panel."""
    return _file_outputs(run_id)


@app.get("/live")
def live_catch() -> dict[str, Any]:
    """Live caught/scored posts for the Fleet Caught reels panel."""
    live = getattr(controller, "live", None)
    if live:
        return {"ok": True, **live}
    # No in-memory live state (fresh boot / idle). Fall back to the latest
    # on-disk scored file, but be explicit that it may be stale — never
    # silently present an unrelated older run's scores as "current".
    out = _file_outputs()
    run = out.get("run") or {}
    stale = bool(run.get("stale_filtered"))
    threshold = run.get("threshold") or get_settings().relevance_threshold
    scored = out.get("posts", {}).get("all_scored") or out.get("posts", {}).get("filtered") or []
    kept = [p for p in scored if p.get("kept") or (p.get("relevance_score") or 0) >= threshold]
    bot_state = getattr(controller, "state", None)
    stage = "idle"
    if bot_state == BotState.RUNNING:
        stage = str(getattr(controller, "current_step", None) or "running")
    elif stale:
        stage = "stale"
    return {
        "ok": True,
        "stage": stage,
        "stale": stale,
        "threshold": threshold,
        "caught": len(scored) or len(out.get("posts", {}).get("raw") or []),
        "kept": len(kept),
        "rejected": max(0, len(scored) - len(kept)),
        "posts": [] if stale and not scored else (scored or out.get("posts", {}).get("raw") or []),
    }


@app.get("/engage/circuit")
def get_engage_circuit() -> dict[str, Any]:
    return {"ok": True, **circuit_status()}


@app.get("/engage/scripted-health")
def get_scripted_health() -> dict[str, Any]:
    return {"ok": True, **scripted_health_snapshot()}


class ClearCircuitBody(BaseModel):
    rotate_profile: bool = False


@app.post("/engage/circuit/clear")
def post_clear_engage_circuit(body: ClearCircuitBody | None = None) -> dict[str, Any]:
    body = body or ClearCircuitBody()
    status = clear_circuit(rotate_profile=body.rotate_profile)
    return {
        "ok": True,
        "message": (
            "Engage pause cleared"
            + (" and browser profile rotated — log into IG in the new window if asked" if body.rotate_profile else "")
        ),
        **status,
    }


@app.delete("/engage/circuit")
def delete_engage_circuit() -> dict[str, Any]:
    status = clear_circuit(rotate_profile=False)
    return {"ok": True, "message": "Engage pause cleared", **status}


@app.get("/interactions")
def get_interactions(
    run_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    items = list_interactions(run_id=run_id, status=status, kind=kind, limit=limit)
    return {
        "interactions": items,
        "usage": usage_snapshot(),
        "circuit": circuit_status(),
        "scripted_health": scripted_health_snapshot(),
    }


@app.post("/interactions/propose")
def post_propose(body: ProposeBody | None = None) -> dict[str, Any]:
    body = body or ProposeBody()
    created = propose_interactions(
        run_id=body.run_id or controller.run_id,
        include_post=body.include_post,
    )
    return {"created": created, "count": len(created)}


@app.post("/interactions/execute-approved")
async def post_execute_approved(body: ExecuteBody | None = None) -> dict[str, Any]:
    body = body or ExecuteBody()
    results = await execute_approved_interactions(
        run_id=controller.run_id,
        checkpoint=controller.checkpoint,
        dry_run=body.dry_run,
        interaction_ids=body.ids or None,
    )
    scripted_n = sum(
        1 for r in results if (r.get("payload") or {}).get("executor") == "scripted"
    )
    fallback_n = sum(
        1 for r in results if (r.get("payload") or {}).get("executor") == "llm_fallback"
    )
    summary = f"{scripted_n} scripted"
    if fallback_n:
        summary += f" / {fallback_n} AI-fallback (update scripted_actions.py)"
    return {
        "results": results,
        "count": len(results),
        "scripted_summary": summary,
        "scripted_health": scripted_health_snapshot(),
    }


@app.get("/interactions/{interaction_id}")
def get_one_interaction(interaction_id: str) -> dict[str, Any]:
    row = get_interaction(interaction_id)
    if row is None:
        raise HTTPException(404, f"Unknown interaction {interaction_id}")
    return row


@app.patch("/interactions/{interaction_id}")
def patch_interaction(interaction_id: str, body: InteractionUpdate) -> dict[str, Any]:
    row = get_interaction(interaction_id)
    if row is None:
        raise HTTPException(404, f"Unknown interaction {interaction_id}")
    updated = update_interaction(
        interaction_id,
        draft_text=body.draft_text,
        final_text=body.final_text,
    )
    assert updated is not None
    return updated


@app.post("/interactions/{interaction_id}/approve")
def post_approve(interaction_id: str, body: ApproveBody | None = None) -> dict[str, Any]:
    body = body or ApproveBody()
    row = approve_interaction(interaction_id, final_text=body.final_text)
    if row is None:
        raise HTTPException(404, f"Unknown interaction {interaction_id}")
    return row


@app.post("/interactions/{interaction_id}/reject")
def post_reject(interaction_id: str, body: RejectBody | None = None) -> dict[str, Any]:
    body = body or RejectBody()
    row = reject_interaction(interaction_id, reason=body.reason)
    if row is None:
        raise HTTPException(404, f"Unknown interaction {interaction_id}")
    return row


@app.post("/interactions/{interaction_id}/skip")
def post_skip(interaction_id: str, body: RejectBody | None = None) -> dict[str, Any]:
    """Alias for reject — operator skip."""
    return post_reject(interaction_id, body)


@app.post("/interactions/{interaction_id}/execute")
async def post_execute(interaction_id: str, body: ExecuteBody | None = None) -> dict[str, Any]:
    body = body or ExecuteBody()
    row = get_interaction(interaction_id)
    if row is None:
        raise HTTPException(404, f"Unknown interaction {interaction_id}")
    if row["status"] == "proposed" and not row.get("auto"):
        raise HTTPException(409, "Approve HITL interaction before execute")
    try:
        return await execute_interaction(
            interaction_id,
            checkpoint=controller.checkpoint,
            dry_run=body.dry_run,
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


class IngestCommentDecision(BaseModel):
    approve: bool = False
    text: str | None = None


@app.get("/ingest/comment-pending")
def get_ingest_comment_pending() -> dict[str, Any]:
    from ig_agent.ingest_comment_gate import get_pending

    return {"pending": get_pending()}


@app.post("/ingest/comment-decision")
def post_ingest_comment_decision(body: IngestCommentDecision) -> dict[str, Any]:
    from ig_agent.ingest_comment_gate import submit_decision

    if not submit_decision(approve=body.approve, text=body.text):
        raise HTTPException(409, "No pending ingest comment approval")
    return {"ok": True, "approve": body.approve}


def serve() -> None:
    import logging
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("ig_agent.api")

    port = int(os.getenv("BOT_PORT", "7411"))
    log.info("Starting Instagram Bot API on 127.0.0.1:%s", port)
    uvicorn.run("ig_agent.api:app", host="127.0.0.1", port=port, reload=False, log_level="info")


if __name__ == "__main__":
    serve()
