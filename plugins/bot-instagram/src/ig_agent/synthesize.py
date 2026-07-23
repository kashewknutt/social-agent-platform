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
_SYNTH_POST_FIELDS = ("post_url", "username", "caption", "post_type", "relevance_score", "adaptable_hook")


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
        compact.append(item)
    return compact


def _compact_notes_for_synthesis(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "post_url": n.get("post_url"),
            "analysis": str(n.get("analysis") or "")[:280],
        }
        for n in notes[:_SYNTH_MAX_POSTS]
    ]


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
    brand = agency_context.get("brand_name") or "the agency"
    site = agency_context.get("website") or ""
    region = agency_context.get("region") or ""
    region_note = f" The agency operates in {region}." if region else ""
    site_note = f" Brand site: {site}." if site else ""

    system = (
        f"You are a Senior B2B Marketing Strategist for {brand}.{region_note}{site_note} "
        "Generate a structured Markdown report with these exact sections:\n"
        "## Trending Reels Script\n"
        "## Lead Generation Ads\n"
        "## Organic Brand Presence\n"
        "## Static Posts & Threads\n"
        "Be specific, actionable, and adapt viral trends to the agency's positioning. "
        f"Always brand as {brand}" + (f" ({site})" if site else "") + "."
    )
    user = (
        f"Agency profile:\n{json.dumps(agency_context, indent=2)}\n\n"
        f"Filtered trending posts ({len(posts)} items):\n{json.dumps(posts, indent=2)}"
        f"{notes_block}\n\n"
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
    top = sorted(posts, key=lambda p: p.get("relevance_score", 0), reverse=True)[:3]
    hooks = [p.get("adaptable_hook") or p.get("caption", "")[:80] for p in top]

    mm_section = ""
    if multimodal_notes:
        mm_section = "\n## Multimodal Notes\n" + "\n".join(
            f"- {n.get('post_url', 'post')}: {n.get('analysis', '')[:200]}"
            for n in multimodal_notes
        )

    return f"""## Trending Reels Script

Adapt the top trend for {brand}:
- Hook: {hooks[0] if hooks else 'POV: Your freelancer vanished mid-MVP'}
- Scene 1: Founder waiting on Slack, no replies, half-built product
- Scene 2: Cut to {brand} shipping a launch-ready MVP on a fixed deadline
- CTA: "Talk to the founder — valnee.com"

## Lead Generation Ads

**Ad 1 — MVP Partner**
- Headline: "Stop hiring freelancers who disappear"
- Body: Fixed price. Guaranteed launch date. 100% code ownership.
- CTA: Book a free strategy call

**Ad 2 — Launch Guarantee**
- Headline: "Your launch date goes in the contract"
- Body: {brand} builds MVPs that actually ship — for non-technical founders.
- CTA: Link in bio → valnee.com

## Organic Brand Presence

- Carousel: "Freelancer roulette vs a delivery partner" (based on filtered trends)
- Story: Behind-the-scenes sprint update
- Comment strategy: Reply to founder MVP pain posts with value-first tips

## Static Posts & Threads

**Instagram caption:**
> Most founders don't fail on ideas — they fail on unreliable execution.
> {brand} is the technical partner that delivers. What's blocked your launch?

**Threads post:**
> Hot take: A cheap freelancer often costs more than a fixed-price MVP.
> Here's what we'd do differently → [thread]{mm_section}
"""
