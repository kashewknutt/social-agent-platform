"""Track hashtag/phrase searches and avoid reusing seeds within a cooldown window."""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ig_agent.config import DATA_DIR

logger = logging.getLogger("ig_agent.hashtag_rotation")

HISTORY_PATH = DATA_DIR / "hashtag_search_history.jsonl"
PHRASE_HISTORY_PATH = DATA_DIR / "phrase_search_history.jsonl"

# Focused people-first discovery tags when Direction hashtags were all searched recently.
# Intentionally excludes meme/viral/money-bait tags.
DISCOVERY_HASHTAGS = (
    "founderstory",
    "founderjourney",
    "founderadvice",
    "startupadvice",
    "startuptips",
    "buildinpublic",
    "saasfounder",
    "b2bsaas",
    "productfounder",
    "mvp",
    "mvplaunch",
    "startupindia",
    "indianfounders",
    "founderpodcast",
    "businesspodcast",
    "startupfounders",
    "foundermindset",
    "founderlife",
    "saas",
    "indiehacker",
    "productvalidation",
    "startuplessons",
    "techfounder",
    "nontechnicalfounder",
    "founderstories",
)

DEFAULT_DISCOVERY_PHRASES = (
    "founder advice",
    "MVP lessons",
    "startup mistakes",
    "product validation",
    "non-technical founder",
    "founder interview",
    "startup podcast",
    "SaaS founder story",
    "technical cofounder advice",
    "MVP launch lessons",
)


def normalize_hashtag(tag: str) -> str:
    raw = (tag or "").strip().lower()
    raw = raw.lstrip("#")
    raw = re.sub(r"[^a-z0-9_]", "", raw.replace(" ", ""))
    return raw


def normalize_phrase(phrase: str) -> str:
    raw = (phrase or "").strip().lower()
    raw = re.sub(r"\s+", " ", raw)
    return raw


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00").replace("+00:00", ""))
    except Exception:
        return None


def _load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            tag = normalize_hashtag(str(row.get("tag") or row.get("phrase") or ""))
            # Phrases keep spaces; re-read phrase key if present.
            if row.get("phrase"):
                tag = normalize_phrase(str(row.get("phrase") or ""))
            elif "tag" in row:
                tag = normalize_hashtag(str(row.get("tag") or ""))
            ts = _parse_ts(str(row.get("ts") or ""))
            if tag and ts:
                rows.append({"tag": tag, "ts": ts})
    except Exception:
        logger.exception("Failed to read history %s", path)
    return rows


def load_history() -> list[dict[str, Any]]:
    return _load_history(HISTORY_PATH)


def recent_hashtags(within_days: float = 2.0) -> set[str]:
    cutoff = datetime.now() - timedelta(days=within_days)
    out: set[str] = set()
    for row in load_history():
        if row["ts"] >= cutoff:
            out.add(row["tag"])
    return out


def last_used_map() -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for row in load_history():
        tag = row["tag"]
        ts = row["ts"]
        if tag not in latest or ts > latest[tag]:
            latest[tag] = ts
    return latest


def record_hashtag_search(tag: str, *, source: str = "ingest") -> None:
    norm = normalize_hashtag(tag)
    if not norm:
        return
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "tag": norm,
        "ts": datetime.now().isoformat(),
        "source": source,
    }
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    logger.info("Recorded hashtag search: #%s", norm)


def recent_phrases(within_days: float = 2.0) -> set[str]:
    cutoff = datetime.now() - timedelta(days=within_days)
    out: set[str] = set()
    for row in _load_history(PHRASE_HISTORY_PATH):
        if row["ts"] >= cutoff:
            out.add(row["tag"])
    return out


def last_used_phrase_map() -> dict[str, datetime]:
    latest: dict[str, datetime] = {}
    for row in _load_history(PHRASE_HISTORY_PATH):
        tag = row["tag"]
        ts = row["ts"]
        if tag not in latest or ts > latest[tag]:
            latest[tag] = ts
    return latest


