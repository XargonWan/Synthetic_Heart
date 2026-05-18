from __future__ import annotations

from core.db import get_conn_ctx
from core.logging_utils import log_error, log_info

_table_initialized = False


async def init_radio_tables():
    global _table_initialized
    if _table_initialized:
        return
    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS radio_activity_log (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        track_title VARCHAR(512),
                        track_artist VARCHAR(512),
                        banter_text TEXT,
                        banter_audio_file VARCHAR(1024),
                        style VARCHAR(50),
                        status VARCHAR(50) DEFAULT 'injected'
                    )
                """)
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_radio_timestamp
                    ON radio_activity_log (timestamp)
                """)
                await conn.commit()
                _table_initialized = True
                log_info("[radio_host] DB tables initialized")
        except Exception as e:
            log_error(f"[radio_host] Failed to init DB tables: {e}")
            raise
