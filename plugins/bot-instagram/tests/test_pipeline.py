"""Basic pipeline tests (offline, no API key required)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ig_agent.config import FILTERED_DIR, RAW_DIR, REPORTS_DIR, get_settings
from ig_agent.filter import filter_raw_file, load_agency_context, score_posts
from ig_agent.offline import score_posts_offline
from ig_agent.safety import can_start_scroll_session, human_delay, session_cooldown_seconds
from ig_agent.synthesize import synthesize_dashboard


def test_settings_and_dirs():
    settings = get_settings()
    assert settings.kimi_filter_model == "kimi-k2.6"
    assert RAW_DIR.exists()
    assert FILTERED_DIR.exists()
    assert REPORTS_DIR.exists()


def test_offline_filter():
    sample = json.loads((RAW_DIR / "sample_scraped.json").read_text())
    posts = sample["posts"]
    ctx = load_agency_context()
    scored = score_posts_offline(posts, ctx)
    assert len(scored) >= 2
    assert all(p["relevance_score"] >= 60 for p in scored)
    # Skincare post should be filtered out
    captions = [p.get("caption", "") for p in scored]
    assert not any("skincare" in c.lower() for c in captions)


def test_filter_sample_file():
    sample_path = RAW_DIR / "sample_scraped.json"
    out = filter_raw_file(sample_path, offline=True)
    data = json.loads(out.read_text())
    assert data["post_count"] >= 2
    assert out.name.startswith("filtered_")


def test_synthesize_offline():
    filter_raw_file(RAW_DIR / "sample_scraped.json", offline=True)
    report = synthesize_dashboard(offline=True)
    content = report.read_text()
    assert "Trending Reels Script" in content
    assert "Lead Generation Ads" in content


def test_safety_helpers():
    assert 1.0 <= human_delay(1.0, 5.0) <= 5.0
    assert 3 * 3600 <= session_cooldown_seconds() <= 6 * 3600
    assert can_start_scroll_session()


if __name__ == "__main__":
    test_settings_and_dirs()
    test_offline_filter()
    test_filter_sample_file()
    test_synthesize_offline()
    test_safety_helpers()
    print("All offline tests passed.")
