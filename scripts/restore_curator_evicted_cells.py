#!/usr/bin/env python3
"""Restore MemCells that the memory curator's over-cap eviction deleted.

Trigger: `SoulCompiler.run_curator` evicts the lowest-salience cells once the
store is over `max_memories`, and until the grace window was applied to that
branch too, a cell written TODAY was the lowest-salience row in the store
(recency is 0.2 of the scale and a calm cell carries no emotional intensity),
so each pass deleted the cells that had just been compiled. Measured on
2026-09-22: 14 cells, including all of that morning's marriage conversation.

Every such eviction leaves exactly one tombstone: the `mem_scenes` row that
referenced the cell. A scene holds ONE cell and its `summary` is written
verbatim from that cell's `episodic_trace` (verified byte-for-byte on surviving
rows), so the deleted memory's content is still on disk and can be written
back. Nothing here is invented: id, session, timestamp, scene and text all come
from the stored scene row. Only what the scene does not carry is defaulted
(empty atomic facts, neutral emotion tag, retrieval count 0), and every
restored cell is stamped as distilled, because recall skips cells that are not.

Usage (from the repo root, so the plugin resolves the live store):

    ./.venv/Scripts/python.exe scripts/restore_curator_evicted_cells.py
    ./.venv/Scripts/python.exe scripts/restore_curator_evicted_cells.py --apply

The preview is the default and writes nothing. `--apply` writes the cells
through the plugin's own repository (mem_cells + mem_cell_vectors) and prints
where the pre-write record was saved.

Run it AFTER the curator fix is live: a curator pass under the old code deletes
the restored cells again, which is why this script is re-runnable and idempotent
(the ids are the originals, so a second run updates the same rows).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.soul.models import EmotionalTag, MemCell  # noqa: E402
from plugins.soul_plugin.soul_plugin import SoulPlugin  # noqa: E402

# Scenes that still reference a cell the store no longer holds.
DANGLING_SCENES_SQL = """
SELECT s.id AS scene_id,
       s.summary AS summary,
       s.created_at AS scene_created_at,
       c AS cell_id
FROM mem_scenes s
CROSS JOIN LATERAL jsonb_array_elements_text(s.cell_ids) AS c
WHERE s.created_at >= $1
  AND NOT EXISTS (SELECT 1 FROM mem_cells m WHERE m.id = c)
ORDER BY c
"""


def _parse_cell_id(cell_id: str) -> tuple[str, datetime]:
    """Split ``<session_id>:<UTC stamp>`` into its two halves."""

    session_id, _, stamp = cell_id.rpartition(":")
    if not session_id or not stamp.endswith("Z"):
        raise ValueError(f"unexpected memcell id: {cell_id!r}")
    parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%S%fZ")
    return session_id, parsed.replace(tzinfo=timezone.utc)


def _neutral_tag() -> EmotionalTag:
    """The tag the deployment itself writes for a calm cell."""

    return EmotionalTag(
        state_snapshot={"joy": 0.0, "sad": 0.0, "fear": 0.0, "anger": 0.0},
        dominant_emotion="neutral",
        intensity=0.0,
        valence=0.0,
    )


async def _restore(*, apply: bool, since: datetime) -> int:
    plugin = SoulPlugin()
    repo = plugin._repo
    compiler = plugin._compiler

    # The plugin falls back to an in-memory store when it cannot resolve the
    # runtime DSN, and that fallback would silently "succeed" while writing
    # nothing. Refuse instead.
    if type(repo).__name__ != "PostgresSoulRepository":
        print(
            f"REFUSING to run: the plugin resolved {type(repo).__name__}, not the "
            "live Postgres store. Check SOUL_POSTGRES_DSN for this process."
        )
        return 2

    pool = await cast(Any, repo)._get_pool()
    rows = await pool.fetch(DANGLING_SCENES_SQL, since)

    print(f"scenes referencing a missing cell (since {since.date()}): {len(rows)}")
    if not rows:
        print("nothing to restore")
        return 0

    now = datetime.now(timezone.utc)
    record: list[dict[str, object]] = []
    restored: list[str] = []
    skipped: list[tuple[str, str]] = []

    for row in rows:
        cell_id = str(row["cell_id"])
        trace = str(row["summary"] or "").strip()
        try:
            session_id, event_ts = _parse_cell_id(cell_id)
        except ValueError as exc:
            skipped.append((cell_id, str(exc)))
            continue
        if not trace:
            skipped.append((cell_id, "scene holds no summary text"))
            continue

        embedding = await compiler.embedder.embed(trace)
        cell = MemCell(
            id=cell_id,
            episodic_trace=trace,
            atomic_facts=[],
            emotional_tag=_neutral_tag(),
            foresight_signals=[],
            event_timestamp=event_ts,
            session_id=session_id,
            embedding=embedding,
            retrieval_count=0,
            explicit_importance=0.0,
            consolidated=True,
            scene_id=str(row["scene_id"]),
            distilled_at=now,
        )
        record.append(
            {
                "cell_id": cell.id,
                "session_id": cell.session_id,
                "event_timestamp": cell.event_timestamp.isoformat(),
                "scene_id": cell.scene_id,
                "episodic_trace": cell.episodic_trace,
                "embedding_dims": len(embedding or []),
            }
        )

        print(
            f"  {cell.id}\n"
            f"    session={cell.session_id} event={cell.event_timestamp.isoformat()}\n"
            f"    scene={cell.scene_id} dims={len(embedding or [])}\n"
            f"    trace={cell.episodic_trace[:160]!r}"
        )

        if apply:
            await repo.upsert_memcell(cell)
            restored.append(cell.id)

    backup_dir = Path.home() / "soul-restore-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"soul_restore_{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    backup.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat(),
                "applied": apply,
                "since": since.isoformat(),
                "cells": record,
                "skipped": [{"cell_id": c, "reason": r} for c, r in skipped],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nrecord saved: {backup}")

    if not apply:
        print(
            f"PREVIEW ONLY: {len(record)} cell(s) would be restored. Re-run with --apply."
        )
        return 0

    remaining = await pool.fetch(DANGLING_SCENES_SQL, since)
    print(
        f"restored={len(restored)} skipped={len(skipped)} still_dangling={len(remaining)}"
    )
    if remaining:
        print("WARNING: some scenes still reference a missing cell:")
        for row in remaining:
            print(f"  {row['cell_id']}")
        return 1
    return 0


def _local_midnight() -> datetime:
    """Today's midnight in the deployment's own timezone."""

    return (
        datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the cells (without this the run is a preview)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "only scenes created on or after this moment (ISO 8601); "
            "defaults to local midnight today"
        ),
    )
    args = parser.parse_args()
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        since = _local_midnight()
    return asyncio.run(_restore(apply=args.apply, since=since))


if __name__ == "__main__":
    raise SystemExit(main())
