#!/usr/bin/env python3
"""Archive the repeats and the system text that crept into SOUL's chat cells.

Two kinds of cell do not belong in the live store:

* **system text** compiled as if it were a conversation (the diary consolidator's
  transcripts, web-search notices): reason ``housekeeping``;
* **repeats**: within one chat and one audience, cells that share almost every word
  with a more complete one (``core.soul.dedupe``): reason ``duplicate``, with the
  surviving cell recorded in ``merged_into``. The survivor is the fullest cell of the
  group and gains the participants of the ones it absorbs.

Nothing is deleted outright: cells go to ``mem_cells_archive`` and expire by their
reason (``SOUL_ARCHIVE_COPY_RETENTION_DAYS`` for these two). Preview is the default.

    uv run python scripts/dedupe_soul_cells.py
    uv run python scripts/dedupe_soul_cells.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import build_runtime_postgres_dsn  # noqa: E402
from core.soul.dedupe import (  # noqa: E402
    DUPLICATE_OVERLAP,
    content_words,
    overlap,
    union_participants,
)
from core.soul.repository import PostgresSoulRepository  # noqa: E402
import plugins.radio_host.recon_radio_status  # noqa: E402,F401  (registers the radio's sessions)
from core.soul.repository import _SHARED_CANDIDATE_MAX_CHARS  # noqa: E402
from core.soul.service import (  # noqa: E402
    is_housekeeping_session,
    on_demand_session_prefixes,
)


def plan_repeats(
    cells: list[dict[str, Any]], threshold: float = DUPLICATE_OVERLAP
) -> dict[str, list[str]]:
    """``keeper id -> ids it absorbs``, per (session, audience) group.

    The fullest cell of a group is tried first as a keeper, so the survivor is the
    most complete wording; ties go to the earliest.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        # ``pool`` is set for shared cells of ordinary chats, which may repeat each
        # other across chats; everything else is compared inside its own session.
        groups[(cell.get("pool", cell["session_id"]), cell["visibility"])].append(cell)
    absorbed: dict[str, list[str]] = {}
    for members in groups.values():
        members.sort(key=lambda c: (-len(c["words"]), c["event_timestamp"]))
        keepers: list[dict[str, Any]] = []
        for cell in members:
            home = next(
                (k for k in keepers if overlap(cell["words"], k["words"]) >= threshold),
                None,
            )
            if home is None:
                keepers.append(cell)
            else:
                absorbed.setdefault(home["id"], []).append(cell["id"])
    return absorbed


async def _run(apply: bool) -> int:
    repository = PostgresSoulRepository(dsn=build_runtime_postgres_dsn())
    pool = await repository._get_pool()
    try:
        rows = await pool.fetch(
            "SELECT id, session_id, visibility, episodic_trace, event_timestamp, participants "
            "FROM mem_cells WHERE source_kind = 'chat'"
        )
        system_ids: list[str] = []
        cells: list[dict[str, Any]] = []
        for row in rows:
            if is_housekeeping_session(row["session_id"]):
                system_ids.append(row["id"])
                continue
            session = row["session_id"]
            crosses_chats = (
                row["visibility"] == "shared"
                and not session.lower().startswith(on_demand_session_prefixes())
                and len(row["episodic_trace"]) <= _SHARED_CANDIDATE_MAX_CHARS
            )
            cells.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    **({"pool": "*shared*"} if crosses_chats else {}),
                    "visibility": row["visibility"],
                    "words": content_words(row["episodic_trace"]),
                    "event_timestamp": row["event_timestamp"],
                    "participants": row["participants"],
                }
            )
        plan = plan_repeats(cells)
        absorbed_total = sum(len(v) for v in plan.values())
        print(
            f"{len(rows)} chat cells: {len(system_ids)} system text, "
            f"{absorbed_total} repeats of {len(plan)} fuller cells, "
            f"{len(rows) - len(system_ids) - absorbed_total} stay"
        )
        if not apply:
            print("preview only; re-run with --apply to archive them")
            return 0

        done = await repository.delete_memcells(system_ids, reason="housekeeping")
        print(f"archived {done} system-text cells")
        by_id = {c["id"]: c for c in cells}
        merged = 0
        for keeper_id, victims in plan.items():
            keeper = await repository.get_memcell(keeper_id)
            if keeper is not None:
                people = [by_id[v]["participants"] for v in victims]
                parsed = [
                    json.loads(p) if isinstance(p, str) else (p or []) for p in people
                ]
                keeper.participants = union_participants(keeper.participants, *parsed)
                await repository.upsert_memcell(keeper)
            merged += await repository.delete_memcells(
                victims, reason="duplicate", merged_into=keeper_id
            )
        print(f"archived {merged} repeats")
        return 0
    finally:
        await repository.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    return asyncio.run(_run(parser.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(main())
