import asyncio
import pytest

import core.db as db


@pytest.mark.asyncio
async def test_reuse_existing_pool_for_current_loop(monkeypatch):
    """get_pool should reuse an already-cached pool for the active event loop."""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)

    db._pools_by_loop.clear()
    fake_pool = object()
    db._pools_by_loop[loop_id] = fake_pool

    pool = await db.get_pool()
    assert pool is fake_pool
    assert db._pools_by_loop.get(loop_id) is fake_pool


@pytest.mark.asyncio
async def test_creates_pool_via_selected_backend(monkeypatch):
    """get_pool should create and cache a pool via the configured backend."""
    created = {"count": 0}

    async def fake_create_pool(**kwargs):
        created["count"] += 1
        return {"pool": "fake", "kwargs": kwargs}

    monkeypatch.setattr(db, "_get_db_type", lambda: "mysql")
    monkeypatch.setattr(db.aiomysql, "create_pool", fake_create_pool)

    db._pools_by_loop.clear()

    pool = await db.get_pool()
    assert pool["pool"] == "fake"
    assert created["count"] == 1

    # A second call in the same loop should reuse the cached pool.
    same_pool = await db.get_pool()
    assert same_pool is pool
    assert created["count"] == 1

    db._pools_by_loop.clear()
