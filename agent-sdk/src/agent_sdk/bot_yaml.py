"""Load bot.yaml metadata."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class BotMeta(BaseModel):
    id: str
    name: str
    network: str
    version: str = "0.1.0"
    entry: str = "app:app"
    default_port: int = 7411
    description: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


def load_bot_yaml(path: Path | str) -> BotMeta:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    known = {"id", "name", "network", "version", "entry", "default_port", "description"}
    extra = {k: v for k, v in data.items() if k not in known}
    return BotMeta(**{k: data[k] for k in known if k in data}, extra=extra)
