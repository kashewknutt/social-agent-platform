"""SQLite persistence for the video Analyzer (upload -> AI caption/title/hashtags)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from ig_agent.config import ANALYZER_DB_PATH, get_settings

VALID_STATUSES = frozenset({"uploaded", "processing", "done", "failed"})


def _now() -> str:
    return datetime.now().isoformat()


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or ANALYZER_DB_PATH
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
    get_settings()  # ensure data dirs (incl. upload dir) exist
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_analyses (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                original_filename TEXT,
                video_path TEXT,
                context_note TEXT,
                title TEXT,
                caption TEXT,
                hashtags_json TEXT,
                raw_analysis TEXT,
                error TEXT,
                posted INTEGER NOT NULL DEFAULT 0,
                posted_username TEXT,
                posted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payload_json TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_analyses_status ON video_analyses(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_video_analyses_posted ON video_analyses(posted)"
        )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    data["posted"] = bool(data.get("posted"))
    raw_hashtags = data.pop("hashtags_json", None)
    try:
        data["hashtags"] = json.loads(raw_hashtags) if raw_hashtags else []
    except json.JSONDecodeError:
        data["hashtags"] = []
    raw_payload = data.pop("payload_json", None)
    if raw_payload:
        try:
            data["payload"] = json.loads(raw_payload)
        except json.JSONDecodeError:
            data["payload"] = {"raw": raw_payload}
    else:
        data["payload"] = {}
    return data


def create_video_analysis(
    *,
    status: str = "uploaded",
    original_filename: str | None = None,
    video_path: str | None = None,
    context_note: str | None = None,
    payload: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    init_db(db_path)
    analysis_id = uuid4().hex
    now = _now()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO video_analyses (
                id, status, original_filename, video_path, context_note,
                title, caption, hashtags_json, raw_analysis, error,
                posted, posted_username, posted_at, created_at, updated_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 0, NULL, NULL, ?, ?, ?)
            """,
            (
                analysis_id,
                status,
                original_filename,
                video_path,
                context_note,
                now,
                now,
                json.dumps(payload or {}),
            ),
        )
    result = get_video_analysis(analysis_id, db_path=db_path)
    assert result is not None
    return result


def get_video_analysis(analysis_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM video_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
    return _row_to_dict(row)


def list_video_analyses(
    *,
    status: str | None = None,
    posted: bool | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    init_db(db_path)
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if posted is not None:
        clauses.append("posted = ?")
        params.append(1 if posted else 0)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM video_analyses
            {where}
            ORDER BY created_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [d for d in (_row_to_dict(r) for r in rows) if d is not None]


def update_video_analysis(
    analysis_id: str,
    *,
    status: str | None = None,
    title: str | None = None,
    caption: str | None = None,
    hashtags: list[str] | None = None,
    raw_analysis: str | None = None,
    error: str | None = None,
    posted: bool | None = None,
    posted_username: str | None = None,
    payload: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    init_db(db_path)
    existing = get_video_analysis(analysis_id, db_path=db_path)
    if existing is None:
        return None
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    fields: list[str] = ["updated_at = ?"]
    params: list[Any] = [_now()]
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if caption is not None:
        fields.append("caption = ?")
        params.append(caption)
    if hashtags is not None:
        fields.append("hashtags_json = ?")
        params.append(json.dumps(hashtags))
    if raw_analysis is not None:
        fields.append("raw_analysis = ?")
        params.append(raw_analysis)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    if posted is not None:
        fields.append("posted = ?")
        params.append(1 if posted else 0)
        fields.append("posted_at = ?")
        params.append(_now() if posted else None)
    if posted_username is not None:
        fields.append("posted_username = ?")
        params.append(posted_username)
    if payload is not None:
        fields.append("payload_json = ?")
        params.append(json.dumps(payload))
    params.append(analysis_id)
    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE video_analyses SET {', '.join(fields)} WHERE id = ?",
            params,
        )
    return get_video_analysis(analysis_id, db_path=db_path)


def delete_video_analysis(analysis_id: str, db_path: Path | None = None) -> bool:
    init_db(db_path)
    existing = get_video_analysis(analysis_id, db_path=db_path)
    if existing is None:
        return False
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM video_analyses WHERE id = ?", (analysis_id,))
    video_path = existing.get("video_path")
    if video_path:
        try:
            p = Path(video_path)
            if p.exists():
                p.unlink()
                # Clean up the now-empty per-upload directory too.
                try:
                    p.parent.rmdir()
                except OSError:
                    pass
        except Exception:
            pass
    return True
