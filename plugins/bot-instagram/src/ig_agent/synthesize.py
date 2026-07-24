"""Daily creative synthesis dashboard generation using Kimi K3."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ig_agent.config import FILTERED_DIR, REPORTS_DIR, Settings, get_settings
from ig_agent.filter import load_agency_context
from ig_agent.llm import KimiClient

logger = logging.getLogger("ig_agent.synthesize")

# Synthesis only needs enough of each post to inspire the report — dumping
# every field (media paths, raw booleans, etc.) for every kept post bloats
# the prompt and slows generation for no benefit. Cap both the fields and
# the post count.
_SYNTH_MAX_POSTS = 10
_SYNTH_POST_FIELDS = (
    "post_url",
    "username",
    "caption",
    "post_type",
    "relevance_score",
    "adaptable_hook",
    "content_format",
    "human_present",
    "spoken_or_instructional",
    "format_score",
    "video_description",
)


def _collect_filtered_posts(filtered_path: Path | None = None) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    if filtered_path and filtered_path.exists():
        data = json.loads(filtered_path.read_text(encoding="utf-8"))
        return list(data.get("posts", []))

    paths = [
        p
        for p in sorted(FILTERED_DIR.glob("filtered_*.json"))
        if not p.name.endswith("_with_media.json") and "sample" not in p.name.lower()
    ]
    # Prefer the newest single file — avoid mixing historical scrapes + samples
    if paths:
        data = json.loads(paths[-1].read_text(encoding="utf-8"))
        return list(data.get("posts", []))
    return posts


def _compact_posts_for_synthesis(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(posts, key=lambda p: int(p.get("relevance_score") or 0), reverse=True)
    compact = []
    for p in ranked[:_SYNTH_MAX_POSTS]:
        item = {k: p.get(k) for k in _SYNTH_POST_FIELDS if p.get(k) is not None}
        if p.get("caption"):
            item["caption"] = str(p["caption"])[:280]
        if p.get("video_description"):
            item["video_description"] = str(p["video_description"])[:280]
        compact.append(item)
    return compact


def _compact_notes_for_synthesis(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "post_url": n.get("post_url"),
            "analysis": str(n.get("analysis") or "")[:280],
            "content_format": n.get("content_format"),
            "format_score": n.get("format_score"),
        }
        for n in notes[:_SYNTH_MAX_POSTS]
    ]


def _follow_candidates(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    ranked = sorted(
        posts,
        key=lambda p: (
            int(p.get("format_score") or 0),
            int(p.get("relevance_score") or 0),
        ),
        reverse=True,
    )
    for p in ranked:
        username = (p.get("username") or "").strip().lstrip("@")
        if not username or username.lower() in seen:
            continue
        if not (p.get("human_present") or p.get("spoken_or_instructional") or p.get("format_kept")):
            continue
        seen.add(username.lower())
        out.append(
            {
                "username": username,
                "profile_url": p.get("profile_url")
                or f"https://www.instagram.com/{username}/",
                "post_url": p.get("post_url"),
                "content_format": p.get("content_format"),
                "format_score": p.get("format_score"),
                "relevance_score": p.get("relevance_score"),
                "reason": p.get("format_reason") or p.get("reason") or "people-first match",
            }
        )
        if len(out) >= 5:
            break
    return out


def _build_synthesis_prompt(
    agency_context: dict[str, Any],
    posts: list[dict[str, Any]],
    multimodal_notes: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    notes_block = ""
    if multimodal_notes:
        compact_notes = _compact_notes_for_synthesis(multimodal_notes)
        notes_block = f"\n\nMultimodal analysis notes:\n{json.dumps(compact_notes, indent=2)}"

    posts = _compact_posts_for_synthesis(posts)
    follows = _follow_candidates(posts)
    brand = agency_context.get("brand_name") or "the agency"
    site = agency_context.get("website") or ""
    region = agency_context.get("region") or ""
    region_note = f" The agency operates in {region}." if region else ""
    site_note = f" Brand site: {site}." if site else ""

    system = (
        f"You are a Senior B2B Marketing Strategist for {brand}.{region_note}{site_note} "
        "Generate a structured Markdown report with these exact sections:\n"
        "## Founder-Led Reel Scripts\n"
        "## Interactive Posts & Stories\n"
        "## Trust & Proof Content\n"
        "## Creator Follow Recommendations\n"
        "Prioritize talking-head / mic / instructional formats inspired by the filtered people-first "
        "sources. Each reel script needs: hook, spoken beats, visual direction, proof element, and "
        "ONE audience action. Interactive ideas must be genuine participation "
        "(A/B founder decisions, what would you ship first, MVP teardown requests, Q&A, "
        "misconception checks, scope/pricing trade-offs, build reviews, case-study predictions) — "
        "NEVER 'comment KEYWORD for DM' bait. Tie each concept to one trust outcome: competence, "
        "reliability, transparency, proof, or empathy. "
        f"Always brand as {brand}" + (f" ({site})" if site else "") + "."
    )
    user = (
        f"Agency profile:\n{json.dumps(agency_context, indent=2)}\n\n"
        f"Filtered people-first posts ({len(posts)} items):\n{json.dumps(posts, indent=2)}"
        f"{notes_block}\n\n"
        f"Suggested creator follow shortlist (approval-only):\n{json.dumps(follows, indent=2)}\n\n"
        f"Generate today's content marketing execution plan as clean Markdown."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def synthesize_dashboard(
    settings: Settings | None = None,
    multimodal_notes: list[dict[str, Any]] | None = None,
    output_date: date | None = None,
    offline: bool = False,
    filtered_path: Path | None = None,
) -> Path:
    """Generate the daily Markdown dashboard from filtered trends."""
    cfg = settings or get_settings()
    ctx = load_agency_context()
    posts = _collect_filtered_posts(filtered_path)

    if not posts:
        raise RuntimeError(
            "No filtered posts found. Run ingest and filter first."
        )

    day = output_date or date.today()
    out_path = REPORTS_DIR / f"Daily_Social_Dashboard_{day.isoformat()}.md"
    header = (
        f"# Daily Social Dashboard — {day.isoformat()}\n\n"
        f"_Generated at {datetime.now().strftime('%Y-%m-%d %H:%M')} "
        f"from {len(posts)} filtered posts._\n\n"
    )

    if offline or not cfg.moonshot_api_key:
        body = _synthesize_offline(ctx, posts, multimodal_notes)
    else:
        try:
            client = KimiClient(cfg, timeout=cfg.kimi_synth_timeout_s)
            body = client.chat(
                _build_synthesis_prompt(ctx, posts, multimodal_notes),
                model=cfg.kimi_synth_model,
            )
        except Exception as exc:
            # The dashboard is a nice-to-have artifact — a slow/failed Kimi
            # call here must never abort the whole run (propose/engage still
            # need to happen). Fall back to the offline template instead.
            logger.warning("Synthesis Kimi call failed (%s) — using offline template", exc)
            body = _synthesize_offline(ctx, posts, multimodal_notes)
            header += "_(Kimi API was unavailable/slow — using offline template for this section.)_\n\n"

    out_path.write_text(header + body, encoding="utf-8")
    return out_path


def _synthesize_offline(
    agency_context: dict[str, Any],
    posts: list[dict[str, Any]],
    multimodal_notes: list[dict[str, Any]] | None = None,
) -> str:
    """Template dashboard when Kimi API is unavailable (dev/testing)."""
    brand = agency_context.get("brand_name", "Agency")
    site = (agency_context.get("website") or "https://valnee.com").replace("https://", "").replace("http://", "").rstrip("/")
    top = sorted(posts, key=lambda p: p.get("relevance_score", 0), reverse=True)[:3]
    hooks = [p.get("adaptable_hook") or (p.get("caption") or "")[:80] for p in top]
    follows = _follow_candidates(posts)

    mm_section = ""
    if multimodal_notes:
        mm_section = "\n## Multimodal Notes\n" + "\n".join(
            f"- {n.get('post_url', 'post')}: {n.get('analysis', '')[:200]}"
            for n in multimodal_notes
        )

    follow_lines = "\n".join(
        f"- @{f['username']} — {f.get('content_format') or 'people-first'} "
        f"(topic {f.get('relevance_score')}, format {f.get('format_score')}). "
        f"Why: {f.get('reason')}. Source: {f.get('post_url')}"
        for f in follows
    ) or "- No strong people-first creators in this batch yet — expand hashtag/phrase seeds."

    return f"""## Founder-Led Reel Scripts

