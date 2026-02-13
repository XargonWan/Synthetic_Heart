import asyncio
import pytest

import core.db as db


@pytest.mark.asyncio
async def test_reuse_existing_pool_when_max_reached(monkeypatch):
    """When number of pools >= DB_MAX_POOLS a pre-existing pool should be reused."""
    # Ensure synthetic testing mode so get_pool uses FakePool
    monkeypatch.setenv("SYNTH_TESTING", "1")
    monkeypatch.setenv("DB_MAX_POOLS", "1")

    # Put a fake pool object in the registry to simulate an existing pool
    db._pools_by_loop.clear()
    fake_pool = object()
    db._pools_by_loop[111] = fake_pool

    # Now call get_pool; because DB_MAX_POOLS == 1 it should reuse the fake pool
    pool = await db.get_pool()
    assert pool is fake_pool
    # Ensure current loop id also maps to that pool
    try:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
    except RuntimeError:
        loop_id = 0
    assert db._pools_by_loop.get(loop_id) is fake_pool


@pytest.mark.asyncio
async def test_creates_fake_pool_when_allowed(monkeypatch):
    """When DB_MAX_POOLS is large enough and SYNTH_TESTING=1 a FakePool should be created."""
    monkeypatch.setenv("SYNTH_TESTING", "1")
    monkeypatch.setenv("DB_MAX_POOLS", "10")

    db._pools_by_loop.clear()

    pool = await db.get_pool()
    # The FakePool created in testing mode should have an 'acquire' coroutine method
    assert hasattr(pool, "acquire")

    # Clean up
    db._pools_by_loop.clear()
