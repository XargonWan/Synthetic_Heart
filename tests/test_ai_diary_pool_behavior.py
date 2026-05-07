import asyncio

import core.db as db
import plugins.ai_diary as ai_diary


def test_sync_add_diary_does_not_create_many_pools(monkeypatch):
    monkeypatch.setenv("SYNTH_TESTING", "1")
    monkeypatch.setenv("DB_MAX_POOLS", "1")
    ai_diary.PLUGIN_ENABLED = True

    async def fake_get_pool():
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        pool = db._pools_by_loop.get(loop_id)
        if pool is None:
            pool = object()
            db._pools_by_loop[loop_id] = pool
        return pool

    async def fake_upsert(*args, **kwargs):
        await db.get_pool()
        return None

    monkeypatch.setattr(db, "get_pool", fake_get_pool)
    monkeypatch.setattr(ai_diary, "_upsert_diary_impl", fake_upsert)

    # Clear any existing pools
    db._pools_by_loop.clear()

    for i in range(5):
        # Call sync wrapper repeatedly
        ai_diary.add_diary_entry(content=f"entry {i}")

    # Only one unique pool object should exist (we may map multiple loop_ids to it)
    unique_pools = set(db._pools_by_loop.values())
    assert len(unique_pools) <= 1
