
import asyncio
import os
import aiomysql
from dotenv import load_dotenv

load_dotenv()

async def inspect():
    host = os.getenv("DB_HOST", "127.0.0.1")
    port = int(os.getenv("DB_PORT", 3306))
    user = os.getenv("DB_USER", "root")
    password = os.getenv("DB_PASS", "")
    db_name = os.getenv("DB_NAME", "synth")

    print(f"Connecting to {user}@{host}:{port}/{db_name}...")
    try:
        conn = await aiomysql.connect(host=host, port=port, user=user, password=password, db=db_name)
        async with conn.cursor() as cur:
            print("Checking columns for grillo_activity_log...")
            await cur.execute(f"SHOW COLUMNS FROM grillo_activity_log")
            columns = await cur.fetchall()
            for col in columns:
                print(col)
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(inspect())
