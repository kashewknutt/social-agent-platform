"""Normalize scraped post payloads from browser-use / Kimi output."""

from __future__ import annotations

import json
import re
from typing import Any


def _try_load_posts_blob(text: str) -> list[dict[str, Any]] | None:
    """Parse the first valid JSON object/list that contains Instagram posts."""
    if not text or not isinstance(text, str):
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
            # Bare list of post-like dicts
            if any(p.get("post_url") or p.get("caption") for p in data):
                return list(data)
    return None


def normalize_posts(posts: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Expand wrapped raw_text JSON blobs into a flat list of post dicts."""
    if not posts:
        return []

    out: list[dict[str, Any]] = []
    for item in posts:
        if not isinstance(item, dict):
            continue

        # Already a normal post with a URL
        if item.get("post_url"):
            out.append(item)
            continue

        raw = item.get("raw_text") or item.get("caption") or ""
        nested = _try_load_posts_blob(raw) if isinstance(raw, str) else None
        if nested:
            out.extend(nested)
            continue

        # Keep caption-only items that aren't wrappers
        if item.get("caption") and "raw_text" not in item:
            out.append(item)
            continue

        out.append(item)

    # Deduplicate by URL when present
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for post in out:
        url = str(post.get("post_url") or "").strip()
        if url:
            if url in seen:
                continue
            seen.add(url)
        deduped.append(post)
    return deduped
