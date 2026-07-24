"""Offline heuristic filter for development/testing without API calls."""

from __future__ import annotations

from typing import Any

from ig_agent.format_gate import enrich_post_identity, score_format_offline

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
    "validation",
    "podcast",
    "interview",
    "advice",
    "lesson",
    "cofounder",
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

# People-first cues boost topical score when present in caption/description.
PEOPLE_KEYWORDS = [
    "podcast",
    "interview",
    "microphone",
    "talking",
    "speaking",
    "advice",
    "lesson",
    "founder story",
    "q&a",
    "explain",
    "walkthrough",
]


def score_one_offline(
    post: dict[str, Any],
    idx: int,
    agency_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score a single post (always returns a score; caller applies threshold)."""
    post = enrich_post_identity(post)
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

    if any(k in caption for k in PEOPLE_KEYWORDS):
        score += 12
        reasons.append("people-first cue")

    if post.get("post_url"):
        score += 5

    score = max(0, min(100, score))
    brand = agency_context.get("brand_name", "agency") if agency_context else "agency"
    item = {
        **post,
        "post_index": idx,
        "relevance_score": score,
        "reason": "; ".join(reasons) or "no strong keyword match",
        "adaptable_hook": f"Adapt for {brand}",
        "kept": False,  # filled by caller
    }
    # Attach offline format fields for callers that skip the full gate.
    fmt = score_format_offline(
        item,
        preferred_formats=list((agency_context or {}).get("preferred_formats") or []),
    )
    item.update(
        {
            "content_format": fmt.get("content_format"),
            "human_present": fmt.get("human_present"),
            "spoken_or_instructional": fmt.get("spoken_or_instructional"),
            "format_score": fmt.get("format_score"),
            "format_reason": fmt.get("format_reason"),
            "format_kept": fmt.get("format_kept"),
        }
    )
    return item


def score_posts_offline(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    threshold: int = 60,
) -> list[dict[str, Any]]:
    """Keyword-based relevance scoring when Kimi API is unavailable."""
    from ig_agent.format_gate import apply_format_gate

    scored: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        item = score_one_offline(post, idx, agency_context)
        if item["relevance_score"] < threshold:
            continue
        item["kept"] = True
        scored.append(item)
    require = str((agency_context or {}).get("research_mode") or "people_first").lower() in {
        "people_first",
        "people",
        "people-first",
        "",
    }
    gated = apply_format_gate(
        scored,
        preferred_formats=list((agency_context or {}).get("preferred_formats") or []),
        require_format=require,
    )
    return [p for p in gated if p.get("kept")]


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
