#!/usr/bin/env python3
"""Retire situational notes a live store is still asserting but that are stale.

Trigger (live, 2026-09-23, the 2D instance): every prompt still carried

    [Temporal context]
    - [TSC EVENT] The wedding is tomorrow (2026-09-23); the day is Dee's, ...

on the morning the human had already said the wedding happened two mornings
earlier. The stand of stale rows `wedding day` / `wedding tomorrow` had windows
that had not run out yet, so the expiry sweep left them alone, and the debrief
had no way to name them for retirement (it was never shown the filed wording).
The code fix stops the next one: the extractor now sees the filed notes and the
block prints each note's own window. This script is the one-off repair of the
rows that were already standing.

Nothing is deleted. Each row only changes `status` to `superseded`, which is
what the debrief's own supersede path writes, and every changed row is dumped in
full to `repair_backup_<stamp>.json` in the repo root before the update. It is
re-runnable and idempotent: a subject that is no longer active is reported and
skipped.

Usage (from the repo root, so the plugin resolves the live store):

    ./.venv/Scripts/python.exe scripts/retire_stale_situational_notes.py \
        --subject "wedding day" --subject "wedding tomorrow"
    ./.venv/Scripts/python.exe scripts/retire_stale_situational_notes.py \
        --subject "wedding day" --subject "wedding tomorrow" --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from plugins.soul_plugin.soul_plugin import SoulPlugin  # noqa: E402


async def _retire(subjects: list[str], *, apply: bool) -> int:
    plugin = SoulPlugin()

    # The plugin falls back to an in-memory store when it cannot resolve the
    # runtime DSN, and that fallback would silently "succeed" while writing
    # nothing. Refuse instead.
    repo = plugin._repo
    if type(repo).__name__ != "PostgresSoulRepository":
        print(
            f"REFUSING to run: the plugin resolved {type(repo).__name__}, not the "
            "live Postgres store. Check SOUL_POSTGRES_DSN for this process."
        )
        return 2

    now = datetime.now(timezone.utc)
    active = await repo.list_active_situational_notes(now=now)
    wanted = {subject.strip().lower() for subject in subjects}
    matches = [
        note for note in active if str(note.subject or "").strip().lower() in wanted
    ]
    missing = sorted(
        wanted - {str(note.subject or "").strip().lower() for note in matches}
    )

    print(f"active notes in the store: {len(active)}")
    for note in matches:
        print(
            f"  match {note.id} | {note.subject} | "
            f"{note.valid_from} -> {note.valid_until} | {note.summary}"
        )
    for subject in missing:
        print(f"  no active note with subject {subject!r} (already retired?)")

    if not matches:
        print("nothing to retire")
        return 0
    if not apply:
        print("preview only: pass --apply to retire the rows above")
        return 0

    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    backup_path = REPO_ROOT / f"repair_backup_{stamp}.json"
    backup_path.write_text(
        json.dumps(
            {
                "reason": "stale situational notes retired after the 2026-09-23 "
                "wedding-notice incident",
                "changed_at": now.isoformat(),
                "rows_before": [asdict(note) for note in matches],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    retired = 0
    for note in matches:
        await repo.resolve_situational_note(note.id, new_status="superseded")
        retired += 1
    print(f"retired {retired} note(s) as superseded; rows saved in {backup_path.name}")

    after = await repo.list_active_situational_notes(now=datetime.now(timezone.utc))
    still = [
        note for note in after if str(note.subject or "").strip().lower() in wanted
    ]
    print(
        f"active notes now: {len(after)} (targeted subjects still active: {len(still)})"
    )
    return 0 if not still else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subject",
        action="append",
        required=True,
        help="exact subject of a filed note to retire (repeatable)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the change; without it the run is a preview",
    )
    args = parser.parse_args()
    return asyncio.run(_retire(cast(list[str], args.subject), apply=args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
