import asyncio

import pytest

from core.db import ensure_core_tables, get_conn_ctx
from core.config import set_base_cortex


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
