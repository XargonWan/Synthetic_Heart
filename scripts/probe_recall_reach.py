"""Does the memory block her prompt builds actually reach the 14 backfilled summaries?

Replays the union the prompt engine runs for a free-text search (prompt_engine.py :3615-3669) for a
few token queries, and reports where each backfilled memory lands in the 100-row pool, or that it is
out of it. Keyword LIKE only, no embeddings, no model calls. Read-only.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LOG_DIR", str(ROOT / "tmp" / "verify_logs"))

from core.db import get_conn_ctx  # noqa: E402

POOL_MAX = 100

QUERIES: dict[str, str] = {
    "one distinctive word: rain": "rain",
    "his 00:28 message, as words": "Heheh no charge required i put in twice the",
    "a memory's own phrasing: minecraft": "minecraft",
    "an intimacy word: surrendering": "surrendering",
    "a mundane one: roof": "roof",
}


async def _fetch(conn, sql, params=None):
    async with conn.cursor() as cur:
        if params:
            await cur.execute(sql, params)
        else:
            await cur.execute(sql)
        return await cur.fetchall()


def _where(tokens, table):
    cols = ("content",) if table == "memories" else (
        "content", "personal_thought", "interaction_summary", "user_message"
    )
    clauses, params = [], []
    for tok in tokens:
        for col in cols:
            clauses.append(f"{col} LIKE %s")
            params.append("%" + tok + "%")
    return "(" + " OR ".join(clauses) + ")", params


async def main() -> None:
    async with get_conn_ctx() as conn:
        rows = await _fetch(
            conn,
            "SELECT id, created_at, content FROM memories WHERE source='compaction_backfill' ORDER BY id",
        )
        mine = {int(r["id"]): (str(r["created_at"])[:19], str(r["content"])) for r in rows}
        print(f"backfilled memories in the store: {len(mine)}")
        for mid, (ts, body) in list(mine.items())[:3]:
            print(f"  id={mid} created_at={ts} :: {body[:70].replace(chr(10), ' ')}")
        if len(mine) > 3:
            print(f"  ... and {len(mine) - 3} more")

        for label, text in QUERIES.items():
            tokens = [t for t in text.split() if t.strip()][:12]
            if not tokens:
                continue
            wm, pm = _where(tokens, "memories")
            wd, pd = _where(tokens, "ai_diary")
            sql = (
                f"SELECT 'memories' AS source, id, created_at, content FROM memories WHERE {wm} "
                f"UNION ALL "
                f"SELECT 'ai_diary' AS source, id, created_at, content FROM ai_diary WHERE {wd} "
                f"ORDER BY created_at DESC LIMIT %s"
            )
            pool = await _fetch(conn, sql, pm + pd + [POOL_MAX])
            ids = [int(r["id"]) for r in pool if r["source"] == "memories"]
            hits = [mid for mid in mine if mid in ids]
            ranks = {mid: ids.index(mid) + 1 for mid in hits}
            newest = str(pool[0]["created_at"])[:19] if pool else "-"
            print(f"\n### {label}")
            print(f"    tokens={tokens}")
            print(f"    pool={len(pool)} rows (limit {POOL_MAX}), newest row {newest}")
            print(f"    backfilled memories in the pool: {len(hits)} of {len(mine)}")
            if hits:
                for mid in sorted(hits, key=lambda m: ranks[m])[:5]:
                    print(f"      id={mid} rank={ranks[mid]}/{len(pool)} :: {mine[mid][1][:60].replace(chr(10), ' ')}")
            else:
                print("      NONE: every backfilled summary lost the race to newer diary rows")


if __name__ == "__main__":
    asyncio.run(main())
