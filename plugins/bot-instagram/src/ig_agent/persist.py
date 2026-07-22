"""SQLite persistence for Instagram interactions (likes/follows/comments/DMs/posts)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ig_agent.config import DB_PATH, get_settings

VALID_KINDS = frozenset({"like", "follow", "comment", "dm", "post"})
VALID_STATUSES = frozenset(
    {
        "proposed",
        "approved",
        "rejected",
        "executing",
        "done",
        "failed",
        "skipped",
    }
)
AUTO_KINDS = frozenset({"like", "follow"})
HITL_KINDS = frozenset({"comment", "dm", "post"})


def _now() -> str:
    return datetime.now().isoformat()


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    get_settings()  # ensure data dirs
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interactions (
                id TEXT PRIMARY KEY,
                run_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                post_url TEXT,
                profile_url TEXT,
                username TEXT,
                draft_text TEXT,
                final_text TEXT,
                auto INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                executed_at TEXT,
                payload_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interactions_run ON interactions(run_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_interactions_status ON interactions(status)"
        )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["auto"] = bool(data.get("auto"))
    raw_payload = data.pop("payload_json", None)
    if raw_payload:
        try:
            data["payload"] = json.loads(raw_payload)
        except json.JSONDecodeError:
            data["payload"] = {"raw": raw_payload}
    else:
        data["payload"] = {}
    return data


def create_interaction(
    *,
    kind: str,
    status: str = "proposed",
    run_id: str | None = None,
    post_url: str | None = None,
    profile_url: str | None = None,
    username: str | None = None,
    draft_text: str | None = None,
    final_text: str | None = None,
    auto: bool | None = None,
    payload: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if kind not in VALID_KINDS:
        raise ValueError(f"Invalid kind: {kind}")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    init_db(db_path)
    interaction_id = uuid4().hex
    now = _now()
    is_auto = AUTO_KINDS.__contains__(kind) if auto is None else bool(auto)
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO interactions (
                id, run_id, kind, status, post_url, profile_url, username,
                draft_text, final_text, auto, error, created_at, updated_at,
                executed_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?)
            """,
            (
                interaction_id,
                run_id,
                kind,
                status,
                post_url,
                profile_url,
                username,
                draft_text,
                final_text,
                1 if is_auto else 0,
                now,
                now,
                json.dumps(payload or {}),
            ),
        )
    result = get_interaction(interaction_id, db_path=db_path)
    assert result is not None
    return result


def get_interaction(interaction_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM interactions WHERE id = ?", (interaction_id,)
        ).fetchone()
    return _row_to_dict(row)


def list_interactions(
    *,
    run_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM interactions
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]  # type: ignore[misc]


def update_interaction(
    interaction_id: str,
    *,
    status: str | None = None,
    draft_text: str | None = None,
    final_text: str | None = None,
    error: str | None = None,
    executed_at: str | None = None,
    payload: dict[str, Any] | None = None,
    username: str | None = None,
    profile_url: str | None = None,
    post_url: str | None = None,
    run_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    init_db(db_path)
    existing = get_interaction(interaction_id, db_path=db_path)
    if existing is None:
        return None
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    fields: list[str] = ["updated_at = ?"]
    params: list[Any] = [_now()]
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if draft_text is not None:
        fields.append("draft_text = ?")
        params.append(draft_text)
    if final_text is not None:
        fields.append("final_text = ?")
        params.append(final_text)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    if executed_at is not None:
        fields.append("executed_at = ?")
        params.append(executed_at)
    if username is not None:
        fields.append("username = ?")
        params.append(username)
    if profile_url is not None:
        fields.append("profile_url = ?")
        params.append(profile_url)
    if post_url is not None:
        fields.append("post_url = ?")
        params.append(post_url)
    if run_id is not None:
        fields.append("run_id = ?")
        params.append(run_id)
    if payload is not None:
        fields.append("payload_json = ?")
        params.append(json.dumps(payload))
    params.append(interaction_id)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE interactions SET {', '.join(fields)} WHERE id = ?",
            params,
        )
    return get_interaction(interaction_id, db_path=db_path)


def approve_interaction(
    interaction_id: str,
    *,
    final_text: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    existing = get_interaction(interaction_id, db_path=db_path)
    if existing is None:
        return None
    text = final_text if final_text is not None else existing.get("draft_text")
    return update_interaction(
        interaction_id,
        status="approved",
        final_text=text,
        error="",
        db_path=db_path,
    )


def reject_interaction(
    interaction_id: str,
    *,
    reason: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    return update_interaction(
        interaction_id,
        status="rejected",
        error=reason or "skipped by operator",
        db_path=db_path,
    )


def mark_executing(interaction_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    return update_interaction(interaction_id, status="executing", error="", db_path=db_path)


def mark_done(interaction_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    return update_interaction(
        interaction_id,
        status="done",
        executed_at=_now(),
        error="",
        db_path=db_path,
    )


def mark_failed(
    interaction_id: str,
    error: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    return update_interaction(
        interaction_id,
        status="failed",
        error=error,
        executed_at=_now(),
        db_path=db_path,
    )


def extract_post_identity(post: dict[str, Any]) -> dict[str, str | None]:
    """Pull username / profile_url / post_url from a filtered post when present."""
    username = (
        post.get("username")
        or post.get("author")
        or post.get("handle")
        or post.get("profile_username")
    )
    if isinstance(username, str):
        username = username.lstrip("@").strip() or None
    else:
        username = None

    profile_url = post.get("profile_url") or post.get("author_url")
    if not profile_url and username:
        profile_url = f"https://www.instagram.com/{username}/"

    post_url = post.get("post_url") or post.get("url")
    return {
        "username": username,
        "profile_url": profile_url if isinstance(profile_url, str) else None,
        "post_url": post_url if isinstance(post_url, str) else None,
    }
