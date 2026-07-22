"""Normalize scraped post payloads from browser-use / Kimi output."""

from __future__ import annotations

import json
import re
from typing import Any

IG_POST_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)


def is_agent_noise(text: str | None) -> bool:
    """True when text looks like browser-use AgentOutput / thought dump, not a caption."""
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    if not t:
        return False
    markers = (
        "AgentOutput(",
        "ActionModel(",
        "thinking=",
        "evaluation_previous_goal=",
        "memory=",
        "next_goal=",
        "current_state=",
    )
    return any(m in t for m in markers) or t.startswith("[AgentOutput")


def canonicalize_ig_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    m = IG_POST_URL_RE.search(url.strip())
    if not m:
        return None
    kind = "reel" if "/reel" in m.group(0).lower() else "p"
    if "/tv/" in m.group(0).lower():
        kind = "tv"
    return f"https://www.instagram.com/{kind}/{m.group(1)}/"


def extract_ig_urls(text: str) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in IG_POST_URL_RE.finditer(text):
        url = canonicalize_ig_url(m.group(0))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _try_load_posts_blob(text: str) -> list[dict[str, Any]] | None:
    """Parse the first valid JSON object/list that contains Instagram posts."""
    if not text or not isinstance(text, str) or is_agent_noise(text):
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch not in "{[":
            continue
        try:
            data, _ = decoder.raw_decode(cleaned, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("posts"), list):
            posts = [p for p in data["posts"] if isinstance(p, dict)]
            if posts:
                return posts
        if isinstance(data, list) and data and all(isinstance(p, dict) for p in data):
            if any(p.get("post_url") or p.get("caption") for p in data):
                return list(data)
    return None


def _post_richness(post: dict[str, Any]) -> int:
    score = 0
    if post.get("post_url"):
        score += 10
    cap = post.get("caption") or ""
    if cap and not is_agent_noise(str(cap)):
        score += min(len(str(cap)), 400)
    for key in ("likes", "views", "comments_count", "username", "post_type"):
        if post.get(key) not in (None, "", []):
            score += 2
    if post.get("relevance_score") is not None:
        score += 5
    if post.get("liked") in (True, "true", 1, "1"):
        score += 3
    if post.get("followed") in (True, "true", 1, "1"):
        score += 3
    return score


def merge_posts(*batches: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Merge post batches by URL; keep the richest record per URL; drop noise."""
    by_url: dict[str, dict[str, Any]] = {}
    for batch in batches:
        for post in normalize_posts(batch or []):
            url = canonicalize_ig_url(post.get("post_url"))
            if not url:
                continue
            post = {**post, "post_url": url}
            prev = by_url.get(url)
            if not prev:
                by_url[url] = post
                continue
            # Prefer richer metadata, but never lose true liked/followed flags
            winner = post if _post_richness(post) > _post_richness(prev) else prev
            loser = prev if winner is post else post
            for flag in ("liked", "followed"):
                if loser.get(flag) in (True, "true", 1, "1"):
                    winner[flag] = True
            by_url[url] = winner
    return list(by_url.values())


def normalize_posts(posts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Expand wrapped raw_text JSON blobs into a flat list of real IG posts."""
    if not posts:
        return []

    out: list[dict[str, Any]] = []
    for item in posts:
        if not isinstance(item, dict):
            continue

        url = canonicalize_ig_url(item.get("post_url"))
        caption = item.get("caption")
        raw = item.get("raw_text")

        if url:
            clean = dict(item)
            clean["post_url"] = url
            if is_agent_noise(str(caption or "")):
                clean["caption"] = ""
            if is_agent_noise(str(raw or "")):
                clean.pop("raw_text", None)
            out.append(clean)
            continue

        for blob in (raw, caption):
            if isinstance(blob, str) and not is_agent_noise(blob):
                nested = _try_load_posts_blob(blob)
                if nested:
                    out.extend(nested)
                    break
                urls = extract_ig_urls(blob)
                if urls:
                    for u in urls:
                        out.append({"post_url": u, "caption": "", "raw_text": None})
                    break

    # Deduplicate by URL; drop anything still without a real post/reel URL
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for post in out:
        url = canonicalize_ig_url(post.get("post_url"))
        if not url or url in seen:
            continue
        seen.add(url)
        clean = dict(post)
        clean["post_url"] = url
        if is_agent_noise(str(clean.get("caption") or "")):
            clean["caption"] = ""
        deduped.append(clean)
    return deduped
