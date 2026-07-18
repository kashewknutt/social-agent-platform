"""FastAPI control surface shared by every bot."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from agent_sdk.control import BotController
from agent_sdk.models import (
    Direction,
    HealthResponse,
    MessageResponse,
    RunRequest,
    StatusPayload,
)


def create_control_app(controller: BotController, *, title: str | None = None) -> FastAPI:
    app = FastAPI(title=title or f"{controller.name} Control API")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(bot_id=controller.bot_id, state=controller.state)

    @app.get("/status", response_model=StatusPayload)
    def status() -> StatusPayload:
        return controller.status()

    @app.post("/run", response_model=StatusPayload)
    async def run(request: RunRequest) -> StatusPayload:
        try:
            return await controller.start(request)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/pause", response_model=StatusPayload)
    async def pause() -> StatusPayload:
        try:
            return await controller.pause()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/resume", response_model=StatusPayload)
    async def resume() -> StatusPayload:
        try:
            return await controller.resume()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/stop", response_model=StatusPayload)
    async def stop() -> StatusPayload:
        return await controller.stop()

    @app.get("/logs")
    def logs(tail: int = Query(default=200, ge=1, le=2000)) -> dict:
        return {"lines": controller.logs(tail)}

    @app.get("/direction", response_model=Direction)
    def get_direction() -> Direction:
        return controller.get_direction()

    @app.put("/direction", response_model=Direction)
    def put_direction(direction: Direction) -> Direction:
        return controller.set_direction(direction)

    @app.get("/")
    def root() -> MessageResponse:
        return MessageResponse(
            message=f"{controller.name} control API",
            state=controller.state,
        )

    return app
