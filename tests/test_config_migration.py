import asyncio

import pytest

from core.config_manager import config_registry
from core.db import ensure_core_tables, get_conn_ctx
from core.config import set_base_cortex


@pytest.mark.asyncio
async def test_legacy_base_cortex_migrates_to_config():
    """If a legacy `settings.base_cortex` exists, _load_from_db should pick it up
    and persist it into the modern `config` table (migration).
    """
    await ensure_core_tables()

    # Ensure no existing config value for BASE_CORTEX
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM config WHERE config_key = %s", ("BASE_CORTEX",))
            await cur.execute(
                "REPLACE INTO settings (`setting_key`, `value`) VALUES (%s, %s)",
                ("base_cortex", "selenium_gemini"),
            )
            await conn.commit()

    # _load_from_db should return the legacy value and persist it into `config`
    raw = await config_registry._load_from_db("BASE_CORTEX")
    assert raw == "selenium_gemini"

    # Verify the migrated value is now present in `config`
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM config WHERE config_key = %s", ("BASE_CORTEX",))
            row = await cur.fetchone()
            assert row and row[0] == "selenium_gemini"


@pytest.mark.asyncio
async def test_set_base_cortex_persists_to_config_table():
    await ensure_core_tables()

    # Persist a new base cortex via public API
    await set_base_cortex("selenium_gemini")

    # Ensure config table contains the entry
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM config WHERE config_key = %s", ("BASE_CORTEX",))
            row = await cur.fetchone()
            assert row and row[0] == "selenium_gemini"
