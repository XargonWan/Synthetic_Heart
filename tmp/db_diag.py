import os
import asyncio
from core.db import get_conn_ctx

# Diagnostic helper — do NOT hardcode credentials. Use the project's DB context (reads config/env).
# Example usage: DB connection will be picked up from the environment or the running container config.

async def _run_diagnostics(age_days: int = 30):
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute('SELECT COUNT(*) AS cnt_total FROM memories WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)', (age_days,))
            row = await cur.fetchone()
            print('count_older_30_days:', (row or {}).get('cnt_total'))

            await cur.execute("SELECT COUNT(*) AS cnt_tagged FROM memories WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY) AND tags IS NOT NULL AND tags <> '' AND tags <> '[]'", (age_days,))
            row = await cur.fetchone()
            print('count_tagged_older_30_days:', (row or {}).get('cnt_tagged'))

            await cur.execute('SELECT id, tags, timestamp, CHAR_LENGTH(content) as content_len FROM memories WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY) ORDER BY timestamp ASC LIMIT 20', (age_days,))
            rows = await cur.fetchall()
            print('\nsample_oldest_rows (up to 20):')
            for r in rows or []:
                print(r)

            await cur.execute('SELECT COUNT(*) AS cnt_total_all FROM memories')
            row = await cur.fetchone()
            print('\ncount_all_memories:', (row or {}).get('cnt_total_all'))

            await cur.execute("SELECT COUNT(*) AS cnt_tagged_all FROM memories WHERE tags IS NOT NULL AND tags <> '' AND tags <> '[]'")
            row = await cur.fetchone()
            print('count_tagged_all:', (row or {}).get('cnt_tagged_all'))

if __name__ == '__main__':
    asyncio.run(_run_diagnostics())
