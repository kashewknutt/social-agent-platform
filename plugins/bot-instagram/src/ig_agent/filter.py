"""Relevance filtering of scraped Instagram posts using Kimi."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from ig_agent.config import AGENCY_CONTEXT_PATH, FILTERED_DIR, RAW_DIR, Settings, get_settings
from ig_agent.llm import KimiClient
from ig_agent.offline import score_all_posts_offline, score_posts_offline
from ig_agent.posts import normalize_posts


def load_agency_context(path: Path | None = None) -> dict[str, Any]:
    ctx_path = path or AGENCY_CONTEXT_PATH
    return json.loads(ctx_path.read_text(encoding="utf-8"))


def _build_filter_prompt(agency_context: dict[str, Any], posts: list[dict[str, Any]]) -> list[dict[str, str]]:
    compact = []
    for i, post in enumerate(posts):
        compact.append(
            {
                "post_index": i,
                "post_url": post.get("post_url"),
                "caption": (post.get("caption") or post.get("raw_text") or "")[:600],
                "likes": post.get("likes"),
                "views": post.get("views"),
                "comments_count": post.get("comments_count"),
                "post_type": post.get("post_type"),
                "username": post.get("username"),
            }
        )
    brand = agency_context.get("brand_name") or "the agency"
    region = agency_context.get("region") or ""
    region_note = ""
    if region:
        region_note = (
            f" {region} creators are a plus but NOT required — never reject solely "
            f"for missing {region} signal. Score founder/MVP/startup/business content "
            "high even from other regions if it fits the pillars."
        )
    system = (
        f"You are a B2B social media analyst for {brand}. "
        "Score EVERY Instagram post for relevance to the agency's content strategy. "
        "Be generous with founder-journey, business-struggle, MVP, startup, and "
        "build-in-public angles — these are core targets even when not explicitly about tech. "
        "Return ONLY JSON: "
        '{"results": [{"post_index": 0, "relevance_score": 85, "reason": "...", '
        '"adaptable_hook": "..."}]}. '
        "Include one result per input post (even low scores). Scores are 0-100."
        f"{region_note}"
    )
    user = (
        f"Agency profile:\n{json.dumps(agency_context, indent=2)}\n\n"
        f"Posts to evaluate:\n{json.dumps(compact, indent=2)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def score_all_posts(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    offline: bool = False,
) -> list[dict[str, Any]]:
    """Score every post; each item has relevance_score + kept bool."""
    cfg = settings or get_settings()
    ctx = agency_context or load_agency_context()
    posts = normalize_posts(posts)
    if not posts:
        return []

    if offline or not cfg.moonshot_api_key:
        return score_all_posts_offline(posts, ctx, cfg.relevance_threshold)

    client = KimiClient(cfg)
    try:
        response = client.chat_json(
            _build_filter_prompt(ctx, posts),
            model=cfg.kimi_filter_model,
        )
    except Exception:
        return score_all_posts_offline(posts, ctx, cfg.relevance_threshold)

    results = response.get("results", [])
    if not isinstance(results, list) or not results:
        return score_all_posts_offline(posts, ctx, cfg.relevance_threshold)

    by_idx: dict[int, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("post_index", -1))
            score = int(item.get("relevance_score", 0))
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(posts):
            continue
        by_idx[idx] = {
            **posts[idx],
            **item,
            "post_index": idx,
            "relevance_score": score,
            "kept": score >= cfg.relevance_threshold,
        }

    # Fill any missing indexes with offline scores
    all_scored: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        if idx in by_idx:
            all_scored.append(by_idx[idx])
        else:
            from ig_agent.offline import score_one_offline

            item = score_one_offline(post, idx, ctx)
            item["kept"] = item["relevance_score"] >= cfg.relevance_threshold
            all_scored.append(item)
    return all_scored


def score_posts(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    offline: bool = False,
) -> list[dict[str, Any]]:
    """Score posts and return only those above the relevance threshold."""
    return [p for p in score_all_posts(posts, agency_context, settings, offline=offline) if p.get("kept")]


def filter_raw_file(
    raw_path: Path,
    agency_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    offline: bool = False,
) -> Path:
    """Filter a single raw scrape file and write filtered output (kept + all scored)."""
    cfg = settings or get_settings()
    raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
    posts = raw_data.get("posts", [])
    if not posts and isinstance(raw_data.get("data"), list):
        posts = raw_data["data"]
    posts = normalize_posts(posts)

    all_scored = score_all_posts(posts, agency_context, cfg, offline=offline)
    kept = [p for p in all_scored if p.get("kept")]
    output = {
        "source_file": raw_path.name,
        "filtered_at": datetime.now().isoformat(),
        "threshold": cfg.relevance_threshold,
        "post_count": len(kept),
        "normalized_input_count": len(posts),
        "scored_count": len(all_scored),
        "posts": kept,
        "all_scored": all_scored,
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
