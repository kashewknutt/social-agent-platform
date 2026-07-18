"""HTTP control API for the Instagram bot."""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from agent_sdk.api import create_control_app

from ig_agent.engage import execute_approved_interactions, execute_interaction
from ig_agent.config import FILTERED_DIR, RAW_DIR, REPORTS_DIR
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
    """Build an Outputs payload from latest on-disk artifacts."""
    import json
    from datetime import datetime
    from pathlib import Path

    raw_files = sorted(RAW_DIR.glob("scraped_*.json"), key=lambda p: p.stat().st_mtime)
    filtered_files = sorted(
        (p for p in FILTERED_DIR.glob("filtered_*.json") if not p.name.endswith("_with_media.json")),
        key=lambda p: p.stat().st_mtime,
    )
    reports = sorted(REPORTS_DIR.glob("Daily_Social_Dashboard_*.md"), key=lambda p: p.stat().st_mtime)

    raw_path = raw_files[-1] if raw_files else None
    filtered_path = filtered_files[-1] if filtered_files else None
    report_path = reports[-1] if reports else None

    raw_posts: list[dict[str, Any]] = []
    filtered_posts: list[dict[str, Any]] = []
    if raw_path and raw_path.exists():
        try:
            raw_posts = list(json.loads(raw_path.read_text(encoding="utf-8")).get("posts") or [])
        except Exception:
            raw_posts = []
    if filtered_path and filtered_path.exists():
        try:
            filtered_posts = list(json.loads(filtered_path.read_text(encoding="utf-8")).get("posts") or [])
        except Exception:
            filtered_posts = []

    report = None
    if report_path and report_path.exists():
        report = {
            "path": str(report_path),
            "generated_at": datetime.fromtimestamp(report_path.stat().st_mtime).isoformat(),
            "body": report_path.read_text(encoding="utf-8"),
        }

    run = None
    if filtered_path or report_path or raw_path:
        run = {
            "run_id": run_id or (filtered_path.stem if filtered_path else "latest"),
            "status": "completed",
            "started_at": None,
            "finished_at": report["generated_at"] if report else None,
            "raw_path": str(raw_path) if raw_path else None,
            "filtered_path": str(filtered_path) if filtered_path else None,
            "report_path": str(report_path) if report_path else None,
        }

    return {
        "ok": True,
        "run": run,
        "runs": [run] if run else [],
        "posts": {"raw": raw_posts, "filtered": filtered_posts, "media": []},
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


@app.get("/interactions")
def get_interactions(
    run_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    items = list_interactions(run_id=run_id, status=status, kind=kind, limit=limit)
    return {"interactions": items, "usage": usage_snapshot()}


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
    return {"results": results, "count": len(results)}


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


def serve() -> None:
    import uvicorn

    port = int(os.getenv("BOT_PORT", "7411"))
    uvicorn.run("ig_agent.api:app", host="127.0.0.1", port=port, reload=False)


if __name__ == "__main__":
    serve()
