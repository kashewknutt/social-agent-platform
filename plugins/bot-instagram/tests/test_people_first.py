"""People-first discovery, format gate, and HITL follow tests (offline)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ig_agent.filter import filter_raw_file, load_agency_context, score_posts
from ig_agent.format_gate import (
    apply_format_gate,
    extract_username_from_caption,
    score_format_offline,
)
from ig_agent.hashtag_rotation import (
    DISCOVERY_HASHTAGS,
    normalize_hashtag,
    pick_hashtags_for_session,
    pick_phrases_for_session,
)
from ig_agent.config import RAW_DIR, get_settings
from ig_agent.persist import AUTO_KINDS, HITL_KINDS, init_db
from ig_agent.propose import propose_interactions
from ig_agent.synthesize import synthesize_dashboard


def test_hashtag_pool_is_people_first():
    assert "trending" not in DISCOVERY_HASHTAGS
    assert "reelsviral" not in DISCOVERY_HASHTAGS
    assert "founderstory" in DISCOVERY_HASHTAGS
    assert "founderpodcast" in DISCOVERY_HASHTAGS
    tags, note = pick_hashtags_for_session(["#founderstory", "#mvp"], max_pick=1, within_days=0)
    assert tags
    assert normalize_hashtag(tags[0]) in {"founderstory", "mvp"} or tags[0] in DISCOVERY_HASHTAGS
    assert "fresh" in note.lower() or "reusing" in note.lower() or "Using" in note


def test_phrase_rotation():
    phrases, note = pick_phrases_for_session(
        ["founder advice", "MVP lessons"],
        max_pick=1,
        within_days=0,
    )
    assert phrases
    assert phrases[0]
    assert "phrase" in note.lower() or "Using" in note


def test_username_extraction_from_og_caption():
    caption = (
        '807 likes, 159 comments - theblur.io on March 18, 2026: '
        '"Comment website to get your free consultation"'
    )
    assert extract_username_from_caption(caption) == "theblur.io"


def test_format_gate_keeps_talking_head_rejects_meme():
    talking = score_format_offline(
        {
            "caption": "Founder advice on MVP launch lessons — let me explain",
            "video_description": "Talking-head founder speaks into a microphone on a podcast interview.",
        }
    )
    meme = score_format_offline(
        {
            "caption": "AAAaaaHHHHhhhhh #codememes #meme #codinglife",
            "video_description": "Text-on-screen coding meme with a faceless desk setup.",
        }
    )
    assert talking["format_kept"] is True
    assert talking["human_present"] or talking["spoken_or_instructional"]
    assert meme["format_kept"] is False


def test_apply_format_gate_with_topical_kept():
    posts = [
        {
            "post_url": "https://www.instagram.com/reel/a/",
            "caption": "In this reel I explain MVP validation for non-technical founders",
            "video_description": "Person speaking to camera with instructional walkthrough.",
            "relevance_score": 80,
            "kept": True,
        },
        {
            "post_url": "https://www.instagram.com/reel/b/",
            "caption": "POV lock in desk setup #developer",
            "video_description": "Faceless cinematic coding screen aesthetic.",
            "relevance_score": 70,
            "kept": True,
        },
    ]
    gated = apply_format_gate(posts, require_format=True)
    kept_urls = {p["post_url"] for p in gated if p.get("kept")}
    assert "https://www.instagram.com/reel/a/" in kept_urls
    assert "https://www.instagram.com/reel/b/" not in kept_urls


def test_legacy_direction_migration():
    from ig_agent.runtime import migrate_legacy_direction

    legacy = {
        "competitor_hashtags": ["#trending"],
        "goals": "Find adaptable content angles that position Valnee Solutions...",
        "constraints": "While browsing research, like and follow relevant creator posts live.",
    }
    migrated = migrate_legacy_direction(legacy)
    assert "#trending" not in migrated["competitor_hashtags"]
    assert "#founderstory" in migrated["competitor_hashtags"]
    assert migrated["discovery_phrases"]
    assert migrated["preferred_formats"]
    assert migrated["research_mode"] == "people_first"
    assert "people content" in migrated["goals"].lower()
    assert "auto-follow" in migrated["constraints"].lower()
    ctx = load_agency_context()
    assert ctx.get("research_mode") == "people_first"
    assert "#trending" not in (ctx.get("competitor_hashtags") or [])
    assert any("founder" in h.lower() for h in ctx.get("competitor_hashtags") or [])
    assert ctx.get("discovery_phrases")
    assert "talking_head" in (ctx.get("preferred_formats") or [])


def test_sample_pipeline_people_first_and_hitl_follows(tmp_path: Path):
    settings = get_settings()
    filtered = filter_raw_file(RAW_DIR / "sample_scraped.json", offline=True, settings=settings)
    data = json.loads(filtered.read_text(encoding="utf-8"))
    kept = data.get("posts") or []
    assert len(kept) >= 2
    # Coding meme / faceless desk should not survive both gates
    captions = " ".join((p.get("caption") or "").lower() for p in kept)
    assert "codememes" not in captions
    assert "skincare" not in captions
    assert any(p.get("format_kept") for p in kept)

    report = synthesize_dashboard(offline=True, filtered_path=filtered)
    content = report.read_text(encoding="utf-8")
    assert "Founder-Led Reel Scripts" in content
    assert "Interactive Posts" in content
    assert "Creator Follow Recommendations" in content
    assert "Trending Reels Script" not in content
    assert "comment KEYWORD" not in content.lower()

    db = tmp_path / "interactions.db"
    init_db(db)
    created = propose_interactions(
        run_id="people-first-test",
        filtered_path=filtered,
        db_path=db,
        settings=settings,
    )
    assert created
    assert "like" in AUTO_KINDS
    assert "follow" in HITL_KINDS
    assert "follow" not in AUTO_KINDS

    likes = [r for r in created if r["kind"] == "like"]
    follows = [r for r in created if r["kind"] == "follow"]
    assert likes
    assert all(r["auto"] for r in likes)
    assert follows
    assert all(not r["auto"] for r in follows)
    assert all(r.get("username") for r in follows)
    assert all(r.get("draft_text") for r in follows)


if __name__ == "__main__":
    test_hashtag_pool_is_people_first()
    test_phrase_rotation()
    test_username_extraction_from_og_caption()
    test_format_gate_keeps_talking_head_rejects_meme()
    test_apply_format_gate_with_topical_kept()
    test_agency_context_people_first_defaults()
    test_sample_pipeline_people_first_and_hitl_follows(Path("/tmp/people-first-tests"))
    print("People-first offline tests passed.")
