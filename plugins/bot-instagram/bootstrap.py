#!/usr/bin/env python3
"""Register this plugin with social-agent-platform.

If the platform is missing next to this repo, clone it, then symlink/copy
this bot into platform/plugins/bot-instagram.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
PARENT = PLUGIN_ROOT.parent
PLATFORM_NAME = "social-agent-platform"
PLATFORM_REPO = "https://github.com/kashewknutt/social-agent-platform.git"


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def find_platform() -> Path | None:
    candidates = [
        PARENT / PLATFORM_NAME,
        PLUGIN_ROOT / PLATFORM_NAME,
        Path.cwd().parent / PLATFORM_NAME,
    ]
    for c in candidates:
        if (c / "platform.manifest.yaml").exists():
            return c.resolve()
    return None


def ensure_platform() -> Path:
    existing = find_platform()
    if existing:
        print(f"✓ Platform found at {existing}")
        return existing
    dest = PARENT / PLATFORM_NAME
    print(f"→ Cloning platform into {dest}")
    run(["git", "clone", "--depth", "1", PLATFORM_REPO, str(dest)])
    return dest


def link_plugin(platform: Path) -> Path:
    plugins = platform / "plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    target = plugins / "bot-instagram"
    if target.exists() or target.is_symlink():
        print(f"✓ Plugin already linked at {target}")
        return target
    # Prefer symlink for local dev; copy if symlink fails (Windows without privilege)
    try:
        target.symlink_to(PLUGIN_ROOT, target_is_directory=True)
        print(f"✓ Symlinked plugin → {target}")
    except OSError:
        shutil.copytree(PLUGIN_ROOT, target, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__"))
        print(f"✓ Copied plugin → {target}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Instagram bot as a platform plugin")
    parser.add_argument("--no-install", action="store_true")
    args = parser.parse_args()

    platform = ensure_platform()
    link_plugin(platform)

    # Delegate install to platform setup for the instagram profile
    setup = platform / "setup.py"
    cmd = [sys.executable, str(setup), "--profile", "instagram"]
    if args.no_install:
        cmd.append("--no-install")
    print("→ Running platform setup for profile=instagram")
    run(cmd, cwd=platform)
    print("\nOpen the PLATFORM in Cursor (not this plugin repo):")
    print(f"  {platform}")
    print(f"  or {platform / 'social-agents.code-workspace'}")


if __name__ == "__main__":
    main()
