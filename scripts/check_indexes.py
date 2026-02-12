import asyncio
import os
import sys

# Add current directory to path so we can import core
sys.path.append(os.getcwd())

from core.db import get_conn_ctx


async def check_indexes():
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                tables = ["memories", "ai_diary", "chat_history_cache"]
                for table in tables:
                    await cur.execute(f"SHOW INDEX FROM {table}")
                    indexes = await cur.fetchall()
                    print(f"\nIndexes for {table}:")
                    for idx in indexes:
                        print(f"  {idx[2]} ({idx[4]}) - Type: {idx[10]}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(check_indexes())
