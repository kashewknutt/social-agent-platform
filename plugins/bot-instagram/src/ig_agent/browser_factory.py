"""Shared browser-use session helpers for Instagram bot.

browser-use's BrowserStartEvent defaults to a 30s timeout. On Windows with a
real Chrome profile that is often too short — Chrome opens, CDP never becomes
ready in time, and the orphan window holds SingletonLock so every following
DM/comment also fails. These helpers raise timeouts, clear stale locks, and
build sessions with our configured Chrome binary.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from ig_agent.config import Settings

logger = logging.getLogger(__name__)

# browser-use reads these once per event construction via os.getenv.
_TIMEOUT_ENV: dict[str, str] = {
    "TIMEOUT_BrowserStartEvent": "180",
    "TIMEOUT_BrowserLaunchEvent": "120",
    "TIMEOUT_BrowserStopEvent": "60",
    "TIMEOUT_BrowserKillEvent": "60",
    "TIMEOUT_BrowserConnectedEvent": "60",
    "TIMEOUT_NavigateToUrlEvent": "60",
    "TIMEOUT_BrowserStateRequestEvent": "60",
}

_LOCK_NAMES = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "lockfile",
)


def ensure_browser_timeouts() -> None:
    """Bump browser-use event timeouts before any BrowserSession is created."""
    for key, value in _TIMEOUT_ENV.items():
        # Only set if unset so operators can override via env.
        os.environ.setdefault(key, value)


def clear_profile_locks(user_data_dir: Path | str) -> int:
    """Remove Chrome Singleton* lock files that block a new session."""
    root = Path(user_data_dir)
    removed = 0
    if not root.exists():
        return 0
    candidates = [root / name for name in _LOCK_NAMES]
    # Also check Default/ and nested profile dirs Chrome sometimes uses.
    for sub in root.iterdir() if root.is_dir() else []:
        if sub.is_dir():
            candidates.extend(sub / name for name in _LOCK_NAMES)
    for path in candidates:
        try:
            if path.exists() or path.is_symlink():
                path.unlink(missing_ok=True)
                removed += 1
                logger.info("Removed stale browser lock: %s", path)
        except OSError as exc:
            logger.warning("Could not remove lock %s: %s", path, exc)
    return removed


def kill_orphan_chrome_for_profile(user_data_dir: Path | str) -> int:
    """Kill Chrome/Chromium processes whose command line references our profile.

    When BrowserStartEvent times out, Chrome often stays open holding the
    profile lock. Clearing locks alone is not enough on Windows.
    """
    try:
        import psutil
    except ImportError:
        return 0

    needle = str(Path(user_data_dir).resolve()).lower().replace("/", "\\")
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in {"chrome.exe", "chromium.exe", "google-chrome", "chrome", "chromium"}:
                continue
            cmdline = proc.info.get("cmdline") or []
            joined = " ".join(str(p) for p in cmdline).lower().replace("/", "\\")
            if needle and needle in joined:
                proc.kill()
                killed += 1
                logger.info("Killed orphan browser pid=%s for profile %s", proc.pid, user_data_dir)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            logger.exception("Error inspecting browser process")
    if killed:
        # Give the OS a moment to release file locks.
        time.sleep(1.0)
    return killed


def prepare_profile(user_data_dir: Path | str) -> Path:
    """Ensure profile dir exists and is not locked by a dead Chrome."""
    path = Path(user_data_dir)
    path.mkdir(parents=True, exist_ok=True)
    kill_orphan_chrome_for_profile(path)
    clear_profile_locks(path)
    return path


def make_browser_session(
    settings: Settings,
    *,
    user_data_dir: Path | str | None = None,
    headless: bool = False,
    keep_alive: bool = False,
) -> Any:
    """Create a BrowserSession configured for local Chrome + our profile."""
    from browser_use import BrowserSession

    ensure_browser_timeouts()
    profile = prepare_profile(user_data_dir or settings.browser_user_data_dir)
    chrome = (settings.chrome_path or "").strip()
    kwargs: dict[str, Any] = {
        "headless": headless,
        "user_data_dir": str(profile),
        "is_local": True,
        "keep_alive": keep_alive,
        # Extensions + captcha watchdog slow CDP readiness and are unused for IG.
        "enable_default_extensions": False,
        "captcha_solver": False,
        # Snappier page/action pacing for comment/DM execute.
        "minimum_wait_page_load_time": 0.15,
        "wait_for_network_idle_page_load_time": 0.35,
        "wait_between_actions": 0.05,
    }
    if chrome and Path(chrome).exists():
        kwargs["executable_path"] = chrome
    else:
        logger.warning(
            "CHROME_PATH not found (%s) — browser-use will search defaults",
            chrome or "(empty)",
        )
    return BrowserSession(**kwargs)


async def safe_kill(browser: Any) -> None:
    """Best-effort session kill that never raises to the caller."""
    if browser is None:
        return
    try:
        await browser.kill()
    except Exception as exc:
        logger.warning("browser.kill() failed: %s", exc)
    # Belt-and-suspenders: free the profile if kill left orphans.
    try:
        udd = getattr(getattr(browser, "browser_profile", None), "user_data_dir", None)
        if udd:
            kill_orphan_chrome_for_profile(udd)
            clear_profile_locks(udd)
    except Exception:
        pass
