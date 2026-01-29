import asyncio
import os
import sys
import aiomysql

# Add project root to sys.path
sys.path.append(os.getcwd())

# Load environment variables
from dotenv import load_dotenv
load_dotenv(".env")

async def delete_weather_event():
    host = os.getenv("DB_HOST", "localhost")
    port = int(os.getenv("DB_PORT", "3306"))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASS", "")
    db_name = os.getenv("DB_NAME", "synth")

    try:
        conn = await aiomysql.connect(
            host=host, port=port, user=user, password=password, db=db_name
        )
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM scheduled_events WHERE created_by = 'admin_restore'")
            print(f"Deleted {cur.rowcount} event(s) created by admin_restore.")
            await conn.commit()
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(delete_weather_event())
