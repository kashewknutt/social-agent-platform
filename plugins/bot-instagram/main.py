#!/usr/bin/env python3
"""CLI entry point for the Kimi-powered Instagram trend agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Ensure src is on path when running directly
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ig_agent.config import FILTERED_DIR, RAW_DIR, get_settings
from ig_agent.engage import execute_auto_interactions
from ig_agent.filter import filter_all_raw, filter_latest_raw, filter_raw_file, load_agency_context, score_posts
from ig_agent.ingest import capture_trends_with_delays
from ig_agent.llm import KimiClient
from ig_agent.multimodal import analyze_from_filtered_file
from ig_agent.persist import init_db
from ig_agent.propose import propose_interactions
from ig_agent.scheduler import run_daemon
from ig_agent.synthesize import synthesize_dashboard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ig_agent")


def cmd_smoke_test(_: argparse.Namespace) -> None:
    """Verify Kimi API connectivity."""
    client = KimiClient()
    reply = client.smoke_test()
    print(f"Kimi smoke test OK: {reply.strip()}")


def cmd_filter(args: argparse.Namespace) -> None:
    """Filter raw scrape data for relevance."""
    offline = getattr(args, "offline", False)
    if args.file:
        path = filter_raw_file(Path(args.file), offline=offline)
        print(f"Filtered → {path}")
    elif args.all:
        paths = filter_all_raw()
        print(f"Filtered {len(paths)} file(s)")
        for p in paths:
            print(f"  → {p}")
    else:
        path = filter_latest_raw()
        if path:
            print(f"Filtered → {path}")
        else:
            print("No raw files to filter.")


def cmd_filter_sample(args: argparse.Namespace) -> None:
    """Run filter pipeline on bundled sample data (no API for ingest)."""
    sample_path = RAW_DIR / "sample_scraped.json"
    if not sample_path.exists():
        print(f"Sample file missing: {sample_path}")
        sys.exit(1)
    offline = getattr(args, "offline", False)
    path = filter_raw_file(sample_path, offline=offline)
    print(f"Sample filtered → {path}")


def cmd_synthesize(args: argparse.Namespace) -> None:
    """Generate daily Markdown dashboard."""
    offline = getattr(args, "offline", False)
    multimodal_notes = None
    if args.multimodal:
        filtered_files = sorted(FILTERED_DIR.glob("filtered_*.json"))
        if filtered_files:
            multimodal_notes = analyze_from_filtered_file(filtered_files[-1])
    report = synthesize_dashboard(multimodal_notes=multimodal_notes, offline=offline)
    print(f"Dashboard → {report}")


def cmd_ingest(_: argparse.Namespace) -> None:
    """Run a single Instagram ingestion pass."""
    from ig_agent.hashtag_rotation import pick_hashtags_for_session, prune_history

    ctx = load_agency_context()
    settings = get_settings()
    prune_history(keep_days=14.0)
    hashtags, note = pick_hashtags_for_session(
        ctx.get("competitor_hashtags"),
        within_days=settings.hashtag_cooldown_days,
    )
    print(note)
    path = asyncio.run(capture_trends_with_delays(hashtags=hashtags))
    print(f"Ingested → {path}")


def cmd_run_once(args: argparse.Namespace) -> None:
    """Full pipeline: ingest → filter → (optional multimodal) → synthesize."""
    from ig_agent.hashtag_rotation import pick_hashtags_for_session, prune_history

    ctx = load_agency_context()
    settings = get_settings()
    prune_history(keep_days=14.0)
    hashtags, note = pick_hashtags_for_session(
        ctx.get("competitor_hashtags"),
        within_days=settings.hashtag_cooldown_days,
    )

    if args.sample:
        sample_path = RAW_DIR / "sample_scraped.json"
        if not sample_path.exists():
            print(f"Sample file missing: {sample_path}")
            sys.exit(1)
        raw_path = sample_path
        print(f"Using sample data: {raw_path}")
    else:
        print(note)
        print("Starting Instagram ingestion...")
        raw_path = asyncio.run(capture_trends_with_delays(hashtags=hashtags))
        print(f"Ingested → {raw_path}")

    print("Filtering for relevance...")
    offline = getattr(args, "offline", False)
    filtered_path = filter_raw_file(raw_path, offline=offline)
    print(f"Filtered → {filtered_path}")

    multimodal_notes = None
    settings = get_settings()
    if args.multimodal or settings.enable_multimodal:
        print("Running multimodal analysis on top posts...")
        multimodal_notes = analyze_from_filtered_file(filtered_path)
        print(f"Multimodal notes: {len(multimodal_notes)}")

    print("Synthesizing daily dashboard...")
    report = synthesize_dashboard(multimodal_notes=multimodal_notes, offline=offline)
    print(f"Dashboard → {report}")

    engage = getattr(args, "engage", True) and settings.engage_after_research
    if engage:
        init_db()
        print("Proposing engagement interactions...")
        created = propose_interactions(
            filtered_path=filtered_path,
            agency_context=ctx,
            settings=settings,
        )
        print(f"Proposed {len(created)} interaction(s)")
        if args.sample or offline:
            print("Sample/offline — skipped browser engagement")
        else:
            print("Executing auto likes/follows...")
            results = asyncio.run(execute_auto_interactions(settings=settings))
            done = sum(1 for r in results if r.get("status") == "done")
            print(f"Auto execute done={done} total={len(results)}")


def cmd_propose(_: argparse.Namespace) -> None:
    """Propose interactions from the latest filtered shortlist (offline-safe)."""
    init_db()
    created = propose_interactions()
    print(json.dumps({"count": len(created), "ids": [c["id"] for c in created]}, indent=2))


def cmd_daemon(_: argparse.Namespace) -> None:
    """Run continuous background scheduler."""
    asyncio.run(run_daemon())


def cmd_serve(args: argparse.Namespace) -> None:
    """Expose the HTTP control API for the orchestrator."""
    import os

    import uvicorn

    os.environ["BOT_PORT"] = str(args.port)
    uvicorn.run("ig_agent.api:app", host=args.host, port=args.port, reload=False)


def cmd_analyze_media(_: argparse.Namespace) -> None:
    """Run multimodal analysis on the latest filtered scrape."""
    settings = get_settings()
    settings.enable_multimodal = True
    filtered_files = sorted(FILTERED_DIR.glob("filtered_*.json"))
    if not filtered_files:
        print("No filtered files found.")
        sys.exit(1)
    notes = analyze_from_filtered_file(filtered_files[-1], settings)
    print(json.dumps(notes, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kimi-powered local Instagram trend agent",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use offline heuristics instead of Kimi API (dev/testing)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_smoke = sub.add_parser("smoke-test", help="Verify Kimi API connectivity")
    p_smoke.set_defaults(func=cmd_smoke_test)

    p_ingest = sub.add_parser("ingest", help="Single Instagram scrape pass")
    p_ingest.set_defaults(func=cmd_ingest)

    p_filter = sub.add_parser("filter", help="Filter raw scrape data")
    p_filter.add_argument("--file", help="Specific raw JSON file to filter")
    p_filter.add_argument("--all", action="store_true", help="Filter all unfiltered raw files")
    p_filter.set_defaults(func=cmd_filter)

    p_filter_sample = sub.add_parser("filter-sample", help="Filter bundled sample data")
    p_filter_sample.set_defaults(func=cmd_filter_sample)

    p_synth = sub.add_parser("synthesize", help="Generate daily Markdown dashboard")
    p_synth.add_argument("--multimodal", action="store_true", help="Include multimodal notes")
    p_synth.set_defaults(func=cmd_synthesize)

    p_run = sub.add_parser("run-once", help="Full pipeline: ingest → filter → synthesize → propose")
    p_run.add_argument("--sample", action="store_true", help="Use sample data instead of live scrape")
    p_run.add_argument("--multimodal", action="store_true", help="Run multimodal analysis")
    p_run.add_argument("--no-engage", dest="engage", action="store_false", help="Skip propose/engage")
    p_run.set_defaults(func=cmd_run_once, engage=True)

    p_propose = sub.add_parser("propose", help="Propose interactions from latest filtered shortlist")
    p_propose.set_defaults(func=cmd_propose)

    p_daemon = sub.add_parser("daemon", help="Continuous background scheduler")
    p_daemon.set_defaults(func=cmd_daemon)

    p_serve = sub.add_parser("serve", help="Start HTTP control API for orchestrator")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=7411)
    p_serve.set_defaults(func=cmd_serve)

    p_mm = sub.add_parser("analyze-media", help="Run multimodal analysis on latest filtered file")
    p_mm.set_defaults(func=cmd_analyze_media)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