**Script 1 — Talking-head trust reset**
- Hook: {hooks[0] if hooks else 'Most founders do not fail on ideas — they fail on unreliable execution.'}
- Spoken beats:
  1. Name the pain: freelancer ghosting / bloated agency timelines.
  2. Show the Valnee alternative: fixed price, launch date in the contract, full code ownership.
  3. One concrete proof (sprint shipped / case study result).
- Visual direction: Founder on camera / mic; cut to product UI or launch checklist; no meme B-roll.
- Proof element: Fixed launch date + 100% code ownership.
- Audience action: "Would you rather ship a thin MVP in 4 weeks or wait 4 months for a polished never-launch? Reply 4W or 4M."
- Trust outcome: reliability

**Script 2 — Instructional teardown**
- Hook: "If your MVP scope cannot fit on one whiteboard, you are not building an MVP."
- Spoken beats: show a bloated scope → cut to a shippable core → invite founders to audit theirs.
- Visual direction: whiteboard / loom-style instruction with a person on camera.
- Proof element: Before/after scope cut from a real build.
- Audience action: "Drop your MVP idea in one sentence — I'll reply with what to cut first."
- Trust outcome: competence

## Interactive Posts & Stories

1. **A/B founder decision (Story poll):** "Validate with 20 interviews first" vs "Ship a clickable prototype this month" — ask why in replies. Trust: transparency.
2. **What would you ship first?** Carousel of 3 feature options for a fictional founder product; ask audience to vote. Trust: empathy.
3. **MVP teardown request:** "Send a screenshot of your current roadmap — I'll mark must-ship vs later." Trust: competence.
4. **Misconception check:** "True or false: A cheap freelancer costs less than a fixed-price MVP partner." Invite debate. Trust: proof.
5. **Case-study prediction:** Show a problem brief; ask "What would you build first?" then reveal what {brand} shipped. Trust: proof.

Avoid comment-KEYWORD-for-DM bait. Every interaction should create a conversation {brand} can answer publicly.

## Trust & Proof Content

- Case study frame: non-technical founder → fixed scope → launched on the contracted date → owns the repo.
- Risk-reduction post: "Your launch date goes in the contract" with a simple checklist founders can steal.
- Transparency carousel: what is included in an MVP fixed-price engagement vs what is explicitly out of scope.
- Soft CTA to {site} only after the proof section — never as the hook.

## Creator Follow Recommendations

{follow_lines}

These are approval-only recommendations. Do not auto-follow.
{mm_section}
"""
