import asyncio
import os
import sys

# Add current directory to path so we can import core
sys.path.append(os.getcwd())

from core.db import get_conn_ctx


async def check_schema():
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                tables = ["memories", "ai_diary", "chat_history_cache"]
                for table in tables:
                    await cur.execute(f"SHOW CREATE TABLE {table}")
                    schema = await cur.fetchone()
                    print(f"\nSchema for {table}:\n{schema[1]}")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(check_schema())
