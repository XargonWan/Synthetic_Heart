import pytest
from plugins.grillo.grillo_impl import GrilloPlugin


@pytest.mark.asyncio
async def test_create_action_exec_handles_db_failure(monkeypatch):
    async def fake_get_conn_ctx():
        raise Exception("db unavailable")

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)
    monkeypatch.setattr(
        GrilloPlugin,
        "_fallback_write_action_exec",
        classmethod(lambda cls, exec_obj: _async_none()),
    )

    res = await GrilloPlugin.create_action_exec(
        activity_log_id=1, action_index=0, action_type="test", payload={"a": 1}
    )
    assert res is None


@pytest.mark.asyncio
async def test_fetch_action_execs_handles_db_failure(monkeypatch):
    async def fake_get_conn_ctx():
        raise Exception("db unavailable")

    monkeypatch.setattr("core.db.get_conn_ctx", fake_get_conn_ctx)
    monkeypatch.setattr(
        GrilloPlugin,
        "_fallback_read_action_execs",
        classmethod(lambda cls, activity_ids: _async_empty_dict()),
    )

    res = await GrilloPlugin.fetch_action_execs([1, 2, 3])
    assert isinstance(res, dict)
    assert res == {}


async def _async_none():
    return None


async def _async_empty_dict():
    return {}
