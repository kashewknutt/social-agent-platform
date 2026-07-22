"""Offline heuristic filter for development/testing without API calls."""

from __future__ import annotations

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
    "freelancer",
    "agency",
    "launch",
    "build",
    "ship",
    "indie",
    "nocode",
    "no-code",
    "ai",
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


def score_one_offline(
    post: dict[str, Any],
    idx: int,
    agency_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score a single post (always returns a score; caller applies threshold)."""
    caption = (post.get("caption") or post.get("raw_text") or "").lower()
    score = 40
    reasons: list[str] = []

    for kw in RELEVANT_KEYWORDS:
        if kw in caption:
            score += 12
            reasons.append(f"matches '{kw}'")

    for kw in IRRELEVANT_KEYWORDS:
        if kw in caption:
            score -= 25
            reasons.append(f"irrelevant '{kw}'")

    if post.get("post_type") == "reel" and any(
        k in caption for k in ("developer", "software", "startup", "mvp", "founder")
    ):
        score += 10
        reasons.append("reel + founder/tech signal")

    if post.get("post_url"):
        score += 5

    score = max(0, min(100, score))
    brand = agency_context.get("brand_name", "agency") if agency_context else "agency"
    return {
        **post,
        "post_index": idx,
        "relevance_score": score,
        "reason": "; ".join(reasons) or "no strong keyword match",
        "adaptable_hook": f"Adapt for {brand}",
        "kept": False,  # filled by caller
    }


def score_posts_offline(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    threshold: int = 60,
) -> list[dict[str, Any]]:
    """Keyword-based relevance scoring when Kimi API is unavailable."""
    scored: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        item = score_one_offline(post, idx, agency_context)
        if item["relevance_score"] < threshold:
            continue
        item["kept"] = True
        scored.append(item)
    return scored


def score_all_posts_offline(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    threshold: int = 60,
) -> list[dict[str, Any]]:
    """Score every post and mark kept vs rejected."""
    out: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        item = score_one_offline(post, idx, agency_context)
        item["kept"] = item["relevance_score"] >= threshold
        out.append(item)
    return out
