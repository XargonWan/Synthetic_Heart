#!/usr/bin/env python3
"""One-shot migration: consolidate ai_diary days with multiple rows into a single row per day.

Run inside the synth-dev container:
    python /app/scripts/migrate_diary_consolidate.py

For each calendar day that has more than one row in ai_diary:
  1. Concatenate all content values (ordered by id ASC) with '\n\n---\n\n'.
  2. UPDATE the row with the highest id for that day to hold the concatenated content.
  3. DELETE all lower-id rows for that day.

Only days with more than one row are touched.  Days with exactly one row are left as-is.
"""

from __future__ import annotations

import os
import sys
from typing import Any

try:
    import MySQLdb  # type: ignore[import]
except ModuleNotFoundError:
    try:
        import pymysql as MySQLdb  # type: ignore[import]

        MySQLdb.install_as_MySQLdb()  # type: ignore[attr-defined]
    except ModuleNotFoundError:
        print("ERROR: neither MySQLdb nor pymysql is available. Install one and retry.")
        sys.exit(1)

# ── Config (env-first, sensible dev defaults) ────────────────────────────────

DB_HOST = os.environ.get("DB_HOST", "synth-db")
DB_PORT = int(os.environ.get("DB_PORT", 3306))
DB_USER = os.environ.get("DB_USER", "synth")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "DigiHeart01")
DB_NAME = os.environ.get("DB_NAME", "synth")

SEPARATOR = "\n\n---\n\n"


# ── Helpers ───────────────────────────────────────────────────────────────────


def connect() -> Any:
    return MySQLdb.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        passwd=DB_PASSWORD,
        db=DB_NAME,
        charset="utf8mb4",
        use_unicode=True,
    )


def migrate(dry_run: bool = False) -> None:
    conn = connect()
    cur = conn.cursor(MySQLdb.cursors.DictCursor)

    # Find all days that have more than one row
    cur.execute(
        """
        SELECT DATE(created_at) AS day, COUNT(*) AS cnt
        FROM ai_diary
        GROUP BY DATE(created_at)
        HAVING cnt > 1
        ORDER BY day ASC
        """
    )
    multi_days: list[dict] = cur.fetchall()

    if not multi_days:
        print("Nothing to migrate — every day already has at most one row.")
        return

    print(f"Days to consolidate: {len(multi_days)}")

    total_rows_merged = 0
    total_rows_deleted = 0

    for day_row in multi_days:
        day: str = str(day_row["day"])
        cnt: int = day_row["cnt"]

        # Fetch all rows for this day, ordered by id ASC (oldest first)
        cur.execute(
            """
            SELECT id, content
            FROM ai_diary
            WHERE DATE(created_at) = %s
            ORDER BY id ASC
            """,
            (day,),
        )
        rows: list[dict] = cur.fetchall()

        if len(rows) <= 1:
            continue  # shouldn't happen given HAVING cnt>1, but be safe

        # Consolidate content: join with '---', skip blanks
        parts = [r["content"] for r in rows if r["content"]]
        combined = SEPARATOR.join(parts)

        keeper_id: int = rows[-1]["id"]  # highest id (most recent row)
        delete_ids: list[int] = [r["id"] for r in rows[:-1]]

        print(
            f"  {day}: {cnt} rows → keeper id={keeper_id}, "
            f"deleting ids={delete_ids[:5]}{'…' if len(delete_ids) > 5 else ''}"
        )

        if not dry_run:
            # Update keeper with concatenated content
            cur.execute(
                "UPDATE ai_diary SET content = %s WHERE id = %s",
                (combined, keeper_id),
            )
            # Delete the rest
            fmt = ",".join(["%s"] * len(delete_ids))
            cur.execute(f"DELETE FROM ai_diary WHERE id IN ({fmt})", delete_ids)
            conn.commit()

        total_rows_merged += 1
        total_rows_deleted += len(delete_ids)

    conn.close()

    mode = "[DRY RUN] " if dry_run else ""
    print(
        f"\n{mode}Done. "
        f"Days consolidated: {total_rows_merged}, rows deleted: {total_rows_deleted}."
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    if dry:
        print("=== DRY RUN — no changes will be written ===\n")
    migrate(dry_run=dry)
