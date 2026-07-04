from plugins import memory_search


class DummyCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, q, params):
        return None

    async def fetchall(self):
        return []


class DummyConn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def cursor(self):
        return DummyCursor()


async def test_execute_action_preflight_no_error(monkeypatch):
    plugin = memory_search.MemorySearchPlugin()

    # Patch _build_query_and_params to return a non-empty query (so it proceeds past early returns)
    plugin._build_query_and_params = lambda payload, max_results: ("SELECT 1", [])

    # Patch DB context manager to avoid real DB calls
    monkeypatch.setattr(memory_search, "get_conn_ctx", lambda: DummyConn())

    res = await plugin.execute_action(
        {"payload": {"mode": "tags", "tags": ["x"]}}, {"preflight": True}, None, None
    )

    assert res["processed"] is True
    # delivered_to_llm must be present and False in preflight mode
    assert res.get("delivered_to_llm") is False


async def test_execute_action_requests_delivery_when_not_preflight(monkeypatch):
    plugin = memory_search.MemorySearchPlugin()

    plugin._build_query_and_params = lambda payload, max_results: ("SELECT 1", [])
    monkeypatch.setattr(memory_search, "get_conn_ctx", lambda: DummyConn())

    async def fake_request_llm_delivery(
        action_outputs, original_context, action_type="memory_search"
    ):
        return True

    monkeypatch.setattr(
        memory_search, "request_llm_delivery", fake_request_llm_delivery
    )

    res = await plugin.execute_action(
        {"payload": {"mode": "tags", "tags": ["x"]}}, {}, None, None
    )

    assert res["processed"] is True
    assert res.get("delivered_to_llm") is True


async def test_execute_action_skips_delivery_for_action_result_evaluation(monkeypatch):
    plugin = memory_search.MemorySearchPlugin()

    plugin._build_query_and_params = lambda payload, max_results: ("SELECT 1", [])
    monkeypatch.setattr(memory_search, "get_conn_ctx", lambda: DummyConn())

    def fake_request_llm_delivery(
        action_outputs, original_context, action_type="memory_search"
    ):
        raise AssertionError(
            "request_llm_delivery should not be called during action-result delivery"
        )

    monkeypatch.setattr(
        memory_search, "request_llm_delivery", fake_request_llm_delivery
    )

    res = await plugin.execute_action(
        {"payload": {"mode": "tags", "tags": ["x"]}},
        {"system_message": {"is_action_result_delivery": True}},
        None,
        None,
    )

    assert res["processed"] is True
    assert res.get("delivered_to_llm") is False
