"""People-first format gate: prefer human-speaking / instructional content."""

from __future__ import annotations

import re
from typing import Any

# Formats we actively want for Valnee research.
PREFERRED_FORMATS = frozenset(
    {
        "talking_head",
        "microphone",
        "podcast_interview",
        "spoken_explanation",
        "demonstration",
        "q_and_a",
        "direct_instruction",
    }
)

# Caption / description cues that a real person is speaking or instructing.
_PEOPLE_POSITIVE: tuple[tuple[str, int, str], ...] = (
    ("podcast", 18, "podcast"),
    ("interview", 16, "podcast_interview"),
    ("microphone", 20, "microphone"),
    (" mic ", 14, "microphone"),
    ("into the mic", 20, "microphone"),
    ("talking head", 18, "talking_head"),
    ("speaking to", 12, "spoken_explanation"),
    ("speaking about", 12, "spoken_explanation"),
    ("let me explain", 14, "direct_instruction"),
    ("i'll explain", 14, "direct_instruction"),
    ("in this reel i", 12, "spoken_explanation"),
    ("founder story", 14, "talking_head"),
    ("founder advice", 14, "direct_instruction"),
    ("lesson learned", 12, "spoken_explanation"),
    ("what i learned", 12, "spoken_explanation"),
    ("q&a", 14, "q_and_a"),
    ("q and a", 14, "q_and_a"),
    ("ask me anything", 12, "q_and_a"),
    ("walkthrough", 12, "demonstration"),
    ("step by step", 10, "direct_instruction"),
    ("demonstrat", 12, "demonstration"),
    ("teaching", 10, "direct_instruction"),
    ("instruct", 10, "direct_instruction"),
    ("talking to camera", 18, "talking_head"),
    ("to camera", 10, "talking_head"),
    ("on camera", 10, "talking_head"),
    ("founder interview", 16, "podcast_interview"),
    ("startup podcast", 16, "podcast_interview"),
)

_PEOPLE_NEGATIVE: tuple[tuple[str, int], ...] = (
    ("aaaaaa", 25),
    ("meme", 18),
    ("codememes", 22),
    ("coding humor", 18),
    ("this part of my life is called", 22),
    ("ironman", 15),
    ("aesthetic", 10),
    ("lock in", 8),
    ("desk setup", 12),
    ("setup tour", 12),
    ("github contributions", 10),
    ("vs code", 8),
    ("vscode", 8),
    ("faceless", 20),
    ("text on screen only", 18),
    ("slideshow", 16),
    ("carousel of tips", 10),
    ("motion graphics only", 16),
)

