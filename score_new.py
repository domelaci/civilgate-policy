#!/usr/bin/env python3
"""
Score first-time items (summary IS NULL) in a loop via the LLM rotation.
Uses ORDER BY RANDOM() so multiple instances don't duplicate work.

Usage:
  python3 score_new.py              # default batch 50
  python3 score_new.py --batch 100
"""
import argparse
import json
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from pipeline import call_llm, DB_FILE, SCORER_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def score_batch(conn: sqlite3.Connection, batch_size: int) -> int:
    rows = conn.execute(
        """SELECT id, raw_text FROM policies
           WHERE summary IS NULL AND score_failed = 0
           ORDER BY RANDOM()
           LIMIT ?""",
        (batch_size,),
    ).fetchall()

    if not rows:
        return 0

    done = 0
    for row_id, raw_text in rows:
        try:
            result, provider = call_llm(raw_text or "")
            _VALID = {"enacted", "proposed", "open_for_comment", "decision"}
            llm_status = result.get("status")
            conn.execute(
                """UPDATE policies SET
                   summary=?, social_score=?, social_reason=?,
                   environmental_score=?, environmental_reason=?,
                   economic_score=?, economic_reason=?,
                   human_rights_score=?, human_rights_reason=?,
                   governance_score=?, governance_reason=?,
                   tags=?, scope=?, scope_reason=?,
                   scored_at=?, scorer_version=?, scored_by=?, score_failed=0,
                   status=COALESCE(NULLIF(?,NULL), status)
                   WHERE id=?""",
                (
                    result.get("summary"),
                    result.get("social_score"),    result.get("social_reason"),
                    result.get("environmental_score"), result.get("environmental_reason"),
                    result.get("economic_score"),  result.get("economic_reason"),
                    result.get("human_rights_score"),  result.get("human_rights_reason"),
                    result.get("governance_score"), result.get("governance_reason"),
                    json.dumps(result.get("tags", []), ensure_ascii=False),
                    result.get("scope"), result.get("scope_reason"),
                    datetime.utcnow().isoformat(),
                    SCORER_VERSION, provider,
                    llm_status if llm_status in _VALID else None,
                    row_id,
                ),
            )
            conn.commit()
            done += 1
            log.info("Scored id=%d via %s", row_id, provider)
            time.sleep(1)
        except Exception as e:
            log.error("Failed id=%d: %s", row_id, e)
            conn.execute("UPDATE policies SET score_failed=1 WHERE id=?", (row_id,))
            conn.commit()

    return done


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=50)
    args = parser.parse_args()

    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")

    total = conn.execute(
        "SELECT COUNT(*) FROM policies WHERE summary IS NULL AND score_failed=0"
    ).fetchone()[0]
    log.info("Starting first-time scorer — %d items pending", total)

    grand = 0
    while True:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM policies WHERE summary IS NULL AND score_failed=0"
        ).fetchone()[0]
        if remaining == 0:
            log.info("All items scored. Done.")
            break
        log.info("=== Batch start — %d unscored items ===", remaining)
        n = score_batch(conn, args.batch)
        grand += n
        log.info("Batch done: %d scored, %d total so far", n, grand)
        time.sleep(2)

    conn.close()
