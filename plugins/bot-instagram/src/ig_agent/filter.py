"""Relevance filtering of scraped Instagram posts using Kimi."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ig_agent.config import AGENCY_CONTEXT_PATH, FILTERED_DIR, RAW_DIR, Settings, get_settings
from ig_agent.llm import KimiClient
from ig_agent.offline import score_posts_offline


def load_agency_context(path: Path | None = None) -> dict[str, Any]:
    ctx_path = path or AGENCY_CONTEXT_PATH
    return json.loads(ctx_path.read_text(encoding="utf-8"))


def _build_filter_prompt(agency_context: dict[str, Any], posts: list[dict[str, Any]]) -> list[dict[str, str]]:
    system = (
        "You are a B2B social media analyst for a software development agency. "
        "Score each Instagram post for relevance to the agency's content strategy. "
        "Return JSON with shape: "
        '{"results": [{"post_index": 0, "relevance_score": 85, "reason": "...", '
        '"adaptable_hook": "..."}]}. '
        "Scores are 0-100. Only include posts with score >= 60."
    )
    user = (
        f"Agency profile:\n{json.dumps(agency_context, indent=2)}\n\n"
        f"Posts to evaluate:\n{json.dumps(posts, indent=2)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def score_posts(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    offline: bool = False,
) -> list[dict[str, Any]]:
    """Score posts and return enriched results above the relevance threshold."""
    cfg = settings or get_settings()
    ctx = agency_context or load_agency_context()
    if not posts:
        return []

    if offline or not cfg.moonshot_api_key:
        return score_posts_offline(posts, ctx, cfg.relevance_threshold)

    client = KimiClient(cfg)
    response = client.chat_json(
        _build_filter_prompt(ctx, posts),
        model=cfg.kimi_filter_model,
    )

    scored: list[dict[str, Any]] = []
    results = response.get("results", [])
    for item in results:
        idx = item.get("post_index", -1)
        score = item.get("relevance_score", 0)
        if score < cfg.relevance_threshold or idx < 0 or idx >= len(posts):
            continue
        enriched = {**posts[idx], **item}
        scored.append(enriched)
    return scored


def filter_raw_file(
    raw_path: Path,
    agency_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    offline: bool = False,
) -> Path:
    """Filter a single raw scrape file and write filtered output."""
    cfg = settings or get_settings()
    raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
    posts = raw_data.get("posts", [])
    if not posts and isinstance(raw_data.get("data"), list):
        posts = raw_data["data"]

    filtered = score_posts(posts, agency_context, cfg, offline=offline)
    output = {
        "source_file": raw_path.name,
        "filtered_at": datetime.now().isoformat(),
        "post_count": len(filtered),
        "posts": filtered,
    }
    out_path = FILTERED_DIR / f"filtered_{raw_path.stem}.json"
    out_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    return out_path


def filter_latest_raw(settings: Settings | None = None) -> Path | None:
    """Filter the most recent raw scrape file."""
    cfg = settings or get_settings()
    raw_files = sorted(RAW_DIR.glob("scraped_*.json"), key=lambda p: p.stat().st_mtime)
    if not raw_files:
        return None
    return filter_raw_file(raw_files[-1], settings=cfg)


def filter_all_raw(settings: Settings | None = None) -> list[Path]:
    """Filter all unfiltered raw files."""
    cfg = settings or get_settings()
    outputs: list[Path] = []
    for raw_path in sorted(RAW_DIR.glob("scraped_*.json")):
        filtered_name = f"filtered_{raw_path.stem}.json"
        if (FILTERED_DIR / filtered_name).exists():
            continue
        outputs.append(filter_raw_file(raw_path, settings=cfg))
    return outputs
