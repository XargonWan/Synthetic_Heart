
import asyncio
import os
import aiomysql
from dotenv import load_dotenv

load_dotenv()

async def fix_schema():
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", 3306))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASS", "")
    db_name = os.getenv("DB_NAME", "synth")

    print(f"Connecting to {user}@{host}:{port}/{db_name}...")
    try:
        conn = await aiomysql.connect(host=host, port=port, user=user, password=password, db=db_name)
        async with conn.cursor() as cur:
            print("Dropping bad columns from grillo_activity_log...")
            try:
                await cur.execute("ALTER TABLE grillo_activity_log DROP COLUMN setting_key")
                print("Dropped setting_key")
            except Exception as e:
                print(f"Error dropping setting_key: {e}")

            try:
                await cur.execute("ALTER TABLE grillo_activity_log DROP COLUMN setting_value")
                print("Dropped setting_value")
            except Exception as e:
                print(f"Error dropping setting_value: {e}")
                
            try:
                await cur.execute("ALTER TABLE grillo_activity_log DROP COLUMN description")
                print("Dropped description")
            except Exception as e:
                print(f"Error dropping description: {e}")

            # Also check if executed_at exists, if not add it, but it seemed to exist in inspection.

        conn.close()
        print("Schema fix complete.")
    except Exception as e:
        print(f"Connection Error: {e}")

if __name__ == "__main__":
    asyncio.run(fix_schema())
