"""Moonshot / Kimi OpenAI-compatible client helpers."""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI


class KimiClient:
    """Thin wrapper around the OpenAI-compatible Moonshot API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("MOONSHOT_API_KEY", "")
        self.base_url = base_url or os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
        self.default_model = default_model or os.getenv("KIMI_FILTER_MODEL", "kimi-k2.6")
        if not self.api_key:
            raise ValueError(
                "MOONSHOT_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, str] | None = None,
    ) -> str:
        # Kimi k2.6/k3: omit temperature or use 1.0 only.
        kwargs: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if response_format is not None:
            kwargs["response_format"] = response_format
        completion = self._client.chat.completions.create(**kwargs)
        content = completion.choices[0].message.content
        if not content:
            raise RuntimeError("Kimi returned empty content")
        return content

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        text = self.chat(
            messages,
            model=model,
            response_format={"type": "json_object"},
        )
        return json.loads(text)

    def smoke_test(self, model: str | None = None) -> str:
        return self.chat(
            [{"role": "user", "content": "Reply with exactly: Kimi API connected."}],
            model=model or self.default_model,
        )


def browser_llm_kwargs(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Kwargs for browser-use ChatOpenAI compatible with Kimi fixed params."""
    return {
        "model": model or os.getenv("KIMI_BROWSER_MODEL", "kimi-k2.6"),
        "base_url": base_url or os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
        "api_key": api_key or os.getenv("MOONSHOT_API_KEY", ""),
        "temperature": 1.0,
        "frequency_penalty": 0.0,
        "add_schema_to_system_prompt": True,
        "remove_min_items_from_schema": True,
        "remove_defaults_from_schema": True,
        "dont_force_structured_output": True,
    }
