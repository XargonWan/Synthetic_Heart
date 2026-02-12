from types import SimpleNamespace
from datetime import datetime

import pytest

from plugins.memory_search import MemorySearchPlugin


def test_validate_payload_tags_ok():
    p = MemorySearchPlugin()
    action = {"payload": {"mode": "tags", "tags": ["monster", "austria"]}}
    errs = p.validate_payload(action)
    assert errs == []


def test_validate_payload_free_ok():
    p = MemorySearchPlugin()
    action = {"payload": {"mode": "free", "query": "austrian monster"}}
    errs = p.validate_payload(action)
    assert errs == []


def test_validate_payload_errors():
    p = MemorySearchPlugin()
    assert p.validate_payload({})  # missing mode -> returns errors
    assert p.validate_payload({"payload": {"mode": "tags", "tags": []}})  # missing tags
    assert p.validate_payload(
        {"payload": {"mode": "free", "query": "   "}}
    )  # empty query


class DummyCursor:
    def __init__(self, rows):
        self._rows = rows
        self._q = None
        self._params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def execute(self, q, params):
        self._q = q
        self._params = params

    async def fetchall(self):
        return self._rows


class DummyConn:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    def cursor(self):
        return DummyCursor(self._rows)


async def _run_execute(action_payload_rows):
    # Patch get_conn_ctx and request_llm_delivery
    import core.db as db
    import core.auto_response as ar

    async_called = {"called": False, "action_outputs": None, "original_context": None}

    async def fake_request_llm_delivery(
        action_outputs=None, original_context=None, action_type=None, **kwargs
    ):
        async_called["called"] = True
        async_called["action_outputs"] = action_outputs
        async_called["original_context"] = original_context
        return True

    orig_get_conn = db.get_conn_ctx
    orig_req = ar.request_llm_delivery
    try:
        db.get_conn_ctx = lambda: DummyConn(action_payload_rows)
        ar.request_llm_delivery = fake_request_llm_delivery

        p = MemorySearchPlugin()
        action = {"payload": {"mode": "tags", "tags": ["monster"], "max_results": 5}}
        ctx = {"interface": "webui"}
        orig_msg = SimpleNamespace(chat_id=123, interface_path="webui", message_id=99)
        res = await p.execute_action(action, ctx, None, orig_msg)
        assert res.get("processed") is True
        assert isinstance(res.get("results"), list)
        assert async_called["called"] is True
        assert isinstance(async_called["action_outputs"], list)
        return res
    finally:
        db.get_conn_ctx = orig_get_conn
        ar.request_llm_delivery = orig_req


@pytest.mark.asyncio
async def test_execute_action_tags_and_return_results():
    now = datetime.utcnow()
    rows = [
        ("memories", 1, now, "Found memory content example"),
        ("ai_diary", 2, now, "Diary content found"),
    ]
    res = await _run_execute(rows)
    assert len(res["results"]) == 2


@pytest.mark.asyncio
async def test_execute_action_free_and_return_results():
    now = datetime.utcnow()
    rows = [
        ("memories", 3, now, "Free search content matching tokens"),
    ]
    # Build plugin action
    import core.db as db
    import core.auto_response as ar

    async_called = {"called": False}

    async def fake_request_llm_delivery(
        action_outputs=None, original_context=None, action_type=None, **kwargs
    ):
        async_called["called"] = True
        return True

    orig_get_conn = db.get_conn_ctx
    orig_req = ar.request_llm_delivery
    try:
        db.get_conn_ctx = lambda: DummyConn(rows)
        ar.request_llm_delivery = fake_request_llm_delivery

        p = MemorySearchPlugin()
        action = {
            "payload": {"mode": "free", "query": "matching tokens", "max_results": 3}
        }
        ctx = {"interface": "webui"}
        orig_msg = SimpleNamespace(chat_id=321, interface_path="webui", message_id=55)
        res = await p.execute_action(action, ctx, None, orig_msg)
        assert res.get("processed") is True
        assert async_called["called"] is True
        assert len(res["results"]) == 1
    finally:
        db.get_conn_ctx = orig_get_conn
        ar.request_llm_delivery = orig_req
