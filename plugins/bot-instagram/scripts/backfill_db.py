"""One-shot backfill of latest scrape into SQLite."""

from __future__ import annotations

import json
from pathlib import Path

from ig_agent.config import DB_PATH, RAW_DIR, REPORTS_DIR
from ig_agent.filter import filter_raw_file
from ig_agent.persist import RunStore, connect


def main() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM posts WHERE run_id LIKE 'backfill%'")
        conn.execute("DELETE FROM multimodal_notes WHERE run_id LIKE 'backfill%'")
        conn.execute("DELETE FROM reports WHERE run_id LIKE 'backfill%'")
        conn.execute("DELETE FROM runs WHERE run_id LIKE 'backfill%'")

    store = RunStore()
    run_id = "backfill-1784373006"
    raw = RAW_DIR / "scraped_1784373006.json"
    reports = sorted(REPORTS_DIR.glob("Daily_Social_Dashboard_*.md"))
    if not raw.exists() or not reports:
        raise SystemExit("Missing raw scrape or report to backfill")

    store.start_run(run_id, mode="once", multimodal=True)
    print("raw", store.save_raw(run_id, raw))
    filtered = filter_raw_file(raw, offline=True)
    data = json.loads(filtered.read_text(encoding="utf-8"))
    print("filtered file posts", data.get("post_count"))
    print("filtered db", store.save_filtered(run_id, filtered))
    store.save_report(run_id, reports[-1])
    store.finish_run(run_id, "completed")
    print(store.summary(run_id))
    print("db", DB_PATH)

    with connect() as conn:
        rows = conn.execute(
            "SELECT stage, post_url, relevance_score FROM posts WHERE run_id=? ORDER BY stage, id",
            (run_id,),
        ).fetchall()
        for row in rows:
            print(dict(row))


if __name__ == "__main__":
    main()
