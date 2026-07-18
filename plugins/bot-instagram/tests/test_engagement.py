"""Offline engagement tests — no Instagram login required."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ig_agent.config import RAW_DIR, get_settings
from ig_agent.filter import filter_raw_file
from ig_agent.persist import (
    approve_interaction,
    get_interaction,
    init_db,
    list_interactions,
    reject_interaction,
)
from ig_agent.propose import propose_interactions
from ig_agent.safety import can_perform, remaining_cap, usage_snapshot


def test_config_engagement_caps():
    settings = get_settings()
    assert settings.max_likes_per_day == 20
    assert settings.max_follows_per_day == 10
    assert settings.max_comments_per_day == 8
    assert settings.max_dms_per_day == 5
    assert settings.max_posts_per_day == 1
    assert settings.engage_after_research is True


def test_safety_caps_and_usage():
    assert can_perform("like")
    assert remaining_cap("like") >= 1
    snap = usage_snapshot()
    assert "likes_remaining" in snap
    assert snap["max_likes_per_day"] == 20


def test_offline_propose_from_sample(tmp_path: Path):
    db = tmp_path / "interactions.db"
    init_db(db)
    filtered = filter_raw_file(RAW_DIR / "sample_scraped.json", offline=True)
    created = propose_interactions(run_id="test-run", filtered_path=filtered, db_path=db)
    assert created, "expected proposed interactions from sample shortlist"
    kinds = {row["kind"] for row in created}
    assert "like" in kinds
    assert "follow" in kinds
    assert "comment" in kinds
    assert "dm" in kinds
    assert "post" in kinds

    likes = [r for r in created if r["kind"] == "like"]
    assert all(r["auto"] for r in likes)
    assert all(r["status"] == "proposed" for r in likes)
    assert all(r.get("post_url") for r in likes)

    follows = [r for r in created if r["kind"] == "follow"]
    assert all(r["auto"] for r in follows)
    assert all(r.get("username") for r in follows)
    assert all(r.get("profile_url") for r in follows)

    comments = [r for r in created if r["kind"] == "comment"]
    assert all(not r["auto"] for r in comments)
    assert all(r.get("draft_text") for r in comments)

    listed = list_interactions(run_id="test-run", db_path=db)
    assert len(listed) == len(created)

    comment = comments[0]
    approved = approve_interaction(comment["id"], final_text="Edited approval", db_path=db)
    assert approved is not None
    assert approved["status"] == "approved"
    assert approved["final_text"] == "Edited approval"

    dm = next(r for r in created if r["kind"] == "dm")
    rejected = reject_interaction(dm["id"], reason="skip", db_path=db)
    assert rejected is not None
    assert rejected["status"] == "rejected"
    assert get_interaction(dm["id"], db_path=db)["status"] == "rejected"


def test_imports():
    import ig_agent.api  # noqa: F401
    import ig_agent.engage  # noqa: F401
    import ig_agent.persist  # noqa: F401
    import ig_agent.propose  # noqa: F401
    import ig_agent.runtime  # noqa: F401


if __name__ == "__main__":
    from pathlib import Path as P

    test_config_engagement_caps()
    test_safety_caps_and_usage()
    test_offline_propose_from_sample(P("/tmp/ig-engage-smoke"))
    test_imports()
    print("Engagement offline tests passed.")
