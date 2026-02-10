
import asyncio
import os
import sys

# Add repository root to path so we can import core
sys.path.append(os.getcwd())

# Fix for Windows Event Loop runtime error with aiomysql
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from core.db import get_conn_ctx

async def fix_schema():
    print("Connecting to database...")
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                print("Dropping 'emotion_diary' table (to clear schema mismatch)...")
                await cur.execute("DROP TABLE IF EXISTS emotion_diary")
                
                print("Recreating 'emotion_diary' table with correct schema...")
                await cur.execute("""
                    CREATE TABLE emotion_diary (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        source VARCHAR(100),
                        event VARCHAR(100),
                        emotion VARCHAR(100),
                        intensity FLOAT,
                        state VARCHAR(100),
                        trigger_condition VARCHAR(255),
                        decision_logic TEXT,
                        next_check DATETIME,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_timestamp (timestamp)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                print("Table 'emotion_diary' created successfully.")
                
                # Verify emotion_state too
                print("Checking 'emotion_state' schema...")
                await cur.execute("DESCRIBE emotion_state")
                rows = await cur.fetchall()
                print(f"emotion_state columns: {[r[0] for r in rows]}")
                
    except Exception as e:
        print(f"Failed to fix schema: {e}")
        if "1040" in str(e):
            print("\nCRITICAL: Database has 'Too many connections'.")
            print("Please STOP the running 'uv run main.py' process before running this script.")

if __name__ == "__main__":
    asyncio.run(fix_schema())
