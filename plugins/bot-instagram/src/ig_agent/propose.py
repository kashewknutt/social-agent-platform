"""Propose Instagram engagement interactions from filtered research shortlists."""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any, Literal

from ig_agent.config import FILTERED_DIR, Settings, get_settings
from ig_agent.filter import load_agency_context
from ig_agent.persist import (
    create_interaction,
    extract_post_identity,
    init_db,
    list_interactions,
    update_interaction,
)
from ig_agent.safety import remaining_cap

logger = logging.getLogger("ig_agent.propose")

_HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)/?")
Locale = Literal["india", "us"]

# Signals that the creator / caption leans Indian English or India-context
_INDIA_CUES = re.compile(
    r"\b("
    r"india|indian|bharat|delhi|mumbai|bangalore|bengaluru|hyderabad|chennai|pune|"
    r"startupindia|makeinindia|desi|jugaad|yaar|broski|namaste|inr|₹|"
    r"iit|iim|noida|gurgaon|gurugram|ahmedabad|kolkata|jaipur"
    r")\b|"
    r"(^|[_\.])(in|india|bharat|desi)([_\.]|$)",
    re.IGNORECASE,
)
_US_CUES = re.compile(
    r"\b("
    r"usa|u\.s\.|america|american|nyc|new york|sf|bay area|silicon valley|"
    r"la\b|los angeles|austin|seattle|chicago|ycombinator|yc\b|usd|\$"
    r")\b|"
    r"(^|[_\.])(usa|us|nyc|sf)([_\.]|$)",
    re.IGNORECASE,
)


def _latest_filtered_path() -> Path | None:
    files = sorted(
        (
            p
            for p in FILTERED_DIR.glob("filtered_*.json")
            if not p.name.endswith("_with_media.json") and "sample" not in p.name.lower()
        ),
        key=lambda p: p.stat().st_mtime,
    )
    return files[-1] if files else None


def load_filtered_posts(path: Path | None = None, *, include_rejected: bool = False) -> list[dict[str, Any]]:
    target = path or _latest_filtered_path()
    if target is None or not target.exists():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    if include_rejected and data.get("all_scored"):
        posts = list(data["all_scored"])
    else:
        posts = data.get("posts") or []
        if not posts and data.get("all_scored"):
            posts = [p for p in data["all_scored"] if p.get("kept")]
    return posts if isinstance(posts, list) else []


def _username_from_post(post: dict[str, Any]) -> str | None:
    identity = extract_post_identity(post)
    if identity.get("username"):
        return identity["username"]
    url = identity.get("post_url") or ""
    if isinstance(url, str):
        for match in _HANDLE_RE.finditer(url):
            handle = match.group(1)
            if handle.lower() not in {"p", "reel", "reels", "explore", "stories"}:
                return handle
    return None


def detect_locale(post: dict[str, Any], agency: dict[str, Any]) -> Locale:
    """Pick india vs us from caption / username; fall back to agency region."""
    caption = str(post.get("caption") or post.get("raw_text") or "")
    username = str(_username_from_post(post) or post.get("username") or "")
    blob = f"{caption}\n{username}"
    india_hit = bool(_INDIA_CUES.search(blob))
    us_hit = bool(_US_CUES.search(blob))
    if india_hit and not us_hit:
        return "india"
    if us_hit and not india_hit:
        return "us"
    region = str(agency.get("region") or "").strip().lower()
    if region in {"india", "in", "bharat"} or "india" in region:
        return "india"
    if region in {"us", "usa", "united states", "america"} or "united states" in region:
        return "us"
    # Default: agency is India-first (Valnee)
    return "india"


# Phrases that have crept into generated copy and now read as robotic tells.
# Listed explicitly so the model actively avoids them instead of drifting
# back into the same 2-3 constructions every time.
_BANNED_PHRASES = (
    "this hit home",
    "this landed",
    "felt that",
    "founder instinct",
    "founder journey",
    "resonated with",
    "caught your line",
    "sharp take",
    "great post",
    "love this",
    "so true",
    "needed that reminder",
    "shipping > overthinking",
    "watched it twice",
    "watched this twice",
    "had to watch this twice",
    "watched it a few times",
    "watched this a couple times",
    "on repeat",
    "saved this",
    "saving this",
    "bookmarking this",
    "nice one",
    "great content",
    "this is gold",
    "pure gold",
    "underrated",
)
_BANNED_PHRASES_BLOCK = "\n".join(f"- \"{p}\"" for p in _BANNED_PHRASES)

