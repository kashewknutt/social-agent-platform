"""Daily creative synthesis dashboard generation using Kimi K3."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ig_agent.config import FILTERED_DIR, REPORTS_DIR, Settings, get_settings
from ig_agent.filter import load_agency_context
from ig_agent.llm import KimiClient


def _collect_filtered_posts() -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for path in sorted(FILTERED_DIR.glob("filtered_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        posts.extend(data.get("posts", []))
    return posts


def _build_synthesis_prompt(
    agency_context: dict[str, Any],
    posts: list[dict[str, Any]],
    multimodal_notes: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    notes_block = ""
    if multimodal_notes:
        notes_block = f"\n\nMultimodal analysis notes:\n{json.dumps(multimodal_notes, indent=2)}"

    system = (
        "You are a Senior B2B Marketing Strategist specializing in software development agencies. "
        "Generate a structured Markdown report with these exact sections:\n"
        "## Trending Reels Script\n"
        "## Lead Generation Ads\n"
        "## Organic Brand Presence\n"
        "## Static Posts & Threads\n"
        "Be specific, actionable, and adapt viral trends to the agency's positioning."
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
) -> Path:
    """Generate the daily Markdown dashboard from filtered trends."""
    cfg = settings or get_settings()
    ctx = load_agency_context()
    posts = _collect_filtered_posts()

    if not posts:
        raise RuntimeError(
            "No filtered posts found. Run ingest and filter first, or use sample data."
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
        client = KimiClient(cfg)
        body = client.chat(
            _build_synthesis_prompt(ctx, posts, multimodal_notes),
            model=cfg.kimi_synth_model,
        )

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
- Hook: {hooks[0] if hooks else 'POV: Your startup MVP breaks on launch day'}
- Scene 1: Founder on a call, panic face, error logs on screen
- Scene 2: Cut to {brand} engineer fixing architecture diagram
- CTA: "Book a free architecture audit"

## Lead Generation Ads

**Ad 1 — SaaS Rescue**
- Headline: "Your MVP shouldn't die in production"
- Body: Custom SaaS architecture review — fixed scope, 2-week turnaround
- CTA: DM "AUDIT"

**Ad 2 — AI Integration**
- Headline: "Add AI without rewriting your stack"
- Body: {brand} integrates LLM workflows into existing B2B apps
- CTA: Link in bio

## Organic Brand Presence

- Carousel: "5 B2B app mistakes we see every week" (based on filtered trends)
- Story: Behind-the-scenes deploy day timelapse
- Comment strategy: Reply to founder posts about MVP failures with value-first tips

## Static Posts & Threads

**Instagram caption:**
> Most startups don't fail on ideas — they fail on architecture decisions made in week 1.
> We help founders ship MVPs that scale. What's the worst tech debt you've inherited?

**Threads post:**
> Hot take: Your "quick MVP" is costing you $50K in refactors within 6 months.
> Here's what we'd do differently → [thread]{mm_section}
"""
