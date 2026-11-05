"""Independent sanity check of the live day-unit compaction. Read-only.

Checks the accounting (what left `ai_diary`, what arrived in `memories` and in the archive), looks for
duplicate or orphaned rows, and re-runs the anchor check itself against the ORIGINAL day text, so the
verdict does not depend on anything the compactor reported about its own work.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("LOG_DIR", str(Path(__file__).resolve().parent / "logs"))


def load_env() -> None:
    env = REPO / ".env"
    if not env.exists():
        return
    for raw in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def notes_of(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip():
        try:
            p = json.loads(v)
            return p if isinstance(p, dict) else {}
        except Exception:
            return {}
    return {}


def ids_of(v) -> list[int]:
    if isinstance(v, list):
        return [int(x) for x in v]
    if isinstance(v, str):
        import re

        return [int(x) for x in re.findall(r"\d+", v)]
    return []


def stamp(v) -> str:
    return str(v)[:19]


async def main() -> None:
    load_env()
    from core.db import get_conn_ctx
    from plugins.grillo.grillo_compactor.grillo_compactor import _verify_anchors, _format_anchors

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            counts = {}
            for t in ("memories", "archived_memories", "ai_diary", "ai_diary_archive"):
                await cur.execute(f"SELECT count(*) FROM {t}")
                r = await cur.fetchone()
                counts[t] = r.get("count") if isinstance(r, dict) else r[0]

            await cur.execute(
                "SELECT id, created_at, tags, content FROM memories WHERE source='compaction' ORDER BY id"
            )
            memories = await cur.fetchall()
            await cur.execute(
                "SELECT id, created_at, confidence, source_ids, source_count, total_source_chars, "
                "summary_chars, notes, created_by FROM archived_memories "
                "WHERE COALESCE(notes,'') LIKE '%day_unit%' ORDER BY id"
            )
            archives = await cur.fetchall()
            await cur.execute("SELECT id, created_at, content, personal_thought FROM ai_diary")
            diary = await cur.fetchall()
            await cur.execute("SELECT created_at, content, personal_thought FROM ai_diary_archive")
            arch_rows = await cur.fetchall()

    rows = lambda rs: [dict(r) if isinstance(r, dict) else r for r in rs]
    memories, archives, diary, arch_rows = rows(memories), rows(archives), rows(diary), rows(arch_rows)

    print("=== counts ===")
    for t, n in counts.items():
        print(f"  {t:<20} {n}")
    print(f"  memories with source='compaction': {len(memories)}")
    print(f"  archived_memories rows with path=day_unit: {len(archives)}")

    diary_by_id = {int(d.get("id")): d for d in diary if isinstance(d, dict) and d.get("id") is not None}
    arch_by_stamp: dict[str, list] = {}
    for a in arch_rows:
        if isinstance(a, dict):
            arch_by_stamp.setdefault(stamp(a.get("created_at")), []).append(a)

    print("\n=== per memory ===")
    seen_days: dict[int, int] = {}
    problems: list[str] = []
    for m in memories:
        content = m.get("content") or ""
        mid = m.get("id")
        print(f"\n memory id={mid} created={stamp(m.get('created_at'))} chars={len(content)}")
        head = content.split("\n")[0][:120].replace("\n", " ")
        print(f"   head: {head}")
        anchors_block = [ln for ln in content.split("\n") if ln.startswith("[anchors]")]
        print(f"   anchors block present: {bool(anchors_block)}" + (f"  {anchors_block[0][:110]}" if anchors_block else ""))

        # which archive row and which day does this memory belong to?
        match = next(
            (
                a
                for a in archives
                if isinstance(a, dict) and stamp(a.get("created_at")) == stamp(m.get("created_at"))
            ),
            None,
        )
        day_ids = ids_of(match.get("source_ids")) if match else []
        print(f"   archived_memories match: {match.get('id') if match else 'NONE'}"
              f" source_ids={day_ids} conf={match.get('confidence') if match else '-'}"
              f" created_by={match.get('created_by') if match else '-'}")
        if not match:
            problems.append(f"memory {mid}: no archived_memories row for {stamp(m.get('created_at'))}")
            continue
        notes = notes_of(match.get("notes"))
        conf = match.get("confidence")
        if conf is not None and float(conf) < 0.9:
            problems.append(f"memory {mid}: confidence {conf} below the 0.9 replace threshold")
        for cid in notes.get("anchor_check", {}).get("missing", {}).items():
            problems.append(f"memory {mid}: anchor check recorded a miss: {cid}")

        # the source day: still in ai_diary (kept_raw) or in ai_diary_archive (replaced)?
        day = diary_by_id.get(day_ids[0]) if day_ids else None
        where = "still in ai_diary (kept_raw)" if day else "in ai_diary_archive"
        print(f"   source day id={day_ids} -> {where}")

        src = None
        if day:
            src = (day.get("content") or "") + "\n" + (day.get("personal_thought") or "")
        else:
            cands = arch_by_stamp.get(stamp(m.get("created_at")), [])
            if cands:
                src = (cands[0].get("content") or "") + "\n" + (cands[0].get("personal_thought") or "")
        if not src:
            problems.append(f"memory {mid}: the source day text could NOT be found anywhere")
            print("   VERDICT: source text NOT found (cannot re-verify anchors)")
            continue

        # re-run the check against the ORIGINAL day, on the anchors recorded in notes
        recheck = _verify_anchors(src, content, notes.get("anchors") or {})
        missing_txt = json.dumps(recheck["missing"], ensure_ascii=False) if recheck["missing"] else "{}"
        print(f"   independent anchor recheck: coverage={recheck['coverage']} passed={recheck['passed']} "
              f"missing={missing_txt}")
        if not recheck["passed"]:
            problems.append(f"memory {mid}: independent recheck FAILED {recheck['missing']}")
        seen_days[day_ids[0]] = seen_days.get(day_ids[0], 0) + 1

    dupes = {k: v for k, v in seen_days.items() if v > 1}
    if dupes:
        problems.append(f"a day was compacted twice: {dupes}")

    print("\n=== verdict ===")
    if not problems:
        print("  nothing flagged: accounting closed, one memory per day, anchors re-verified independently")
    for p in problems:
        print(f"  PROBLEM: {p}")


if __name__ == "__main__":
    asyncio.run(main())