# The single biggest "this was written by AI" tell across every generated
# surface (comments, DMs, posts, video captions, hashtags). Applied
# everywhere, not just here, so keep this text itself free of the symbol.
_BANNED_SYMBOLS_RULE = (
    "Never use an em dash or en dash, anywhere, for any reason. If you want to "
    "connect two ideas, use a period, a comma, or just the word 'and' or 'but' "
    "instead. Also skip semicolons and rhetorical 'not just X, it's Y' framing. "
    "Real people texting don't use these, and they are the number one giveaway "
    "that a caption was AI generated."
)


def _voice_guardrails_block() -> str:
    """Banned phrases + banned punctuation. Shared by every generated surface
    (comment, DM, post, video caption) so the "sounds like AI" fixes only
    need to happen in one place.
    """
    return (
        f"{_BANNED_SYMBOLS_RULE}\n"
        "Also never use any of these phrases or close variants of them, they "
        "read as robotic filler:\n"
        f"{_BANNED_PHRASES_BLOCK}"
    )


def _locale_voice_block(locale: Locale) -> str:
    if locale == "india":
        return (
            "LOCALE: India (Indian English), but casual, like texting a friend, not "
            "writing a LinkedIn post.\n"
            "Skip stiff corporate phrases entirely (never: 'do the needful', "
            "'respected sir', 'kindly revert', 'as per').\n"
            "Contractions are fine. Lowercase-first is fine, capitalize only for emphasis."
        )
    return (
        "LOCALE: United States (American English), casual and direct.\n"
        "Short sentences. No fluff openings ('Hope this finds you well', "
        "'Just circling back').\n"
        "Contractions are fine. Lowercase-first is fine, capitalize only for emphasis."
    )


def _trending_hashtag_pool(agency: dict[str, Any]) -> str:
    """A few of the creator's own saved niche hashtags, offered as inspiration
    for "currently active in this niche" tags, not a required list.
    """
    tags = [str(t).strip().lstrip("#") for t in (agency.get("competitor_hashtags") or []) if str(t).strip()]
    if not tags:
        return ""
    sample = ", ".join(f"#{t}" for t in tags[:10])
    return (
        f"\nHashtags this account has already identified as relevant to its niche "
        f"(use as inspiration for what's currently active, don't just paste all of "
        f"these every time): {sample}\n"
    )


def _brand_facts(agency: dict[str, Any]) -> str:
    brand = agency.get("brand_name") or "Valnee Solutions"
    site = agency.get("website") or "https://valnee.com"
    voice = agency.get("brand_voice") or "direct, founder-friendly"
    return (
        f"Brand (background only, never paste these bullets into the message):\n"
        f"- Name: {brand}\n"
        f"- Site: {site}\n"
        f"- Voice: {voice}\n"
        f"- One-line offer (paraphrase lightly, never dump verbatim): "
        f"fixed-price MVP partner, guaranteed launch date, full code ownership."
    )


def _system_prompt_comment(locale: Locale, agency: dict[str, Any]) -> str:
    return (
        "You are a real person scrolling Instagram, leaving a quick comment. Not an AI, "
        "not a brand account, not a marketer. You comment the way you'd actually text a "
        "friend: a little casual, a little imperfect, always specific.\n"
        f"{_locale_voice_block(locale)}\n\n"
        "HOW TO WRITE (read everything below FIRST, then react to it):\n"
        "1. Output ONLY the comment text. No quotes, no labels, no explanation.\n"
        "2. 1-2 short sentences, under 180 characters.\n"
        "3. If a video/image description is given below, that IS what you watched. "
        "Pull ONE concrete detail from it (a specific action, number, tool, on-screen "
        "text, moment) and react to THAT in your own words, like you actually saw it "
        "happen. If no video description is given, use the caption the same way "
        "instead. Never generalize the detail into a life lesson or motivational "
        "one-liner, and never just restate/summarize what happened, react to it.\n"
        "4. Every comment must be a DIFFERENT reaction shaped by that specific post. "
        "Do not default to a generic all-purpose line ('watched it twice', 'saved "
        "this', 'nice one', etc.) just because you don't have much to go on, if the "
        "context is thin, ask a short, genuinely curious question about the specific "
        "thing shown instead of praising it vaguely.\n"
        "5. End with a genuine, specific observation or a short, low-stakes question. "
        "Never a call-to-action, never a sales pitch.\n"
        "6. At most ONE emoji, only if it truly fits. Most comments should use zero.\n"
        "7. Do NOT mention Valnee, pitch anything, or reference being an assistant/bot/AI.\n"
        "8. " + _voice_guardrails_block() + "\n"
        "If you catch yourself about to write one of those, stop and say something more "
        "specific about THIS post instead."
    )