_USERNAME_FROM_OG_RE = re.compile(
    r"(?:likes?|views?)[,\s]+[\d.,KMB]*\s*(?:comments?)?\s*[-–—]\s*"
    r"([A-Za-z0-9._]{2,30})\s+on\s+[A-Za-z]+\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)
_USERNAME_SIMPLE_RE = re.compile(
    r"[-–—]\s*([A-Za-z0-9._]{2,30})\s+on\s+[A-Za-z]",
    re.IGNORECASE,
)

DEFAULT_FORMAT_THRESHOLD = 45


def extract_username_from_caption(caption: str | None) -> str | None:
    """Parse creator handle from Instagram og:description-style captions."""
    text = (caption or "").strip()
    if not text:
        return None
    for pattern in (_USERNAME_FROM_OG_RE, _USERNAME_SIMPLE_RE):
        m = pattern.search(text)
        if m:
            user = m.group(1).strip().lstrip("@")
            skip = {"instagram", "reel", "reels", "explore", "www", "http", "https"}
            if user.lower() not in skip and len(user) >= 2:
                return user
    return None


def enrich_post_identity(post: dict[str, Any]) -> dict[str, Any]:
    """Fill username/profile_url from caption when missing."""
    out = dict(post)
    username = (out.get("username") or "").strip().lstrip("@") or None
    if not username:
        username = extract_username_from_caption(out.get("caption") or out.get("raw_text"))
    if username:
        out["username"] = username
        if not out.get("profile_url"):
            out["profile_url"] = f"https://www.instagram.com/{username}/"
    return out


def _text_blob(post: dict[str, Any]) -> str:
    parts = [
        str(post.get("caption") or ""),
        str(post.get("raw_text") or ""),
        str(post.get("video_description") or ""),
        str(post.get("format_reason") or ""),
        str(post.get("adaptable_hook") or ""),
    ]
    return " ".join(parts).lower()


def score_format_offline(
    post: dict[str, Any],
    *,
    preferred_formats: list[str] | None = None,
    threshold: int = DEFAULT_FORMAT_THRESHOLD,
) -> dict[str, Any]:
    """Heuristic people-first format score from caption/description text."""
    text = _text_blob(post)
    score = 28
    reasons: list[str] = []
    content_format = "unknown"
    human_present = False
    spoken = False

    preferred = {
        str(f).strip().lower()
        for f in (preferred_formats or list(PREFERRED_FORMATS))
        if str(f).strip()
    } or set(PREFERRED_FORMATS)

    for needle, bump, fmt in _PEOPLE_POSITIVE:
        if needle in text:
            score += bump
            reasons.append(f"+{fmt}:{needle}")
            content_format = fmt
            human_present = True
            if fmt in {
                "talking_head",
                "microphone",
                "podcast_interview",
                "spoken_explanation",
                "direct_instruction",
                "q_and_a",
            }:
                spoken = True

    for needle, penalty in _PEOPLE_NEGATIVE:
        if needle in text:
            score -= penalty
            reasons.append(f"-faceless_or_meme:{needle}")

    # Explicit multimodal fields already set
    if post.get("human_present") is True:
        human_present = True
        score += 15
        reasons.append("+human_present_flag")
    if post.get("spoken_or_instructional") is True:
        spoken = True
        score += 15
        reasons.append("+spoken_flag")
    if post.get("content_format") and str(post.get("content_format")) in preferred:
        content_format = str(post.get("content_format"))
        score += 10
        reasons.append(f"+preferred:{content_format}")

    # Pure meme captions with almost no educational language
    if re.search(r"a{4,}", text) and "founder" not in text and "mvp" not in text:
        score -= 20
        reasons.append("-scream_meme")

    score = max(0, min(100, score))
    passed = score >= threshold and (human_present or spoken or content_format in preferred)

    # If we only have weak signals, do not keep as people-first.
    if score < threshold:
        passed = False

    return {
        **post,
        "content_format": content_format if content_format != "unknown" else (
            post.get("content_format") or "unknown"
        ),
        "human_present": human_present,
        "spoken_or_instructional": spoken,
        "format_score": score,
        "format_reason": "; ".join(reasons) or "no strong people-first signal",
        "format_kept": passed,
    }


def apply_format_gate(
    posts: list[dict[str, Any]],
    *,
    preferred_formats: list[str] | None = None,
    threshold: int = DEFAULT_FORMAT_THRESHOLD,
    require_format: bool = True,
) -> list[dict[str, Any]]:
    """Score format and optionally require format_kept for overall kept."""
    out: list[dict[str, Any]] = []
    for post in posts:
        enriched = enrich_post_identity(post)
        scored = score_format_offline(
            enriched,
            preferred_formats=preferred_formats,
            threshold=threshold,
        )
        topical_kept = bool(scored.get("kept"))
        if require_format:
            scored["kept"] = topical_kept and bool(scored.get("format_kept"))
            if topical_kept and not scored.get("format_kept"):
                reason = scored.get("reason") or ""
                fmt_reason = scored.get("format_reason") or "failed people-first format gate"
                scored["reason"] = (
                    f"{reason}; format gate: {fmt_reason}".strip("; ").strip()
                )
        out.append(scored)
    return out


def merge_multimodal_format(
    post: dict[str, Any],
    analysis: dict[str, Any] | None,
    *,
    preferred_formats: list[str] | None = None,
    threshold: int = DEFAULT_FORMAT_THRESHOLD,
) -> dict[str, Any]:
    """Merge structured multimodal format fields into a post, then re-score."""
    out = enrich_post_identity(post)
    if not analysis:
        return score_format_offline(
            out, preferred_formats=preferred_formats, threshold=threshold
        )
    for key in (
        "content_format",
        "human_present",
        "spoken_or_instructional",
        "format_reason",
        "video_description",
    ):
        if analysis.get(key) is not None and analysis.get(key) != "":
            out[key] = analysis[key]
    if analysis.get("analysis") and not out.get("video_description"):
        out["video_description"] = analysis["analysis"]
    return score_format_offline(
        out, preferred_formats=preferred_formats, threshold=threshold
    )
