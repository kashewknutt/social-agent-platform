"""Basic agent-sdk controller tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_sdk.control import BotController
from agent_sdk.models import Direction, RunMode, RunRequest


def test_controller_run_and_direction(tmp_path: Path):
    direction = Direction(brand_name="Test", competitor_hashtags=["#saas"])

    async def pipeline(controller: BotController, request: RunRequest) -> None:
        await controller.checkpoint()
        controller.set_step("work", "doing work")
        await controller.checkpoint()

    controller = BotController(
        bot_id="test",
        name="Test Bot",
        network="test",
        root=tmp_path,
        pipeline=pipeline,
        load_direction=lambda: direction,
        save_direction=lambda d: direction.__dict__.update(d.model_dump()),
    )

    async def run() -> None:
        req = RunRequest(mode=RunMode.ONCE, engage=True)
        assert req.engage is True
        status = await controller.start(req)
        assert status.state.value == "running"
        while controller._task and not controller._task.done():
            await asyncio.sleep(0.05)
        assert controller.state.value == "idle"
        assert controller.get_direction().brand_name == "Test"

    asyncio.run(run())


def test_run_request_engage_default():
    assert RunRequest().engage is True
    assert RunRequest(engage=False).engage is False