def _system_prompt_dm(locale: Locale, agency: dict[str, Any]) -> str:
    return (
        "You are a real founder sliding into someone's DMs after seeing their post. "
        "Curious and specific, not running a sales script.\n"
        f"{_locale_voice_block(locale)}\n"
        f"{_brand_facts(agency)}\n\n"
        "HOW TO WRITE:\n"
        "1. Output ONLY the DM text. No quotes, no labels, no explanation.\n"
        "2. 2-4 short sentences, under 320 characters, mobile-readable.\n"
        "3. Open by reacting to ONE concrete, specific detail from their post, prefer "
        "the video/image description below if one is given (that's what you actually "
        "watched), otherwise use the caption. Reference it directly and specifically. "
        "Do not open with a generic compliment or a vague 'loved this'.\n"
        "4. Weave in who you are in ONE short clause, naturally (e.g. 'I build MVPs for "
        "founders over at valnee.com'). Never paste goals, pillars, or a value-prop list.\n"
        "5. End with exactly ONE easy question tied to what THEY posted. Not a generic "
        "'what are you building?' unless nothing more specific fits.\n"
        "6. No hard pitch, no 'book a call', no link-dumping, no stacked asks.\n"
        "7. " + _voice_guardrails_block()
    )


def _system_prompt_post(locale: Locale, agency: dict[str, Any]) -> str:
    return (
        "You write an original Instagram caption inspired by research themes. "
        "Not a copy of someone else's post.\n"
        f"{_locale_voice_block(locale)}\n"
        f"{_brand_facts(agency)}\n"
        f"{_trending_hashtag_pool(agency)}\n"
        "RULES:\n"
        "1. Output ONLY the caption. No labels or explanation.\n"
        "2. Hook in line 1. 3-6 short lines. Founder-friendly.\n"
        "3. Soft brand close on last line (Valnee / valnee.com). No strategy dump.\n"
        "4. End with exactly 4-5 hashtags on the final line. Pick ones that are "
        "currently active/trending for this niche on Instagram right now, not "
        "generic filler (#love, #instagood, #motivation) and not obscure tags "
        "nobody actually searches.\n"
        "5. " + _voice_guardrails_block()
    )


def strip_ai_tell_symbols(text: str) -> str:
    """Defensive net for the em/en dash ban: even with the prompt guardrail,
    models occasionally slip one back in. Swap it for what a real person
    would actually type instead of just deleting it.
    """
    out = text or ""
    out = re.sub(r"\s*[\u2014\u2013]\s*", ", ", out)  # em dash / en dash -> comma
    out = re.sub(r",\s*,", ",", out)  # collapse doubled commas from the swap
    out = re.sub(r",\s*\.", ".", out)  # ", ." -> "."
    out = re.sub(r"[ \t]+\n", "\n", out)  # trailing spaces left behind on a line
    return out


def _clean_model_text(text: str) -> str:
    out = (text or "").strip()
    if out.startswith("```"):
        out = re.sub(r"^```(?:\w+)?\s*", "", out)
        out = re.sub(r"\s*```$", "", out).strip()
    # Strip accidental wrappers
    if (out.startswith('"') and out.endswith('"')) or (out.startswith("'") and out.endswith("'")):
        out = out[1:-1].strip()
    for prefix in ("Comment:", "DM:", "Caption:", "Message:", "Output:"):
        if out.lower().startswith(prefix.lower()):
            out = out[len(prefix) :].strip()
    out = strip_ai_tell_symbols(out)
    return out.strip()


# Varied, cliché-free templates used only when the LLM call fails — kept
# short and rotated (not always the same construction) so an offline
# fallback doesn't reintroduce the same robotic phrase every time.
_FALLBACK_COMMENT_WITH_BIT = (
    "wait, \"{bit}\", okay yeah, makes sense.",
    "\"{bit}\" is the part that got me. how'd that actually play out?",
    "ok \"{bit}\" is a good line ngl.",
    "the \"{bit}\" bit, curious what happened right after that.",
)
_FALLBACK_COMMENT_NO_BIT = (
    "okay this is a solid one, saving it.",
    "good one, screenshotting this.",
    "this is a nice, quiet kind of good.",
)


