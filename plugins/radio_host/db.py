from __future__ import annotations

from core.db import _get_db_type, get_conn_ctx
from core.logging_utils import log_error, log_info

_table_initialized = False


async def init_radio_tables() -> None:
    global _table_initialized
    if _table_initialized:
        return

    is_postgres = _get_db_type() == "postgres"

    if is_postgres:
        id_col = "id SERIAL PRIMARY KEY"
        ts_col = "created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP"
    else:
        id_col = "id INT AUTO_INCREMENT PRIMARY KEY"
        ts_col = "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"

    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS radio_activity_log (
                        {id_col},
                        {ts_col},
                        track_title VARCHAR(512),
                        track_artist VARCHAR(512),
                        banter_text TEXT,
                        banter_audio_file VARCHAR(1024),
                        style VARCHAR(50),
                        status VARCHAR(50) DEFAULT 'injected'
                    )
                """)
                # Migrate existing tables that were created before the
                # timestamp column was added (schema guard).
                if is_postgres:
                    await cur.execute("""
                        ALTER TABLE radio_activity_log
                        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
                        DEFAULT CURRENT_TIMESTAMP
                    """)
                else:
                    # MariaDB doesn't support ADD COLUMN IF NOT EXISTS directly;
                    # use the information_schema to guard the ALTER.
                    await cur.execute("""
                        SELECT COUNT(*) FROM information_schema.COLUMNS
                        WHERE TABLE_SCHEMA = DATABASE()
                          AND TABLE_NAME  = 'radio_activity_log'
                          AND COLUMN_NAME = 'created_at'
                    """)
                    row = await cur.fetchone()
                    if (
                        row
                        and (row[0] if isinstance(row, tuple) else row.get("COUNT(*)"))
                        == 0
                    ):
                        await cur.execute(
                            "ALTER TABLE radio_activity_log "
                            "ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                        )
                # Separate index DDL; both backends support this form.
                await cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_radio_created_at
                    ON radio_activity_log (created_at)
                """)
                await conn.commit()
            _table_initialized = True
            log_info("[radio_host] DB tables initialized")
        except Exception as e:
            log_error(f"[radio_host] Failed to init DB tables: {e}")
            raise


async def trim_old_audio(keep: int = 30) -> list[str]:
    """Keep only the most recent *keep* rows that have an audio file.

    Older rows have their ``banter_audio_file`` column set to NULL in the DB.
    Returns the list of file paths that should now be deleted from disk.
    """
    await init_radio_tables()
    deleted_paths: list[str] = []
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, banter_audio_file FROM radio_activity_log "
                    "WHERE banter_audio_file IS NOT NULL AND banter_audio_file != '' "
                    "ORDER BY created_at DESC"
                )
                rows = await cur.fetchall()
                to_remove = rows[keep:]
                if not to_remove:
                    return []
                ids_to_clear = [r[0] for r in to_remove]
                for r in to_remove:
                    if r[1]:
                        deleted_paths.append(r[1])
                placeholders = ",".join(["%s"] * len(ids_to_clear))
                await cur.execute(
                    f"UPDATE radio_activity_log SET banter_audio_file = NULL "
                    f"WHERE id IN ({placeholders})",
                    ids_to_clear,
                )
                await conn.commit()
    except Exception as e:
        log_error(f"[radio_host] Failed to trim old audio: {e}")
    return deleted_paths
