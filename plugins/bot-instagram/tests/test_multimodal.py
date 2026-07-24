"""Multimodal module structure tests (no API required)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ig_agent.config import MEDIA_DIR, get_settings
from ig_agent.multimodal import analyze_top_posts


def test_multimodal_skips_without_media():
    settings = get_settings()
    settings.enable_multimodal = True
    posts = [{"post_url": "https://example.com", "relevance_score": 90}]
    notes = analyze_top_posts(posts, settings)
    assert notes == []


def test_multimodal_finds_local_image():
    settings = get_settings()
    settings.enable_multimodal = True

    fixture = Path(__file__).parents[1] / "data" / "filtered" / "sample_for_multimodal.json"
    frame = MEDIA_DIR / "sample_reel_frame.png"
    if not fixture.exists() or not frame.exists():
        # Optional local fixture — not required in clean checkouts.
        return

    sample = json.loads(fixture.read_text())
    posts = sample["posts"]
    media_path = posts[0].get("media_path")
    path = MEDIA_DIR / media_path
    assert path.exists()


if __name__ == "__main__":
    test_multimodal_skips_without_media()
    test_multimodal_finds_local_image()
    print("Multimodal structure tests passed.")
