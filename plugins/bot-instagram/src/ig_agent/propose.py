"""Propose Instagram engagement interactions from filtered research shortlists."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ig_agent.config import FILTERED_DIR, Settings, get_settings
from ig_agent.filter import load_agency_context
from ig_agent.persist import (
    AUTO_KINDS,
    create_interaction,
    extract_post_identity,
    init_db,
    list_interactions,
)
from ig_agent.safety import remaining_cap

_HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)/?")


def _latest_filtered_path() -> Path | None:
    files = sorted(FILTERED_DIR.glob("filtered_*.json"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def load_filtered_posts(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or _latest_filtered_path()
    if target is None or not target.exists():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    posts = data.get("posts", [])
    return posts if isinstance(posts, list) else []


def _username_from_post(post: dict[str, Any]) -> str | None:
    identity = extract_post_identity(post)
    if identity.get("username"):
        return identity["username"]
    url = identity.get("post_url") or ""
    if isinstance(url, str):
        # Prefer explicit profile links; skip /p/ and /reel/ segments.
        for match in _HANDLE_RE.finditer(url):
            handle = match.group(1)
            if handle.lower() not in {"p", "reel", "reels", "explore", "stories"}:
                return handle
    return None


def _draft_comment(post: dict[str, Any], agency: dict[str, Any]) -> str:
    brand = agency.get("brand_name") or "our team"
    hook = post.get("adaptable_hook") or "this angle"
    pillar = ""
    pillars = agency.get("content_pillars") or []
    if pillars:
        pillar = f" — especially around {pillars[0]}"
    caption = (post.get("caption") or "").strip()
    snippet = caption[:80].rstrip() + ("…" if len(caption) > 80 else "")
    return (
        f"Sharp take{pillar}. {hook}. "
        f"From {brand}: love how this lands — \"{snippet}\""
    )


def _draft_dm(post: dict[str, Any], agency: dict[str, Any]) -> str:
    brand = agency.get("brand_name") or "our agency"
    goals = (agency.get("goals") or "learning from strong B2B content").strip()
    return (
        f"Hey — saw your post and it resonated with what {brand} is exploring "
        f"({goals}). Would love to compare notes if you're open to it."
    )


def _draft_post(posts: list[dict[str, Any]], agency: dict[str, Any]) -> str:
    brand = agency.get("brand_name") or "our team"
    voice = agency.get("brand_voice") or "clear and practical"
    top = posts[0] if posts else {}
    hook = top.get("adaptable_hook") or "a practical SaaS lesson"
    hashtags = agency.get("competitor_hashtags") or ["#saas", "#softwaredevelopment"]
    tag_line = " ".join(hashtags[:4])
    return (
        f"{hook}.\n\n"
        f"At {brand} we keep it {voice.lower()}: ship the lesson, not the hype.\n\n"
        f"{tag_line}"
    )


def _already_proposed(
    *,
    run_id: str | None,
    kind: str,
    post_url: str | None,
    username: str | None,
    db_path: Path | None,
) -> bool:
    existing = list_interactions(run_id=run_id, kind=kind, limit=500, db_path=db_path)
    for row in existing:
        if post_url and row.get("post_url") == post_url:
            return True
        if kind in {"follow", "dm"} and username and row.get("username") == username:
            return True
        if kind == "post" and row.get("status") in {"proposed", "approved", "done"}:
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
) -> list[dict[str, Any]]:
    """
    Create interaction rows from the filtered shortlist + agency context.

    - like / follow → auto=True, status=proposed (ready for auto-execute)
    - comment / dm / post → auto=False, status=proposed (HITL)
    Offline template drafts are intentional and sufficient for sample runs.
    """
    cfg = settings or get_settings()
    init_db(db_path)
    agency = agency_context or load_agency_context()
    posts = load_filtered_posts(filtered_path)
    if not posts:
        return []

    # Rank by relevance when present.
    ranked = sorted(posts, key=lambda p: int(p.get("relevance_score") or 0), reverse=True)

    like_budget = min(
        max_likes if max_likes is not None else remaining_cap("like", cfg),
        remaining_cap("like", cfg),
        len(ranked),
    )
    follow_budget = min(
        max_follows if max_follows is not None else remaining_cap("follow", cfg),
        remaining_cap("follow", cfg),
        len(ranked),
    )
    comment_budget = min(
        max_comments if max_comments is not None else remaining_cap("comment", cfg),
        remaining_cap("comment", cfg),
        len(ranked),
    )
    dm_budget = min(
        max_dms if max_dms is not None else remaining_cap("dm", cfg),
        remaining_cap("dm", cfg),
        len(ranked),
    )

    created: list[dict[str, Any]] = []
    followed_usernames: set[str] = set()
    dm_usernames: set[str] = set()

    # Likes (auto)
    for post in ranked:
        if len([c for c in created if c["kind"] == "like"]) >= like_budget:
            break
        identity = extract_post_identity(post)
        post_url = identity.get("post_url")
        if not post_url:
            continue
        if _already_proposed(run_id=run_id, kind="like", post_url=post_url, username=None, db_path=db_path):
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

    # Follows (auto) — unique usernames when available
    for post in ranked:
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

    # Comments (HITL)
    for post in ranked:
        if len([c for c in created if c["kind"] == "comment"]) >= comment_budget:
            break
        identity = extract_post_identity(post)
        post_url = identity.get("post_url")
        if not post_url:
            continue
        if _already_proposed(run_id=run_id, kind="comment", post_url=post_url, username=None, db_path=db_path):
            continue
        created.append(
            create_interaction(
                kind="comment",
                status="proposed",
                run_id=run_id,
                post_url=post_url,
                profile_url=identity.get("profile_url"),
                username=_username_from_post(post) or identity.get("username"),
                draft_text=_draft_comment(post, agency),
                auto=False,
                payload={
                    "source": "propose",
                    "adaptable_hook": post.get("adaptable_hook"),
                    "relevance_score": post.get("relevance_score"),
                },
                db_path=db_path,
            )
        )

    # DMs (HITL)
    for post in ranked:
        if len([c for c in created if c["kind"] == "dm"]) >= dm_budget:
            break
        identity = extract_post_identity(post)
        username = _username_from_post(post) or identity.get("username")
        if not username or username in dm_usernames:
            continue
        if _already_proposed(
            run_id=run_id, kind="dm", post_url=identity.get("post_url"), username=username, db_path=db_path
        ):
            continue
        dm_usernames.add(username)
        created.append(
            create_interaction(
                kind="dm",
                status="proposed",
                run_id=run_id,
                post_url=identity.get("post_url"),
                profile_url=identity.get("profile_url") or f"https://www.instagram.com/{username}/",
                username=username,
                draft_text=_draft_dm(post, agency),
                auto=False,
                payload={"source": "propose", "relevance_score": post.get("relevance_score")},
                db_path=db_path,
            )
        )

    # One organic post draft (HITL)
    if include_post and remaining_cap("post", cfg) > 0:
        if not _already_proposed(run_id=run_id, kind="post", post_url=None, username=None, db_path=db_path):
            created.append(
                create_interaction(
                    kind="post",
                    status="proposed",
                    run_id=run_id,
                    draft_text=_draft_post(ranked, agency),
                    auto=False,
                    payload={
                        "source": "propose",
                        "inspired_by": [
                            extract_post_identity(p).get("post_url") for p in ranked[:3]
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