def _fallback_comment(post: dict[str, Any], locale: Locale) -> str:
    caption = (post.get("caption") or "").strip()
    bit = ""
    if caption:
        words = caption.split()
        bit = " ".join(words[:8]).rstrip(".,!?")
        if len(bit) > 48:
            bit = bit[:48].rstrip() + "…"
    templates = _FALLBACK_COMMENT_WITH_BIT if bit else _FALLBACK_COMMENT_NO_BIT
    template = random.choice(templates)
    return template.format(bit=bit) if bit else template


_FALLBACK_DM_WITH_BIT = (
    "hey, \"{bit}\" caught my eye. i build MVPs for founders at {brand}. what's the story behind that?",
    "hey, saw your post, \"{bit}\" is a good detail. i help founders ship MVPs at {brand}. how far along is this?",
)
_FALLBACK_DM_NO_BIT = (
    "hey, saw your post, good stuff. i build MVPs for founders at {brand}. what are you working on right now?",
)


def _fallback_dm(post: dict[str, Any], locale: Locale, agency: dict[str, Any]) -> str:
    brand = agency.get("brand_name") or "Valnee"
    caption = (post.get("caption") or "").strip()
    words = caption.split()
    bit = " ".join(words[:6]).rstrip(".,!?") if words else ""
    if len(bit) > 40:
        bit = bit[:40].rstrip() + "…"
    templates = _FALLBACK_DM_WITH_BIT if bit else _FALLBACK_DM_NO_BIT
    template = random.choice(templates)
    return template.format(bit=bit, brand=brand)


def _fallback_post(posts: list[dict[str, Any]], agency: dict[str, Any], locale: Locale) -> str:
    brand = agency.get("brand_name") or "Valnee Solutions"
    site = (agency.get("website") or "https://valnee.com").replace("https://", "").replace("http://", "").rstrip("/")
    tags = " ".join((agency.get("competitor_hashtags") or ["#mvp", "#startup"])[:4])
    if locale == "india":
        return (
            "Most founders don't fail from lack of ideas.\n"
            "They fail waiting for the \"perfect\" first version.\n\n"
            "Ship the thin slice. Learn. Iterate.\n\n"
            f"That's the build rhythm we keep at {brand}.\n"
            f"{site}\n\n"
            f"{tags}"
        )
    return (
        "Stop polishing the pitch deck.\n"
        "Ship the smallest thing a real user can touch.\n\n"
        "Feedback beats fantasy every time.\n\n"
        f"{brand}\n"
        f"{site}\n\n"
        f"{tags}"
    )


def _llm_draft(
    *,
    kind: str,
    system: str,
    user: str,
    settings: Settings,
) -> str | None:
    try:
        from ig_agent.llm import KimiClient

        client = KimiClient(settings)
        raw = client.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=1,
        )
        return _clean_model_text(raw)
    except Exception as exc:
        logger.warning("LLM %s draft failed: %s", kind, exc)
        return None


def _post_context_block(post: dict[str, Any]) -> str:
    username = _username_from_post(post) or post.get("username") or "unknown"
    caption = (post.get("caption") or post.get("raw_text") or "").strip() or "(no caption)"
    lines = [
        f"Creator: @{username}",
        f"Post type: {post.get('post_type') or 'post'}",
        f"Caption:\n{caption[:600]}",
    ]
    video_desc = (post.get("video_description") or "").strip()
    if video_desc:
        lines.append(
            "What actually happens in the video/image (from someone who watched it, "
            f"this is your best source of concrete detail):\n{video_desc[:600]}"
        )
    hook = post.get("adaptable_hook")
    if hook:
        lines.append(f"Specific detail worth reacting to (internal note): {hook}")
    if not video_desc and caption == "(no caption)":
        lines.append(
            "NOTE: no caption and no video description are available for this post. "
            "Do not invent fake specific details. Keep the reaction short and genuinely "
            "curious instead (e.g. a real question), never generic praise."
        )
    return "\n".join(lines)


