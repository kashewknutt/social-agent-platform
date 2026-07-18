"""BotController — lifecycle, direction, and cooperative pause/stop."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_sdk.events import RunEventLogger
from agent_sdk.models import (
    ArtifactInfo,
    BotState,
    Direction,
    RunMode,
    RunRequest,
    StatusPayload,
    UsageInfo,
)

logger = logging.getLogger("agent_sdk.control")

PipelineFn = Callable[["BotController", RunRequest], Awaitable[None]]
DirectionLoader = Callable[[], Direction]
DirectionSaver = Callable[[Direction], None]
UsageLoader = Callable[[], dict[str, Any]]
ArtifactsLoader = Callable[[], list[ArtifactInfo]]


class BotController:
    """In-process control plane for a single bot."""

    def __init__(
        self,
        *,
        bot_id: str,
        name: str,
        network: str,
        root: Path,
        pipeline: PipelineFn,
        load_direction: DirectionLoader,
        save_direction: DirectionSaver,
        load_usage: UsageLoader | None = None,
        load_artifacts: ArtifactsLoader | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.name = name
        self.network = network
        self.root = Path(root)
        self._pipeline = pipeline
        self._load_direction = load_direction
        self._save_direction = save_direction
        self._load_usage = load_usage
        self._load_artifacts = load_artifacts

        self.state = BotState.IDLE
        self.current_step: str | None = None
        self.last_action: str | None = None
        self.last_error: str | None = None
        self.run_id: str | None = None
        self.mode: RunMode | None = None
        self.updated_at = datetime.now().isoformat()

        self._pause = asyncio.Event()
        self._pause.set()  # not paused
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.events = RunEventLogger(self.root / "data" / "runs")

    def status(self) -> StatusPayload:
        usage = None
        if self._load_usage:
            raw = self._load_usage()
            usage = UsageInfo(**raw)
        artifacts = self._load_artifacts() if self._load_artifacts else []
        return StatusPayload(
            bot_id=self.bot_id,
            name=self.name,
            network=self.network,
            state=self.state,
            current_step=self.current_step,
            last_action=self.last_action,
            last_error=self.last_error,
            run_id=self.run_id,
            mode=self.mode,
            usage=usage,
            artifacts=artifacts,
            updated_at=self.updated_at,
        )

    def get_direction(self) -> Direction:
        return self._load_direction()

    def set_direction(self, direction: Direction) -> Direction:
        self._save_direction(direction)
        self.last_action = "direction updated"
        self.updated_at = datetime.now().isoformat()
        return direction

    def set_step(self, step: str, message: str | None = None) -> None:
        self.current_step = step
        self.last_action = message or step
        self.updated_at = datetime.now().isoformat()
        if self.events.run_id:
            self.events.emit(message or step, step=step)

    async def checkpoint(self) -> None:
        """Await pause; raise CancelledError-like stop when stop requested."""
        while not self._pause.is_set():
            if self._stop.is_set():
                raise asyncio.CancelledError("stop requested")
            self.state = BotState.PAUSED
            self.updated_at = datetime.now().isoformat()
            await asyncio.sleep(0.25)
        if self._stop.is_set():
            raise asyncio.CancelledError("stop requested")
        if self.state == BotState.PAUSED:
            self.state = BotState.RUNNING
            self.updated_at = datetime.now().isoformat()

    async def start(self, request: RunRequest) -> StatusPayload:
        if self._task and not self._task.done():
            raise RuntimeError(f"Bot already {self.state.value}")
        self._stop.clear()
        self._pause.set()
        self.state = BotState.RUNNING
        self.mode = request.mode
        self.last_error = None
        self.run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.events.start(self.run_id)
        self.set_step("starting", f"run {request.mode.value}")
        self._task = asyncio.create_task(self._run_wrapper(request), name=f"{self.bot_id}-run")
        return self.status()

    async def _run_wrapper(self, request: RunRequest) -> None:
        try:
            self.events.emit(f"Pipeline started ({request.mode.value})", step="starting")
            await self._pipeline(self, request)
            if self._stop.is_set():
                self.state = BotState.STOPPED
                self.set_step("stopped", "stopped by operator")
            else:
                self.state = BotState.IDLE
                self.set_step("idle", "run completed")
                self.events.emit("Pipeline completed", step="idle")
        except asyncio.CancelledError:
            self.state = BotState.STOPPED
            self.set_step("stopped", "cancelled")
            self.events.emit("Pipeline cancelled", level="warning", step="stopped")
        except Exception as exc:
            logger.exception("Bot run failed")
            self.state = BotState.ERROR
            self.last_error = str(exc)
            self.set_step("error", str(exc))
            self.events.emit(str(exc), level="error", step="error")
        finally:
            self.updated_at = datetime.now().isoformat()

    async def pause(self) -> StatusPayload:
        if self.state != BotState.RUNNING:
            raise RuntimeError(f"Cannot pause from state {self.state.value}")
        self._pause.clear()
        self.state = BotState.PAUSED
        self.set_step("paused", "paused by operator")
        self.events.emit("Paused", step="paused")
        return self.status()

    async def resume(self) -> StatusPayload:
        if self.state != BotState.PAUSED:
            raise RuntimeError(f"Cannot resume from state {self.state.value}")
        self._pause.set()
        self.state = BotState.RUNNING
        self.set_step(self.current_step or "running", "resumed by operator")
        self.events.emit("Resumed", step="running")
        return self.status()

    async def stop(self) -> StatusPayload:
        self._stop.set()
        self._pause.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.state = BotState.STOPPED
        self.set_step("stopped", "stopped by operator")
        return self.status()

    def logs(self, tail: int = 200) -> list[dict[str, Any]]:
        return self.events.latest_run_tail(tail)
