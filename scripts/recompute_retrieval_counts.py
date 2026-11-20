#!/usr/bin/env python3
"""Bring ``mem_cells.retrieval_count`` back to what it now means.

The count used to rise up to 24 times a day per cell (and, before the throttle, with
every prompt build): live cells reached 190 to 393 within weeks and two reached
2,536 and 906. It now means "on how many days did a person's turn bring this memory
back", at most one a day, so a count can never exceed the days the cell has existed.
This clamps the older, inflated counts to that bound (a cell created today may have
been recalled today, hence at least 1 day). Counts at or below their bound are left
alone.

Run it after deploying the code that counts once a day (the old code would refill
the counts) and after a database backup. The preview is the default; ``--apply``
writes. Re-running is safe.

    uv run python scripts/recompute_retrieval_counts.py
    uv run python scripts/recompute_retrieval_counts.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import build_runtime_postgres_dsn  # noqa: E402
from core.soul.repository import PostgresSoulRepository  # noqa: E402

_SECONDS_PER_DAY = 86400.0


def max_plausible_count(created_at: datetime, now: datetime) -> int:
    """The most a once-a-day count can be for a cell created at ``created_at``."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (now - created_at).total_seconds() / _SECONDS_PER_DAY
    return max(1, math.ceil(age_days))


def clamped_count(count: int, created_at: datetime, now: datetime) -> int:
    """``count`` limited to what the cell's age allows."""
    return min(int(count), max_plausible_count(created_at, now))


async def _run(apply: bool) -> int:
    repository = PostgresSoulRepository(dsn=build_runtime_postgres_dsn())
    pool = await repository._get_pool()
    try:
        now = datetime.now(timezone.utc)
        rows = await pool.fetch(
            "SELECT id, retrieval_count, created_at FROM mem_cells "
            "WHERE retrieval_count > 1 ORDER BY retrieval_count DESC"
        )
        changes = [
            (row["id"], row["retrieval_count"], target)
            for row in rows
            if (target := clamped_count(row["retrieval_count"], row["created_at"], now))
            < row["retrieval_count"]
        ]
        removed = sum(before - after for _, before, after in changes)
        print(f"{len(changes)} of {len(rows)} counted cells exceed their age")
        for cell_id, before, after in changes[:15]:
            print(f"  {before:>6} -> {after:>4}  {cell_id[:70]}")
        if len(changes) > 15:
            print(f"  ... and {len(changes) - 15} more")
        print(f"total removed: {removed}")
        if not apply:
            print("preview only; re-run with --apply to write")
            return 0
        async with pool.acquire() as conn:
            async with conn.transaction():
                for cell_id, _before, after in changes:
                    await conn.execute(
                        "UPDATE mem_cells SET retrieval_count = $2 WHERE id = $1",
                        cell_id,
                        after,
                    )
        print(f"done: {len(changes)} cells updated")
        return 0
    finally:
        await repository.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