def record_phrase_search(phrase: str, *, source: str = "ingest") -> None:
    norm = normalize_phrase(phrase)
    if not norm:
        return
    PHRASE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "phrase": norm,
        "ts": datetime.now().isoformat(),
        "source": source,
    }
    with PHRASE_HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    logger.info("Recorded phrase search: %s", norm)


def _dedupe_preserve_order(tags: list[str], *, normalize=normalize_hashtag) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        norm = normalize(tag)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def pick_hashtags_for_session(
    configured: list[str] | None,
    *,
    max_pick: int = 1,
    within_days: float = 2.0,
    include_discovery: bool = True,
) -> tuple[list[str], str]:
    """Return fresh hashtag(s) for this ingest run and a human-readable note."""
    configured_norm = _dedupe_preserve_order(list(configured or []))
    pool = list(configured_norm)
    if include_discovery:
        pool.extend(DISCOVERY_HASHTAGS)
    pool = _dedupe_preserve_order(pool)

    if not pool:
        return [], "no hashtags configured"

    recent = recent_hashtags(within_days)
    fresh = [t for t in pool if t not in recent]

    skipped = [t for t in configured_norm if t in recent]
    skipped_note = ""
    if skipped:
        skipped_note = f" (skipped recently used: {', '.join('#' + t for t in skipped[:5])})"

    if fresh:
        random.shuffle(fresh)
        picked = fresh[: max(1, max_pick)]
        note = f"Using fresh hashtag(s): {', '.join('#' + t for t in picked)}{skipped_note}"
        return picked, note

    # Everything in pool was used inside the window — pick least recently used.
    used = last_used_map()
    ranked = sorted(pool, key=lambda t: used.get(t, datetime.min))
    picked = ranked[: max(1, max_pick)]
    note = (
        f"All hashtags used within {within_days:g} days — reusing least recent: "
        f"{', '.join('#' + t for t in picked)}"
    )
    return picked, note


def pick_phrases_for_session(
    configured: list[str] | None,
    *,
    max_pick: int = 1,
    within_days: float = 2.0,
    include_defaults: bool = True,
) -> tuple[list[str], str]:
    """Return fresh search phrase(s) for this ingest run."""
    configured_norm = _dedupe_preserve_order(
        list(configured or []), normalize=normalize_phrase
    )
    pool = list(configured_norm)
    if include_defaults:
        pool.extend(DEFAULT_DISCOVERY_PHRASES)
    pool = _dedupe_preserve_order(pool, normalize=normalize_phrase)

    if not pool:
        return [], "no discovery phrases configured"

    recent = recent_phrases(within_days)
    fresh = [t for t in pool if t not in recent]
    skipped = [t for t in configured_norm if t in recent]
    skipped_note = ""
    if skipped:
        skipped_note = f" (skipped recently used: {', '.join(skipped[:3])})"

    if fresh:
        random.shuffle(fresh)
        picked = fresh[: max(1, max_pick)]
        note = f"Using fresh phrase(s): {', '.join(picked)}{skipped_note}"
        return picked, note

    used = last_used_phrase_map()
    ranked = sorted(pool, key=lambda t: used.get(t, datetime.min))
    picked = ranked[: max(1, max_pick)]
    note = (
        f"All phrases used within {within_days:g} days — reusing least recent: "
        f"{', '.join(picked)}"
    )
    return picked, note


def prune_history(keep_days: float = 14.0) -> int:
    """Drop history entries older than keep_days (housekeeping)."""
    removed = 0
    for path in (HISTORY_PATH, PHRASE_HISTORY_PATH):
        if not path.exists():
            continue
        cutoff = datetime.now() - timedelta(days=keep_days)
        kept: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                ts = _parse_ts(str(row.get("ts") or ""))
            except Exception:
                removed += 1
                continue
            if ts and ts >= cutoff:
                kept.append(line)
            else:
                removed += 1
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return removed
