import asyncio

import core.db as db
import plugins.ai_diary as ai_diary


def test_emotions_to_state_map_from_canonical_list():
    state = ai_diary._emotions_to_state_map(
        [{"type": "joy", "intensity": 7}, {"type": "love", "intensity": 6.5}]
    )
    assert state == {"joy": 7.0, "love": 6.5}


def test_emotions_to_state_map_from_dict_and_names():
    state = ai_diary._emotions_to_state_map({"joy": 7, "love": 6})
    assert state == {"joy": 7.0, "love": 6.0}

    state = ai_diary._emotions_to_state_map(["happy"])
    assert state == {}


def test_emotions_to_state_map_skips_garbage():
    assert ai_diary._emotions_to_state_map(None) == {}
    assert ai_diary._emotions_to_state_map([]) == {}
    assert ai_diary._emotions_to_state_map([{"type": "joy"}]) == {}
    assert ai_diary._emotions_to_state_map([{"intensity": 5}]) == {}
    assert ai_diary._emotions_to_state_map("not emotions") == {}


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
