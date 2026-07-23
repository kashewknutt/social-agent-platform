"""Deterministic spam/clickbait caption filter — hard-reject before relevance scoring."""

from __future__ import annotations

import re
from typing import Any

# Substring matches (lowercase). Keep phrases specific enough to avoid false
# positives on legit founder/build-in-public content.
SPAM_PHRASES: tuple[str, ...] = (
    "read caption",
    "read the caption",
    "read full caption",
    "caption for details",
    "if you're a student",
    "if you are a student",
    "if your a student",
    "students can earn",
    "earn side income",
    "side income",
    "passive income",
    "make money online",
    "make money from home",
    "make money fast",
    "earn money online",
    "earn from home",
    "work from home",
    "work from anywhere",
    "no experience needed",
    "no experience required",
    "no prior experience",
    "dm me to start",
    "dm me for details",
    "dm for details",
    "message me to start",
    "message me for details",
    "comment 'yes'",
    'comment "yes"',
    "comment yes to",
    "type interested",
    "comment interested",
    "link in bio to earn",
    "link in bio for details",
    "swipe up to earn",
    "limited slots",
    "limited spots",
    "only 5 spots",
    "only 10 spots",
    "follow these steps",
    "follow the steps below",
    "follow these simple steps",
    "here's how to earn",
    "here is how to earn",
    "how to earn",
    "how i made",
    "how i earned",
    "double tap if you agree",
    "tag a friend who needs this",
    "tag someone who needs this",
    "save this post",
    "save for later",
    "share with someone",
    "affiliate marketing",
    "drop shipping",
    "dropshipping",
    "forex trading",
    "crypto signals",
    "get rich quick",
    "financial freedom in",
    "quit your 9 to 5",
    "quit your 9-5",
    "replace your salary",
    "₹ per day",
    "$ per day",
    "per day from home",
    "without investment",
    "zero investment",
    "guaranteed income",
    "guaranteed profit",
    "100% profit",
    "click the link",
    "click link in bio",
    "whatsapp me",
    "telegram me",
    "join my team",
    "join our team",
    "be your own boss",
    "laptop lifestyle",
    "quick money",
    "easy money",
    "free course",
    "free masterclass",
    "comment below for",
    "send me a dm",
    "send a dm",
    "finding students",
    "students who want to earn",
    "earn online",
    "make money digitally",
    "dead-end 9-5",
    "dead end 9-5",
    "comment how",
    "comment 'how'",
    'comment "how"',
    "comment ?how",
    "comment how to know",
    "comment how to",
    "all you need is a phone",
    "follow me so i can dm",
    "so i can dm you",
    "can dm you the",
    "free guide",
    "secret strategy",
    "unlimited income",
    "earn $",
    "earn ₹",
    "per week",
    "digital product",
    "break into the online space",
)

# Three or more numbered steps (1. 2. 3.) or emoji steps (1️⃣2️⃣3️⃣) or
# "Step 1" / "Step 2" / "Step 3" — classic listicle bait format.
_STEP_DOT_RE = re.compile(r"(?:^|\s)[1-9][.)]\s", re.MULTILINE)
_STEP_EMOJI_RE = re.compile(r"[1-9]️⃣")
_STEP_WORD_RE = re.compile(r"\bstep\s+[1-9]\b", re.IGNORECASE)
# "comment HOW" / "comment 'yes'" engagement bait (any casing).
_COMMENT_BAIT_RE = re.compile(r"comment\s+['\"]?[a-z]{2,12}['\"]?\s*(to know|for|to get)", re.IGNORECASE)


def _normalize_caption(caption: str) -> str:
    """Lowercase and fold curly quotes so phrase matching is reliable."""
    text = caption.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    return text.lower()


def _caption_text(post: dict[str, Any]) -> str:
    return (post.get("caption") or post.get("raw_text") or "").strip()


def _has_numbered_step_bait(caption: str) -> bool:
    dot_hits = len(_STEP_DOT_RE.findall(caption))
    if dot_hits >= 3:
        return True
    emoji_hits = len(_STEP_EMOJI_RE.findall(caption))
    if emoji_hits >= 3:
        return True
    word_steps = set(_STEP_WORD_RE.findall(caption.lower()))
    return len(word_steps) >= 3


def is_spam_post(
    post: dict[str, Any],
    *,
    max_caption_len: int = 900,
) -> tuple[bool, str]:
    """Return (True, reason) if this post looks like spam/clickbait/MLM bait."""
    caption = _caption_text(post)
    if not caption:
        return False, ""

    lower = _normalize_caption(caption)

    for phrase in SPAM_PHRASES:
        if phrase in lower:
            return True, f"spam phrase: {phrase!r}"

    if len(caption) > max_caption_len:
        return True, f"caption too long ({len(caption)} chars)"

    if _has_numbered_step_bait(caption):
        return True, "numbered step bait format"

    if _COMMENT_BAIT_RE.search(lower):
        return True, "comment engagement bait"

    return False, ""


def mark_spam_rejected(post: dict[str, Any], idx: int, reason: str) -> dict[str, Any]:
    """Build a scored post dict for a hard-rejected spam item."""
    return {
        **post,
        "post_index": idx,
        "relevance_score": 0,
        "reason": f"Rejected: spam pattern ({reason})",
        "adaptable_hook": "",
        "kept": False,
    }
