"""Recover the compacted summaries that never reached `memories` (plan item F5).

Why this exists: the compaction runs of 2026-09-22 and 2026-09-23 archived their source diary rows
correctly but the `memories` insert never landed (`insert_memory` passed a `str` for a TIMESTAMPTZ and
swallowed the rejection). So `archived_memories` holds summaries that nothing can reach: recall queries
`memories`, and nothing reads `archived_memories`. The 14 rows in it are real content standing for 44
diary days, and this script copies them up into the table the prompt actually reads.

Properties, on purpose:

* **INSERT-only.** It never deletes or updates anything, so the worst case is a duplicate memory.
  `--undo` prints the one statement that removes what it wrote.
* **Dry run by default.** Nothing is written until `--apply`.
* **Idempotent.** Each written memory is tagged `archived_id:<n>`; a run skips an archive row that
  already has one. Re-running `--apply` therefore writes nothing the second time.
* **The same code path as production**, so it also proves the F1 fix: every row goes through
  `core.db.insert_memory`, which now coerces the timestamp and raises on failure instead of printing.
* Rows are dated by the archive row's own `created_at` (when the summary was made), because the days
  it covers are only known by id.

Usage:

    ./.venv/Scripts/python.exe scripts/backfill_compacted_memories.py            # preview
    ./.venv/Scripts/python.exe scripts/backfill_compacted_memories.py --apply    # write (14 rows)
    ./.venv/Scripts/python.exe scripts/backfill_compacted_memories.py --undo     # the DELETE to undo
    ./.venv/Scripts/python.exe scripts/backfill_compacted_memories.py --undo --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BACKFILL_SOURCE = "compaction_backfill"


def load_env() -> None:
    """.env into os.environ with setdefault, like main.py does."""
    env_path = REPO / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def notes_of(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def tag_list(value: object) -> list:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
            return [value]
        except Exception:
            return [value]
    return []


def anchors_line(notes: dict) -> str:
    """Render the anchors block the same way the compactor does, when the row carries one."""
    anchors = notes.get("anchors")
    if not isinstance(anchors, dict) or not anchors:
        return ""
    try:
        from plugins.grillo.grillo_compactor.grillo_compactor import _format_anchors

        return _format_anchors(anchors)
    except Exception:
        return ""


def memory_content(summary: str, notes: dict) -> str:
    """Prefer the model's concrete `detailed` text over the short `summary` label."""
    detailed = notes.get("detailed")
    if isinstance(detailed, list):
        detailed = "\n".join(str(v) for v in detailed if str(v).strip())
    detailed = (detailed or "").strip() if isinstance(detailed, str) else ""
    body = detailed or (summary or "").strip()
    block = anchors_line(notes)
    return f"{body}\n\n{block}" if block else body


async def collect() -> list[dict]:
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, tag, summary, source_ids, source_count, confidence, notes, "
                "compaction_level, created_at, created_by FROM archived_memories ORDER BY id ASC"
            )
            archived = await cur.fetchall()
            await cur.execute(
                "SELECT id, content, tags, source FROM memories WHERE source = %s",
                (BACKFILL_SOURCE,),
            )
            written = await cur.fetchall()

    norm = lambda r: dict(r) if isinstance(r, dict) else None
    archives = []
    for r in archived:
        row = norm(r)
        if row is None:  # positional fallback (id, tag, summary, source_ids, source_count, conf, notes, lvl, created_at, by)
            keys = ["id", "tag", "summary", "source_ids", "source_count", "confidence", "notes",
                    "compaction_level", "created_at", "created_by"]
            row = dict(zip(keys, r))
        notes = notes_of(row.get("notes"))
        row["notes_parsed"] = notes
        row["content"] = memory_content(row.get("summary") or "", notes)
        archives.append(row)

    already: set[int] = set()
    for r in written:
        row = norm(r) or {"tags": None}
        for token in tag_list(row.get("tags")):
            if token.startswith("archived_id:"):
                try:
                    already.add(int(token.split(":", 1)[1]))
                except ValueError:
                    pass
    return archives, already


async def main() -> int:
    ap = argparse.ArgumentParser(description="Copy compacted summaries into `memories` (insert-only).")
    ap.add_argument("--apply", action="store_true", help="actually write (default is a preview)")
    ap.add_argument("--undo", action="store_true", help="print (or with --apply run) the undo statement")
    ap.add_argument("--limit", type=int, default=0, help="only the first N rows")
    args = ap.parse_args()

    load_env()
    archives, already = await collect()
    pending = [r for r in archives if int(r["id"]) not in already]
    if args.limit:
        pending = pending[: args.limit]

    print(f"archived_memories rows: {len(archives)}")
    print(f"already backfilled     : {len(already)}")
    print(f"to write               : {len(pending)}")
    print()
    for r in pending:
        print(f"  id={int(r['id']):<3} {str(r.get('created_at'))[:16]}  "
              f"{len(r['content']):>5} chars  conf={r.get('confidence')}  "
              f"source_count={r.get('source_count')}  tag={str(r.get('tag'))[:28]}")
        print(f"      {r['content'][:150].replace(chr(10), ' ')}...")
    print()

    if args.undo:
        print("-- undo: removes exactly what this script writes, nothing else --")
        print(f"DELETE FROM memories WHERE source = '{BACKFILL_SOURCE}';")
        if not args.apply:
            print("(add --apply to run it)")
            return 0
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM memories WHERE source = %s", (BACKFILL_SOURCE,))
        print("done: backfilled memories removed.")
        return 0

    if not args.apply:
        print("preview only: nothing was written. Re-run with --apply to write these rows.")
        return 0

    from core.db import insert_memory

    written = failed = 0
    for r in pending:
        tags = tag_list(r.get("tag"))
        tags.append(f"archived_id:{int(r['id'])}")
        try:
            await insert_memory(
                content=r["content"],
                author="grillo",
                source=BACKFILL_SOURCE,
                tags=json.dumps(tags),
                emotion=None,
                intensity=None,
                emotion_state=None,
                timestamp=r.get("created_at"),
            )
            written += 1
            print(f"  wrote archived_id={int(r['id'])} ({len(r['content'])} chars)")
        except Exception as e:
            failed += 1
            print(f"  FAILED archived_id={int(r['id'])}: {type(e).__name__}: {e}")

    print()
    print(f"wrote {written} row(s), {failed} failure(s).")
    if written:
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM memories WHERE source = %s", (BACKFILL_SOURCE,)
                )
                row = await cur.fetchone()
        if isinstance(row, dict):
            count = row.get("count") or row.get("c") or 0
        else:
            count = row[0] if row else 0
        print(f"memories with source='{BACKFILL_SOURCE}' now: {count}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
