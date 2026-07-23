"""Orchestrator FastAPI app — fleet registry + proxy + static UI."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from orchestrator_app.registry import BotRegistry, load_config

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = ROOT / "frontend" / "dist"
FRONTEND_PUBLIC = ROOT / "frontend" / "public"

config = load_config()
registry = BotRegistry(config)

app = FastAPI(title="Social Agent Orchestrator")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    return {"ok": True, "bots": len(registry.list_bots())}


@app.get("/api/bots")
async def list_bots() -> dict[str, Any]:
    items = await asyncio.gather(*[registry.snapshot(bot.id) for bot in registry.list_bots()])
    return {"bots": list(items)}


@app.get("/api/bots/{bot_id}")
async def get_bot(bot_id: str) -> dict[str, Any]:
    try:
        return await registry.snapshot(bot_id)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc


@app.get("/api/bots/{bot_id}/status")
async def bot_status(bot_id: str) -> Any:
    try:
        return await registry.proxy(bot_id, "GET", "/status")
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot unreachable: {exc}") from exc


@app.post("/api/bots/{bot_id}/run")
async def bot_run(bot_id: str, request: Request) -> Any:
    body = await request.json()
    try:
        return await registry.proxy(bot_id, "POST", "/run", body)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot unreachable: {exc}") from exc


@app.post("/api/bots/{bot_id}/pause")
async def bot_pause(bot_id: str) -> Any:
    return await _proxy(bot_id, "POST", "/pause")


@app.post("/api/bots/{bot_id}/resume")
async def bot_resume(bot_id: str) -> Any:
    return await _proxy(bot_id, "POST", "/resume")


@app.post("/api/bots/{bot_id}/stop")
async def bot_stop(bot_id: str) -> Any:
    return await _proxy(bot_id, "POST", "/stop")


@app.get("/api/bots/{bot_id}/logs")
async def bot_logs(bot_id: str, tail: int = Query(200, ge=1, le=2000)) -> Any:
    return await _proxy(bot_id, "GET", f"/logs?tail={tail}")


@app.get("/api/bots/{bot_id}/boot-log")
def bot_boot_log(bot_id: str, tail: int = Query(80, ge=1, le=500)) -> Any:
    try:
        return registry.read_boot_log(bot_id, tail=tail)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc


@app.get("/api/bots/{bot_id}/disconnect-log")
def bot_disconnect_log(bot_id: str, tail: int = Query(30, ge=1, le=200)) -> Any:
    from orchestrator_app.registry import read_disconnect_log

    return {"lines": read_disconnect_log(tail=tail, bot_id=bot_id)}


@app.get("/api/bots/{bot_id}/direction")
async def get_direction(bot_id: str) -> Any:
    return await _proxy(bot_id, "GET", "/direction")


@app.put("/api/bots/{bot_id}/direction")
async def put_direction(bot_id: str, request: Request) -> Any:
    body = await request.json()
    return await _proxy(bot_id, "PUT", "/direction", body)


@app.get("/api/bots/{bot_id}/outputs")
async def bot_outputs(bot_id: str, run_id: str | None = Query(default=None)) -> Any:
    path = "/outputs" if not run_id else f"/outputs?run_id={run_id}"
    return await _proxy(bot_id, "GET", path)


@app.get("/api/bots/{bot_id}/live")
async def bot_live(bot_id: str) -> Any:
    return await _proxy(bot_id, "GET", "/live")


@app.post("/api/bots/{bot_id}/process/start")
def start_process(bot_id: str) -> Any:
    try:
        return registry.start_bot_process(bot_id)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/bots/{bot_id}/process/stop")
def stop_process(bot_id: str) -> Any:
    try:
        return registry.stop_bot_process(bot_id)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc


@app.get("/api/bots/{bot_id}/interactions")
async def bot_interactions(
    bot_id: str,
    run_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
) -> Any:
    qs = [f"limit={limit}"]
    if run_id:
        qs.append(f"run_id={run_id}")
    if status:
        qs.append(f"status={status}")
    if kind:
        qs.append(f"kind={kind}")
    return await _proxy(bot_id, "GET", f"/interactions?{'&'.join(qs)}")


@app.post("/api/bots/{bot_id}/interactions/propose")
async def bot_interactions_propose(bot_id: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy(bot_id, "POST", "/interactions/propose", body)


@app.post("/api/bots/{bot_id}/interactions/execute-approved")
async def bot_interactions_execute_approved(bot_id: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy(bot_id, "POST", "/interactions/execute-approved", body)


@app.get("/api/bots/{bot_id}/engage/circuit")
async def bot_engage_circuit(bot_id: str) -> Any:
    return await _proxy(bot_id, "GET", "/engage/circuit")


@app.get("/api/bots/{bot_id}/engage/scripted-health")
async def bot_scripted_health(bot_id: str) -> Any:
    return await _proxy(bot_id, "GET", "/engage/scripted-health")


@app.get("/api/bots/{bot_id}/ingest/comment-pending")
async def bot_ingest_comment_pending(bot_id: str) -> Any:
    return await _proxy(bot_id, "GET", "/ingest/comment-pending")


@app.post("/api/bots/{bot_id}/ingest/comment-decision")
async def bot_ingest_comment_decision(bot_id: str, request: Request) -> Any:
    body = await request.json()
    return await _proxy(bot_id, "POST", "/ingest/comment-decision", body)


@app.post("/api/bots/{bot_id}/engage/circuit/clear")
async def bot_engage_circuit_clear(bot_id: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy(bot_id, "POST", "/engage/circuit/clear", body)


@app.get("/api/bots/{bot_id}/interactions/{interaction_id}")
async def bot_interaction(bot_id: str, interaction_id: str) -> Any:
    return await _proxy(bot_id, "GET", f"/interactions/{interaction_id}")


@app.patch("/api/bots/{bot_id}/interactions/{interaction_id}")
async def bot_interaction_patch(bot_id: str, interaction_id: str, request: Request) -> Any:
    body = await request.json()
    return await _proxy(bot_id, "PATCH", f"/interactions/{interaction_id}", body)


@app.post("/api/bots/{bot_id}/interactions/{interaction_id}/approve")
async def bot_interaction_approve(bot_id: str, interaction_id: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy(bot_id, "POST", f"/interactions/{interaction_id}/approve", body)


@app.post("/api/bots/{bot_id}/interactions/{interaction_id}/reject")
async def bot_interaction_reject(bot_id: str, interaction_id: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy(bot_id, "POST", f"/interactions/{interaction_id}/reject", body)


@app.post("/api/bots/{bot_id}/interactions/{interaction_id}/skip")
async def bot_interaction_skip(bot_id: str, interaction_id: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy(bot_id, "POST", f"/interactions/{interaction_id}/skip", body)


@app.post("/api/bots/{bot_id}/interactions/{interaction_id}/execute")
async def bot_interaction_execute(bot_id: str, interaction_id: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy(bot_id, "POST", f"/interactions/{interaction_id}/execute", body)


@app.post("/api/bots/{bot_id}/analyzer/upload")
async def bot_analyzer_upload(
    bot_id: str,
    file: UploadFile = File(...),
    context_note: str | None = Form(default=None),
) -> Any:
    content = await file.read()
    files = {
        "file": (
            file.filename or "video.mp4",
            content,
            file.content_type or "application/octet-stream",
        )
    }
    data: dict[str, Any] = {}
    if context_note is not None:
        data["context_note"] = context_note
    try:
        return await registry.proxy_multipart(
            bot_id, "/analyzer/upload", files=files, data=data, timeout=180.0
        )
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot unreachable: {exc}") from exc


@app.get("/api/bots/{bot_id}/analyzer")
async def bot_analyzer_list(
    bot_id: str,
    status: str | None = None,
    posted: bool | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> Any:
    qs = [f"limit={limit}"]
    if status:
        qs.append(f"status={status}")
    if posted is not None:
        qs.append(f"posted={str(posted).lower()}")
    return await _proxy(bot_id, "GET", f"/analyzer?{'&'.join(qs)}")


@app.get("/api/bots/{bot_id}/analyzer/{analysis_id}")
async def bot_analyzer_one(bot_id: str, analysis_id: str) -> Any:
    return await _proxy(bot_id, "GET", f"/analyzer/{analysis_id}")


@app.patch("/api/bots/{bot_id}/analyzer/{analysis_id}")
async def bot_analyzer_patch(bot_id: str, analysis_id: str, request: Request) -> Any:
    body = await request.json()
    return await _proxy(bot_id, "PATCH", f"/analyzer/{analysis_id}", body)


@app.delete("/api/bots/{bot_id}/analyzer/{analysis_id}")
async def bot_analyzer_delete(bot_id: str, analysis_id: str) -> Any:
    return await _proxy(bot_id, "DELETE", f"/analyzer/{analysis_id}")


async def _proxy(bot_id: str, method: str, path: str, body: Any = None) -> Any:
    try:
        return await registry.proxy(bot_id, method, path, body)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown bot {bot_id}") from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, exc.response.text) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Bot unreachable: {exc}") from exc


def _static_dir() -> Path | None:
    if FRONTEND_DIST.exists() and (FRONTEND_DIST / "index.html").exists():
        return FRONTEND_DIST
    if FRONTEND_PUBLIC.exists() and (FRONTEND_PUBLIC / "index.html").exists():
        return FRONTEND_PUBLIC
    return None


@app.get("/")
def index() -> FileResponse:
    static_dir = _static_dir()
    if not static_dir:
        raise HTTPException(404, "Frontend not found. Add frontend/public/index.html")
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


assets_dir = FRONTEND_DIST / "assets"
if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


def main() -> None:
    parser = argparse.ArgumentParser(description="Social agent orchestrator")
    parser.add_argument("--host", default=config.host)
    parser.add_argument("--port", type=int, default=config.port)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run("orchestrator_app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
