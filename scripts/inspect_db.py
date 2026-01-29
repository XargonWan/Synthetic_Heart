
import asyncio
import os
from core.db import get_conn_ctx
from dotenv import load_dotenv

load_dotenv()

async def inspect():
    print("Inspecting DB...")
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                print("Checking columns for grillo_activity_log...")
                await cur.execute("SHOW COLUMNS FROM grillo_activity_log")
                columns = await cur.fetchall()
                for col in columns:
                    print(col)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(inspect())
