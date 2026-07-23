"""Kimi (Moonshot AI) LLM client wrappers."""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from ig_agent.config import Settings, get_settings


class KimiClient:
    """Thin wrapper around the OpenAI-compatible Moonshot API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if not self.settings.moonshot_api_key:
            raise ValueError(
                "MOONSHOT_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        self._client = OpenAI(
            api_key=self.settings.moonshot_api_key,
            base_url=self.settings.kimi_base_url,
            # Hard cap so a slow/stuck Moonshot call can't hang scoring/drafting
            # forever — callers fall back to offline logic on timeout.
            timeout=self.settings.kimi_request_timeout_s,
            max_retries=1,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, str] | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": model or self.settings.kimi_filter_model,
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
        """Quick API connectivity check."""
        return self.chat(
            [
                {
                    "role": "user",
                    "content": "Reply with exactly: Kimi API connected.",
                }
            ],
            model=model or self.settings.kimi_filter_model,
        )


def get_browser_llm(settings: Settings | None = None):
    """Return a browser-use compatible ChatOpenAI instance for Moonshot.

    Kimi k2.6/k3 fix temperature=1 and frequency_penalty=0; other values 400.
    """
    from browser_use import ChatOpenAI

    cfg = settings or get_settings()
    if not cfg.moonshot_api_key:
        raise ValueError("MOONSHOT_API_KEY is not set.")

    return ChatOpenAI(
        model=cfg.kimi_browser_model,
        base_url=cfg.kimi_base_url,
        api_key=cfg.moonshot_api_key,
        temperature=1.0,
        frequency_penalty=0.0,
        add_schema_to_system_prompt=True,
        remove_min_items_from_schema=True,
        remove_defaults_from_schema=True,
        dont_force_structured_output=True,
    )
