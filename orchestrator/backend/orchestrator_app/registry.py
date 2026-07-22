"""Bot registry loaded from orchestrator.yaml."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

logger = logging.getLogger("orchestrator.registry")

DISCONNECT_LOG = Path(__file__).resolve().parents[2] / "data" / "disconnect_log.jsonl"
_last_disconnect: dict[str, tuple[str, float]] = {}


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
    boot_log: Path | None = field(default=None, repr=False)

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


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex((host, port)) == 0


def _health_sync(url: str, timeout: float = 1.5) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            r = client.get(f"{url.rstrip('/')}/health")
            if r.status_code == 200:
                return r.json()
    except Exception:
        return None
    return None


def _log_disconnect(bot_id: str, reason: str, detail: str = "") -> None:
    """Append a disconnect event for Fleet diagnostics (rate-limited per bot+reason)."""
    key = f"{bot_id}:{reason}"
    now = time.time()
    prev = _last_disconnect.get(key)
    if prev and now - prev[1] < 45.0 and prev[0] == detail[:200]:
        return
    _last_disconnect[key] = (detail[:200], now)
    try:
        DISCONNECT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(),
            "bot_id": bot_id,
            "reason": reason,
            "detail": (detail or "")[:500],
        }
        with DISCONNECT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        logger.warning("Bot %s disconnect: %s — %s", bot_id, reason, detail[:200])
    except Exception:
        logger.exception("Failed to write disconnect log")


def read_disconnect_log(tail: int = 50, bot_id: str | None = None) -> list[dict[str, Any]]:
    if not DISCONNECT_LOG.exists():
        return []
    try:
        lines = DISCONNECT_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if bot_id and row.get("bot_id") != bot_id:
            continue
        out.append(row)
    return out[-tail:]


class BotRegistry:
    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self._by_id = {b.id: b for b in config.bots}
        # Reuse one client; trust_env=False avoids corporate proxy / IPv6 stalls on Windows
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=1.0),
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
        """One round-trip status fetch used by the fleet UI.

        Reachability uses /health (fast). /status is best-effort — a slow status
        must not mark the bot offline while the API process is still alive.
        """
        bot = self.get(bot_id)
        managed = self._managed(bot)
        health_data: dict[str, Any] | None = None
        status: dict[str, Any] | None = None
        status_error: str | None = None

        try:
            r = await self._client.get(f"{bot.url}/health")
            if r.status_code == 200:
                health_data = r.json()
            else:
                detail = f"HTTP {r.status_code}: {r.text[:200]}"
                _log_disconnect(bot_id, "health_bad_status", detail)
                return {
                    "id": bot.id,
                    "name": bot.name,
                    "port": bot.port,
                    "path": str(bot.path),
                    "enabled": bot.enabled,
                    "url": bot.url,
                    "health": {
                        "ok": False,
                        "reachable": False,
                        "bot_id": bot.id,
                        "state": "offline",
                        "error": detail,
                        "managed": managed,
                        "boot_log": str(bot.boot_log or self._boot_log_path(bot)),
                    },
                    "status": None,
                }
        except Exception as exc:
            detail = str(exc)
            _log_disconnect(bot_id, "health_unreachable", detail)
            return {
                "id": bot.id,
                "name": bot.name,
                "port": bot.port,
                "path": str(bot.path),
                "enabled": bot.enabled,
                "url": bot.url,
                "health": {
                    "ok": False,
                    "reachable": False,
                    "bot_id": bot.id,
                    "state": "offline",
                    "error": detail,
                    "managed": managed,
                    "boot_log": str(bot.boot_log or self._boot_log_path(bot)),
                },
                "status": None,
            }

        try:
            status = await self.proxy(bot_id, "GET", "/status")
        except Exception as exc:
            status_error = str(exc)
            _log_disconnect(bot_id, "status_timeout", status_error)

        state = "idle"
        if isinstance(status, dict):
            state = status.get("state", health_data.get("state", "idle") if health_data else "idle")
        elif health_data:
            state = health_data.get("state", "idle")

        health = {
            "ok": True,
            "reachable": True,
            "bot_id": bot.id,
            "state": state,
            "managed": managed,
            "boot_log": str(bot.boot_log or self._boot_log_path(bot)),
        }
        if status_error:
            health["status_error"] = status_error
        if not managed and not bot.process:
            health["note"] = "API up but not started by Fleet — click Boot API to manage lifecycle"

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

    def read_boot_log(self, bot_id: str, tail: int = 80) -> dict[str, Any]:
        bot = self.get(bot_id)
        path = bot.boot_log or self._boot_log_path(bot)
        if not path.exists():
            return {"path": str(path), "lines": [], "message": "No boot log yet"}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()[-tail:]
            return {"path": str(path), "lines": lines}
        except Exception as exc:
            return {"path": str(path), "lines": [], "error": str(exc)}

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

    def _boot_log_path(self, bot: BotEntry) -> Path:
        log_dir = bot.path / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / "api_boot.log"

    def _kill_port_holders(self, port: int) -> list[int]:
        """Best-effort kill of whatever still owns the bot port (zombie APIs)."""
        killed: list[int] = []
        if os.name == "nt":
            try:
                out = subprocess.check_output(
                    ["netstat", "-ano", "-p", "tcp"],
                    text=True,
                    errors="ignore",
                )
            except Exception:
                return killed
            needle = f":{port} "
            pids: set[int] = set()
            for line in out.splitlines():
                if needle not in line or "LISTENING" not in line.upper():
                    continue
                parts = line.split()
                if not parts:
                    continue
                try:
                    pids.add(int(parts[-1]))
                except ValueError:
                    continue
            for pid in pids:
                if pid <= 0:
                    continue
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F", "/T"],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    killed.append(pid)
                except Exception:
                    pass
        else:
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f"tcp:{port}"], text=True, errors="ignore"
                )
                for raw in out.split():
                    try:
                        pid = int(raw.strip())
                    except ValueError:
                        continue
                    try:
                        os.kill(pid, signal.SIGTERM)
                        killed.append(pid)
                    except Exception:
                        pass
            except Exception:
                pass
        if killed:
            time.sleep(0.6)
        return killed

    def start_bot_process(self, bot_id: str) -> dict[str, Any]:
        bot = self.get(bot_id)
        if not bot.enabled:
            raise RuntimeError(f"Bot {bot_id} is disabled in orchestrator.yaml")
        if not bot.start_command:
            raise RuntimeError(f"No start_command for {bot_id}")

        # Already managed by this Fleet process
        if bot.process and bot.process.poll() is None:
            health = _health_sync(bot.url)
            if health:
                return {
                    "ok": True,
                    "message": "already running",
                    "pid": bot.process.pid,
                    "health": health,
                }

        # Unmanaged but healthy (started outside Fleet) — adopt, don't spawn a second copy
        existing = _health_sync(bot.url)
        if existing:
            return {
                "ok": True,
                "message": "already up (external process)",
                "pid": None,
                "health": existing,
            }

        # Clear zombie listeners that accept TCP but never serve /health
        freed = self._kill_port_holders(bot.port)
        if bot.process and bot.process.poll() is None:
            try:
                bot.process.terminate()
                bot.process.wait(timeout=3)
            except Exception:
                try:
                    bot.process.kill()
                except Exception:
                    pass
        bot.process = None

        env = os.environ.copy()
        env["BOT_PORT"] = str(bot.port)
        python = self._resolve_python(bot)
        if bot.start_command.startswith("python"):
            cmd = [python, *bot.start_command.split()[1:]]
        else:
            cmd = bot.start_command

        log_path = self._boot_log_path(bot)
        bot.boot_log = log_path
        log_fh = log_path.open("a", encoding="utf-8")
        log_fh.write(f"\n--- boot {time.strftime('%Y-%m-%d %H:%M:%S')} cmd={cmd} ---\n")
        log_fh.flush()

        bot.process = subprocess.Popen(
            cmd,
            cwd=str(bot.path),
            env=env,
            shell=isinstance(cmd, str),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )

        # Wait until /health answers — Boot used to return before bind, looking "broken"
        deadline = time.time() + 12.0
        last_health: dict[str, Any] | None = None
        while time.time() < deadline:
            if bot.process.poll() is not None:
                log_fh.close()
                tail = ""
                try:
                    tail = log_path.read_text(encoding="utf-8")[-1200:]
                except Exception:
                    pass
                raise RuntimeError(
                    f"Bot {bot_id} exited during boot (code {bot.process.returncode}). "
                    f"See {log_path}. Tail:\n{tail}"
                )
            last_health = _health_sync(bot.url)
            if last_health:
                break
            time.sleep(0.35)

        if not last_health:
            return {
                "ok": False,
                "message": (
                    f"started pid={bot.process.pid} but /health not ready yet "
                    f"(port {bot.port} open={_port_open(bot.port)}; "
                    f"freed_pids={freed}). Check {log_path}"
                ),
                "pid": bot.process.pid,
                "log": str(log_path),
                "freed_pids": freed,
            }

        return {
            "ok": True,
            "message": "started",
            "pid": bot.process.pid,
            "health": last_health,
            "freed_pids": freed,
            "log": str(log_path),
        }

    def stop_bot_process(self, bot_id: str) -> dict[str, Any]:
        bot = self.get(bot_id)
        killed = self._kill_port_holders(bot.port)
        if not bot.process or bot.process.poll() is not None:
            bot.process = None
            return {"ok": True, "message": "not running", "freed_pids": killed}
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
        return {"ok": True, "message": "stopped", "pid": pid, "freed_pids": killed}