def draft_comment(post: dict[str, Any], agency: dict[str, Any], settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    locale = detect_locale(post, agency)
    text = _llm_draft(
        kind="comment",
        system=_system_prompt_comment(locale, agency),
        user=_post_context_block(post) + "\n\nWrite the Instagram comment now.",
        settings=cfg,
    )
    return text or _fallback_comment(post, locale)


def draft_dm(post: dict[str, Any], agency: dict[str, Any], settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    locale = detect_locale(post, agency)
    username = _username_from_post(post) or post.get("username") or "there"
    text = _llm_draft(
        kind="dm",
        system=_system_prompt_dm(locale, agency),
        user=(
            _post_context_block(post)
            + f"\n\nWrite a DM to @{username}. Output only the message body."
        ),
        settings=cfg,
    )
    return text or _fallback_dm(post, locale, agency)


def draft_post(
    posts: list[dict[str, Any]],
    agency: dict[str, Any],
    settings: Settings | None = None,
) -> str:
    cfg = settings or get_settings()
    locale = detect_locale(posts[0], agency) if posts else detect_locale({}, agency)
    themes = []
    for p in posts[:3]:
        cap = (p.get("caption") or "").strip()
        if cap:
            themes.append(f"- @{_username_from_post(p) or 'creator'}: {cap[:140]}")
    text = _llm_draft(
        kind="post",
        system=_system_prompt_post(locale, agency),
        user="Research themes:\n" + ("\n".join(themes) or "- founders shipping MVPs") + "\n\nWrite the caption.",
        settings=cfg,
    )
    return text or _fallback_post(posts, agency, locale)


# Back-compat aliases
def _draft_comment(post: dict[str, Any], agency: dict[str, Any]) -> str:
    return draft_comment(post, agency)


def _draft_dm(post: dict[str, Any], agency: dict[str, Any]) -> str:
    return draft_dm(post, agency)


def _draft_post(posts: list[dict[str, Any]], agency: dict[str, Any]) -> str:
    return draft_post(posts, agency)


def _find_open(
    *,
    kind: str,
    post_url: str | None,
    username: str | None,
    run_id: str | None,
    db_path: Path | None,
) -> dict[str, Any] | None:
    """Return an existing proposed/approved HITL row we can refresh."""
    for row in list_interactions(run_id=None, kind=kind, limit=500, db_path=db_path):
        if row.get("status") not in {"proposed", "approved"}:
            continue
        if post_url and row.get("post_url") == post_url:
            return row
        if kind in {"follow", "dm"} and username and row.get("username") == username:
            return row
        if kind == "post":
            return row
    return None


def _already_completed(
    *,
    kind: str,
    post_url: str | None,
    username: str | None,
    db_path: Path | None,
) -> bool:
    """True if we already successfully did this action on this target."""
    for row in list_interactions(run_id=None, kind=kind, limit=500, db_path=db_path):
        if row.get("status") != "done":
            continue
        if post_url and row.get("post_url") == post_url:
            return True
        if kind in {"follow", "dm"} and username and row.get("username") == username:
            return True
        if kind == "post":
            return True
    return False


def _already_proposed(
    *,
    run_id: str | None,
    kind: str,
    post_url: str | None,
    username: str | None,
    db_path: Path | None,
) -> bool:
    if _already_completed(
        kind=kind, post_url=post_url, username=username, db_path=db_path
    ):
        return True
    existing = list_interactions(run_id=run_id, kind=kind, limit=500, db_path=db_path)
    for row in existing:
        # Failed/skipped/rejected may be retried on a later research run.
        if row.get("status") not in {"proposed", "approved", "executing"}:
            continue
        if post_url and row.get("post_url") == post_url:
            return True
        if kind in {"follow", "dm"} and username and row.get("username") == username:
            return True
        if kind == "post":
            return True
    if kind in {"comment", "dm", "post"} and _find_open(
        kind=kind, post_url=post_url, username=username, run_id=run_id, db_path=db_path
    ):
        return True
    return False


def propose_interactions(
    *,
    run_id: str | None = None,
    filtered_path: Path | None = None,
    agency_context: dict[str, Any] | None = None,
    settings: Settings | None = None,
    db_path: Path | None = None,
    max_likes: int | None = None,
    max_follows: int | None = None,
    max_comments: int | None = None,
    max_dms: int | None = None,
    include_post: bool = True,
    refresh_drafts: bool = True,
) -> list[dict[str, Any]]:
    """
    Create interaction rows from the filtered shortlist + agency context.

    - like / follow → auto=True, status=proposed (ready for auto-execute)
    - comment / dm / post → auto=False, status=proposed (HITL)
    When refresh_drafts=True, regenerates draft_text on still-proposed HITL rows.
    """
    cfg = settings or get_settings()
    init_db(db_path)
    agency = agency_context or load_agency_context()
    # Likes/follows: every caught potential. HITL comments/DMs: kept shortlist only.
    all_posts = load_filtered_posts(filtered_path, include_rejected=True)
    kept_posts = load_filtered_posts(filtered_path, include_rejected=False) or [
        p for p in all_posts if p.get("kept")
    ]
    if not all_posts and not kept_posts:
        return []

    ranked_all = sorted(all_posts, key=lambda p: int(p.get("relevance_score") or 0), reverse=True)
    ranked_kept = sorted(kept_posts, key=lambda p: int(p.get("relevance_score") or 0), reverse=True)
    # If nothing cleared the keep threshold, still HITL the best catches so research isn't a dead end.
    if not ranked_kept and ranked_all:
        ranked_kept = ranked_all[:3]
        logger.info(
            "No kept posts — using top %s scored catches for HITL",
            len(ranked_kept),
        )

    like_budget = min(
        max_likes if max_likes is not None else remaining_cap("like", cfg),
        remaining_cap("like", cfg),
        len(ranked_all),
    )
    follow_budget = min(
        max_follows if max_follows is not None else remaining_cap("follow", cfg),
        remaining_cap("follow", cfg),
        len(ranked_all),
    )
    comment_budget = min(
        max_comments if max_comments is not None else remaining_cap("comment", cfg),
        remaining_cap("comment", cfg),
        len(ranked_kept),
    )
    dm_budget = min(
        max_dms if max_dms is not None else remaining_cap("dm", cfg),
        remaining_cap("dm", cfg),
        len(ranked_kept),
    )

    created: list[dict[str, Any]] = []
    followed_usernames: set[str] = set()
    dm_usernames: set[str] = set()

    # Likes (auto) — every potential, not only high-score kept
    for post in ranked_all:
        if len([c for c in created if c["kind"] == "like"]) >= like_budget:
            break
        identity = extract_post_identity(post)
        post_url = identity.get("post_url")
        if not post_url:
            continue
        if _already_proposed(run_id=run_id, kind="like", post_url=post_url, username=None, db_path=db_path):
            continue
        # Skip if already done live during ingest
        if any(
            r.get("kind") == "like" and r.get("post_url") == post_url and r.get("status") == "done"
            for r in list_interactions(kind="like", limit=500, db_path=db_path)
        ):
            continue
        created.append(
            create_interaction(
                kind="like",
                status="proposed",
                run_id=run_id,
                post_url=post_url,
                profile_url=identity.get("profile_url"),
                username=_username_from_post(post) or identity.get("username"),
                auto=True,
                payload={"source": "propose", "relevance_score": post.get("relevance_score")},
                db_path=db_path,
            )
        )

    # Follows (auto) — only when daily follow cap > 0
    if follow_budget > 0:
        for post in ranked_all:
            if len([c for c in created if c["kind"] == "follow"]) >= follow_budget:
                break
            identity = extract_post_identity(post)
            username = _username_from_post(post) or identity.get("username")
            if not username or username in followed_usernames:
                continue
            if _already_proposed(
                run_id=run_id, kind="follow", post_url=identity.get("post_url"), username=username, db_path=db_path
            ):
                continue
            if any(
                r.get("kind") == "follow"
                and (r.get("username") or "").lower() == username.lower()
                and r.get("status") == "done"
                for r in list_interactions(kind="follow", limit=500, db_path=db_path)
            ):
                continue
            followed_usernames.add(username)
            profile_url = identity.get("profile_url") or f"https://www.instagram.com/{username}/"
            created.append(
                create_interaction(
                    kind="follow",
                    status="proposed",
                    run_id=run_id,
                    post_url=identity.get("post_url"),
                    profile_url=profile_url,
                    username=username,
                    auto=True,
                    payload={"source": "propose", "relevance_score": post.get("relevance_score")},
                    db_path=db_path,
                )
            )

    # Comments (HITL) — kept shortlist
    for post in ranked_kept:
        if len([c for c in created if c["kind"] == "comment"]) >= comment_budget:
            break
        identity = extract_post_identity(post)
        post_url = identity.get("post_url")
        if not post_url:
            continue
        locale = detect_locale(post, agency)
        draft = draft_comment(post, agency, cfg)
        open_row = _find_open(
            kind="comment", post_url=post_url, username=None, run_id=run_id, db_path=db_path
        )
        if open_row and refresh_drafts:
            updated = update_interaction(
                open_row["id"],
                draft_text=draft,
                run_id=run_id,
                status="proposed",
                error="",
                payload={
                    **(open_row.get("payload") or {}),
                    "source": "propose",
                    "locale": locale,
                    "adaptable_hook": post.get("adaptable_hook"),
                    "relevance_score": post.get("relevance_score"),
                },
                db_path=db_path,
            )
            if updated:
                created.append(updated)
            continue
        if open_row or _already_proposed(
            run_id=run_id, kind="comment", post_url=post_url, username=None, db_path=db_path
        ):
            continue
        created.append(
            create_interaction(
                kind="comment",
                status="proposed",
                run_id=run_id,
                post_url=post_url,
                profile_url=identity.get("profile_url"),
                username=_username_from_post(post) or identity.get("username"),
                draft_text=draft,
                auto=False,
                payload={
                    "source": "propose",
                    "locale": locale,
                    "adaptable_hook": post.get("adaptable_hook"),
                    "relevance_score": post.get("relevance_score"),
                },
                db_path=db_path,
            )
        )

    # DMs (HITL)
    for post in ranked_kept:
        if len([c for c in created if c["kind"] == "dm"]) >= dm_budget:
            break
        identity = extract_post_identity(post)
        username = _username_from_post(post) or identity.get("username")
        if not username or username in dm_usernames:
            continue
        locale = detect_locale(post, agency)
        draft = draft_dm(post, agency, cfg)
        open_row = _find_open(
            kind="dm",
            post_url=identity.get("post_url"),
            username=username,
            run_id=run_id,
            db_path=db_path,
        )
        dm_usernames.add(username)
        if open_row and refresh_drafts:
            updated = update_interaction(
                open_row["id"],
                draft_text=draft,
                run_id=run_id,
                status="proposed",
                error="",
                payload={
                    **(open_row.get("payload") or {}),
                    "source": "propose",
                    "locale": locale,
                    "relevance_score": post.get("relevance_score"),
                },
                db_path=db_path,
            )
            if updated:
                created.append(updated)
            continue
        if open_row or _already_proposed(
            run_id=run_id,
            kind="dm",
            post_url=identity.get("post_url"),
            username=username,
            db_path=db_path,
        ):
            continue
        created.append(
            create_interaction(
                kind="dm",
                status="proposed",
                run_id=run_id,
                post_url=identity.get("post_url"),
                profile_url=identity.get("profile_url") or f"https://www.instagram.com/{username}/",
                username=username,
                draft_text=draft,
                auto=False,
                payload={
                    "source": "propose",
                    "locale": locale,
                    "relevance_score": post.get("relevance_score"),
                },
                db_path=db_path,
            )
        )

    # One organic post draft (HITL)
    if include_post and remaining_cap("post", cfg) > 0:
        locale = detect_locale(ranked_kept[0], agency) if ranked_kept else "india"
        draft = draft_post(ranked_kept, agency, cfg)
        open_row = _find_open(
            kind="post", post_url=None, username=None, run_id=run_id, db_path=db_path
        )
        if open_row and refresh_drafts:
            updated = update_interaction(
                open_row["id"],
                draft_text=draft,
                run_id=run_id,
                status="proposed",
                error="",
                payload={
                    **(open_row.get("payload") or {}),
                    "source": "propose",
                    "locale": locale,
                    "inspired_by": [extract_post_identity(p).get("post_url") for p in ranked_kept[:3]],
                },
                db_path=db_path,
            )
            if updated:
                created.append(updated)
        elif not open_row and not _already_proposed(
            run_id=run_id, kind="post", post_url=None, username=None, db_path=db_path
        ):
            created.append(
                create_interaction(
                    kind="post",
                    status="proposed",
                    run_id=run_id,
                    draft_text=draft,
                    auto=False,
                    payload={
                        "source": "propose",
                        "locale": locale,
                        "inspired_by": [
                            extract_post_identity(p).get("post_url") for p in ranked_kept[:3]
                        ],
                    },
                    db_path=db_path,
                )
            )

    return created


def propose_from_sample(
    *,
    run_id: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Convenience: filter is assumed already written; propose against latest filtered file."""
    return propose_interactions(run_id=run_id, db_path=db_path)
