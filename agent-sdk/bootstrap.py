#!/usr/bin/env python3
"""Install agent-sdk into a local venv."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def main() -> None:
    venv = ROOT / ".venv"
    py = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-U", "pip", "setuptools", "wheel"], check=True)
    subprocess.run([str(py), "-m", "pip", "install", "-e", str(ROOT)], check=True)
    print(f"Installed. Activate: {venv}")

if __name__ == "__main__":
    main()
