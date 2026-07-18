"""Append-only JSONL run event streams."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_sdk.models import RunEvent


class RunEventLogger:
    """Writes run events to data/runs/<run_id>.jsonl."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._path: Path | None = None
        self._run_id: str | None = None
        self._memory: list[RunEvent] = []

    @property
    def run_id(self) -> str | None:
        return self._run_id

    def start(self, run_id: str) -> Path:
        self._run_id = run_id
        self._path = self.runs_dir / f"{run_id}.jsonl"
        self._memory = []
        self._path.touch(exist_ok=True)
        return self._path

    def emit(
        self,
        message: str,
        *,
        level: str = "info",
        step: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        if not self._run_id or not self._path:
            raise RuntimeError("RunEventLogger.start() must be called first")
        event = RunEvent(
            run_id=self._run_id,
            level=level,
            step=step,
            message=message,
            data=data or {},
        )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")
        self._memory.append(event)
        return event

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        if self._path and self._path.exists():
            lines = self._path.read_text(encoding="utf-8").splitlines()
            return [json.loads(line) for line in lines[-n:] if line.strip()]
        return [e.model_dump() for e in self._memory[-n:]]

    def latest_run_tail(self, n: int = 200) -> list[dict[str, Any]]:
        files = sorted(self.runs_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        if not files:
            return self.tail(n)
        lines = files[-1].read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines[-n:] if line.strip()]
