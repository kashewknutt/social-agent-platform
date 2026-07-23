"""Relevance filtering of scraped Instagram posts using Kimi."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ig_agent.config import AGENCY_CONTEXT_PATH, FILTERED_DIR, RAW_DIR, Settings, get_settings
from ig_agent.llm import KimiClient
from ig_agent.offline import score_all_posts_offline, score_posts_offline
from ig_agent.posts import normalize_posts
from ig_agent.spam_filter import is_spam_post, mark_spam_rejected

logger = logging.getLogger("ig_agent.filter")

# Called with (scored_so_far, total_posts) after each batch finishes so the
# caller can push progressive updates (e.g. Fleet's live Score/Gate column)
# instead of one giant blocking call with zero feedback in between.
ScoreProgressFn = Callable[[list[dict[str, Any]], int], None]


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
        "HARD REJECT (score 0, never keep): spam, clickbait, side-hustle bait, MLM/affiliate "
        "schemes, 'read caption' / 'if you're a student' / 'earn side income' / 'follow these "
        "steps' / numbered how-to-earn listicles, overly long AI-generated caption walls, "
        "and 'DM me' / 'comment yes' engagement bait — regardless of likes or views. "
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


def _score_batch(
    posts: list[dict[str, Any]],
    ctx: dict[str, Any],
    cfg: Settings,
    client: KimiClient,
    *,
    index_offset: int,
) -> list[dict[str, Any]]:
    """Score one small batch of posts against Kimi; offline-score any that fail."""
    try:
        response = client.chat_json(
            _build_filter_prompt(ctx, posts),
            model=cfg.kimi_filter_model,
        )
    except Exception as exc:
        logger.warning(
            "Kimi filter batch failed (%s post(s), offset %s): %s — offline-scoring batch",
            len(posts),
            index_offset,
            exc,
        )
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

    from ig_agent.offline import score_one_offline

    batch_scored: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        if idx in by_idx:
            batch_scored.append(by_idx[idx])
        else:
            item = score_one_offline(post, idx, ctx)
            item["kept"] = item["relevance_score"] >= cfg.relevance_threshold
            batch_scored.append(item)
    return batch_scored


def score_all_posts(
    posts: list[dict[str, Any]],
    agency_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    offline: bool = False,
    on_progress: ScoreProgressFn | None = None,
) -> list[dict[str, Any]]:
    """Score every post in small batches; each item has relevance_score + kept bool.

    Batching (settings.filter_batch_size) plus an optional on_progress callback
    means a single slow/failed Kimi call only blocks its own batch — not the
    whole run — and callers can push live updates after every batch instead of
    waiting on one all-or-nothing call.
    """
    cfg = settings or get_settings()
    ctx = agency_context or load_agency_context()
    posts = normalize_posts(posts)
    total = len(posts)
    if not posts:
        return []

    # Hard-reject spam/clickbait before spending Kimi calls on them.
    clean_posts: list[dict[str, Any]] = []
    clean_indices: list[int] = []
    spam_scored: list[dict[str, Any]] = []
    for idx, post in enumerate(posts):
        is_spam, spam_reason = is_spam_post(post, max_caption_len=cfg.spam_max_caption_len)
        if is_spam:
            spam_scored.append(mark_spam_rejected(post, idx, spam_reason))
        else:
            clean_posts.append(post)
            clean_indices.append(idx)

    if spam_scored:
        logger.info(
            "Spam filter rejected %s/%s post(s) before scoring",
            len(spam_scored),
            total,
        )

    if not clean_posts:
        if on_progress:
            on_progress(spam_scored, total)
        return spam_scored

    if offline or not cfg.moonshot_api_key:
        scored = score_all_posts_offline(clean_posts, ctx, cfg.relevance_threshold)
        # Map batch-local indices back to original post indices.
        for local_idx, item in enumerate(scored):
            item["post_index"] = clean_indices[local_idx]
        all_scored = spam_scored + scored
        all_scored.sort(key=lambda p: int(p.get("post_index", 0)))
        if on_progress:
            on_progress(all_scored, total)
        return all_scored

    client = KimiClient(cfg)
    batch_size = max(1, cfg.filter_batch_size)
    scored_clean: list[dict[str, Any]] = []
    for start in range(0, len(clean_posts), batch_size):
        batch = clean_posts[start : start + batch_size]
        batch_indices = clean_indices[start : start + batch_size]
        # Re-index each batch to 0..len(batch)-1 for the prompt, then map back.
        batch_scored = _score_batch(batch, ctx, cfg, client, index_offset=start)
        for local_idx, item in enumerate(batch_scored):
            item["post_index"] = batch_indices[local_idx]
        scored_clean.extend(batch_scored)
        merged = spam_scored + scored_clean
        merged.sort(key=lambda p: int(p.get("post_index", 0)))
        if on_progress:
            on_progress(merged, total)
    all_scored = spam_scored + scored_clean
    all_scored.sort(key=lambda p: int(p.get("post_index", 0)))
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
    on_progress: ScoreProgressFn | None = None,
) -> Path:
    """Filter a single raw scrape file and write filtered output (kept + all scored)."""
    cfg = settings or get_settings()
    raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
    posts = raw_data.get("posts", [])
    if not posts and isinstance(raw_data.get("data"), list):
        posts = raw_data["data"]
    posts = normalize_posts(posts)

    all_scored = score_all_posts(posts, agency_context, cfg, offline=offline, on_progress=on_progress)
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
