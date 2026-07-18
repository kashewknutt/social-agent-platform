"""Offline heuristic filter for development/testing without API calls."""

from __future__ import annotations

import re
from typing import Any

RELEVANT_KEYWORDS = [
    "software",
    "saas",
    "developer",
    "coding",
    "startup",
    "app",
    "tech",
    "engineer",
    "crm",
    "mvp",
    "architecture",
    "b2b",
    "api",
    "deploy",
    "faang",
    "founder",
    "product",
]

IRRELEVANT_KEYWORDS = [
    "skincare",
    "makeup",
    "beauty",
    "fashion",
    "recipe",
    "fitness",
    "dance",
]


def score_posts_offline(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    threshold: int = 60,
) -> list[dict[str, Any]]:
    """Keyword-based relevance scoring when Kimi API is unavailable."""
    scored: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        caption = (post.get("caption") or "").lower()
        score = 30
        reasons: list[str] = []

        for kw in RELEVANT_KEYWORDS:
            if kw in caption:
                score += 12
                reasons.append(f"matches '{kw}'")

        for kw in IRRELEVANT_KEYWORDS:
            if kw in caption:
                score -= 25
                reasons.append(f"irrelevant '{kw}'")

        if post.get("post_type") == "reel" and any(k in caption for k in ("developer", "software", "startup")):
            score += 10

        score = max(0, min(100, score))
        if score < threshold:
            continue

        scored.append(
            {
                **post,
                "post_index": idx,
                "relevance_score": score,
                "reason": "; ".join(reasons) or "general tech relevance",
                "adaptable_hook": f"Adapt for {agency_context.get('brand_name', 'agency') if agency_context else 'agency'}",
            }
        )
    return scored
