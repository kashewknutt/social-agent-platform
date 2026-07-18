"""Bot registry loaded from orchestrator.yaml."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml


@dataclass
class BotEntry:
    id: str
    name: str
    path: Path
    port: int
    enabled: bool = True
    start_command: str = ""
    url: str = ""
    process: subprocess.Popen | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"http://127.0.0.1:{self.port}"


@dataclass
class OrchestratorConfig:
    host: str = "127.0.0.1"
    port: int = 7400
    bots: list[BotEntry] = field(default_factory=list)


def load_config(path: Path | None = None) -> OrchestratorConfig:
    root = Path(__file__).resolve().parents[2]
    cfg_path = path or root / "orchestrator.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    bots: list[BotEntry] = []
    for item in raw.get("bots", []):
        bot_path = (cfg_path.parent / item["path"]).resolve()
        bots.append(
            BotEntry(
                id=item["id"],
                name=item.get("name", item["id"]),
                path=bot_path,
                port=int(item["port"]),
                enabled=bool(item.get("enabled", True)),
                start_command=item.get("start_command", ""),
                url=item.get("url", ""),
            )
        )
    return OrchestratorConfig(
        host=raw.get("host", "127.0.0.1"),
        port=int(raw.get("port", 7400)),
        bots=bots,
    )


class BotRegistry:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self._by_id = {b.id: b for b in config.bots}
        # Reuse one client; trust_env=False avoids corporate proxy / IPv6 stalls on Windows
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0, connect=0.5),
            trust_env=False,
        )

    def list_bots(self) -> list[BotEntry]:
        return list(self.config.bots)

    def get(self, bot_id: str) -> BotEntry:
        if bot_id not in self._by_id:
            raise KeyError(bot_id)
        return self._by_id[bot_id]

    def _managed(self, bot: BotEntry) -> bool:
        return bot.process is not None and bot.process.poll() is None

    async def proxy(self, bot_id: str, method: str, path: str, json_body: Any = None) -> Any:
        bot = self.get(bot_id)
        response = await self._client.request(
            method,
            f"{bot.url}{path}",
            json=json_body,
        )
        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json()
            except Exception:
                pass
            raise httpx.HTTPStatusError(
                f"{response.status_code}",
                request=response.request,
                response=response,
            )
        if response.status_code == 204 or not response.content:
            return {"ok": True}
        return response.json()

    async def health(self, bot_id: str) -> dict[str, Any]:
        bot = self.get(bot_id)
        try:
            r = await self._client.get(f"{bot.url}/health")
            if r.status_code == 200:
                data = r.json()
                data["reachable"] = True
                data["managed"] = self._managed(bot)
                return data
        except Exception as exc:
            return {
                "ok": False,
                "reachable": False,
                "bot_id": bot_id,
                "state": "offline",
                "error": str(exc),
                "managed": self._managed(bot),
            }
        return {
            "ok": False,
            "reachable": False,
            "bot_id": bot_id,
            "state": "offline",
            "managed": self._managed(bot),
        }

    async def snapshot(self, bot_id: str) -> dict[str, Any]:
        """One round-trip status fetch used by the fleet UI."""
        bot = self.get(bot_id)
        managed = self._managed(bot)
        try:
            status = await self.proxy(bot_id, "GET", "/status")
            health = {
                "ok": True,
                "reachable": True,
                "bot_id": bot.id,
                "state": status.get("state", "idle"),
                "managed": managed,
            }
        except Exception as exc:
            health = {
                "ok": False,
                "reachable": False,
                "bot_id": bot.id,
                "state": "offline",
                "error": str(exc),
                "managed": managed,
            }
            status = None
        return {
            "id": bot.id,
            "name": bot.name,
            "port": bot.port,
            "path": str(bot.path),
            "enabled": bot.enabled,
            "url": bot.url,
            "health": health,
            "status": status,
        }

    def _resolve_python(self, bot: BotEntry) -> str:
        """Prefer platform .venv, then bot .venv, then current interpreter."""
        platform_root = Path(__file__).resolve().parents[3]
        candidates = [
            platform_root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
            bot.path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return sys.executable

    def start_bot_process(self, bot_id: str) -> dict[str, Any]:
        bot = self.get(bot_id)
        if not bot.enabled:
            raise RuntimeError(f"Bot {bot_id} is disabled in orchestrator.yaml")
        if bot.process and bot.process.poll() is None:
            return {"ok": True, "message": "already running", "pid": bot.process.pid}
        if not bot.start_command:
            raise RuntimeError(f"No start_command for {bot_id}")
        env = os.environ.copy()
        env["BOT_PORT"] = str(bot.port)
        python = self._resolve_python(bot)
        if bot.start_command.startswith("python"):
            cmd = [python, *bot.start_command.split()[1:]]
        else:
            cmd = bot.start_command
        bot.process = subprocess.Popen(
            cmd,
            cwd=str(bot.path),
            env=env,
            shell=isinstance(cmd, str),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "message": "started", "pid": bot.process.pid}

    def stop_bot_process(self, bot_id: str) -> dict[str, Any]:
        bot = self.get(bot_id)
        if not bot.process or bot.process.poll() is not None:
            bot.process = None
            return {"ok": True, "message": "not running"}
        if os.name == "nt":
            bot.process.terminate()
        else:
            bot.process.send_signal(signal.SIGTERM)
        try:
            bot.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bot.process.kill()
        pid = bot.process.pid
        bot.process = None
        return {"ok": True, "message": "stopped", "pid": pid}
