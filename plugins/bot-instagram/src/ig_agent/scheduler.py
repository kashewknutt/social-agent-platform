"""Continuous background scheduler for Instagram trend ingestion."""

from __future__ import annotations

import asyncio
import logging

from agent_sdk.models import RunMode, RunRequest

from ig_agent.runtime import build_controller

logger = logging.getLogger("ig_agent.scheduler")


async def run_daemon() -> None:
    """Run continuous ingestion loop with pause/stop checkpoints."""
    controller = build_controller()
    await controller.start(RunRequest(mode=RunMode.DAEMON))
    # Keep the process alive while the controller task runs.
    while controller._task and not controller._task.done():
        await asyncio.sleep(0.5)
    if controller.last_error:
        logger.error("Daemon ended with error: %s", controller.last_error)
