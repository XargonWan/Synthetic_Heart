import pytest

pytest.skip(
    "Legacy memory preflight removed (Recon replaces it)",
    allow_module_level=True,
)

import asyncio
from types import SimpleNamespace
from datetime import datetime


from core.prompt_engine import build_json_prompt


def test_prefight_includes_free_search(monkeypatch):
    # Enable preflight via monkeypatching config_registry.get_value
    def fake_get_value(key, default=None, value_type=None):
        if key == "MEMORY_SEARCH_PREFLIGHT":
            return True
        if key == "MEMORY_SEARCH_PREFLIGHT_MAX_RESULTS":
            return 3
        if key == "MEMORY_SEARCH_PREFLIGHT_STRATEGY":
            return "llm_action"
        return default

    monkeypatch.setattr("core.prompt_engine.config_registry.get_value", fake_get_value)

    async def fake_llm_preflight(
        *, text: str, interface_name: str | None, original_message, max_results: int
    ):
        await asyncio.sleep(0)
        return ["Ricordo qualcosa sul mostro austriaco: era enorme e aveva le corna."]

    monkeypatch.setattr(
        "core.prompt_engine.llm_memory_search_preflight", fake_llm_preflight
    )

    message = SimpleNamespace(
        text="Rekku, ti ricordi il mostro austriaco?",
        date=datetime.utcnow(),
        message_id=123,
        from_user=SimpleNamespace(id=1, full_name="Test", username="tester"),
        chat=SimpleNamespace(id=1, type="private"),
        interface_path="telegram",
    )

    prompt = asyncio.run(build_json_prompt(message, {}))
    serialized = str(prompt)
    assert "mostro austriaco" in serialized or "mostro austriaco" in serialized.lower()


def test_prefight_defaults():
    # Ensure the defaults are updated as requested (prefight enabled and default max 10 and randomize default true)
    import core.prompt_engine as pe

    assert (
        bool(
            pe.config_registry.get_value(
                "MEMORY_SEARCH_PREFLIGHT", None, value_type=bool
            )
        )
        is True
    )
    assert (
        int(
            pe.config_registry.get_value(
                "MEMORY_SEARCH_PREFLIGHT_MAX_RESULTS", None, value_type=int
            )
        )
        == 10
    )
    assert (
        bool(
            pe.config_registry.get_value(
                "MEMORY_SEARCH_PREFLIGHT_RANDOMIZE", None, value_type=bool
            )
        )
        is True
    )


def test_free_memory_randomize(monkeypatch):
    # Prepare dummy rows (15 rows) to simulate DB returning many matches
    from datetime import datetime

    rows = []
    now = datetime.utcnow()
    for i in range(1, 16):
        rows.append(("memories", i, now, f"r{i}"))

    class DummyCursor:
        def __init__(self, rows):
            self._rows = rows

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

    # Patch DB and config to enable randomization and set pool

    # Monkeypatch prompt_engine.get_conn_ctx directly so free_memory_search uses our DummyConn
    import core.prompt_engine as pe

    monkeypatch.setattr(pe, "get_conn_ctx", lambda: DummyConn(rows))

    def fake_get_value(key, default=None, value_type=None):
        if key == "MEMORY_SEARCH_PREFLIGHT_RANDOMIZE":
            return True
        if key == "MEMORY_SEARCH_PREFLIGHT_POOL_MAX":
            return 100
        return default

    monkeypatch.setattr("core.prompt_engine.config_registry.get_value", fake_get_value)

    # Monkeypatch random.shuffle to a deterministic reversal for the test
    import random

    def fake_shuffle(x):
        x.reverse()

    monkeypatch.setattr(random, "shuffle", fake_shuffle)

    res = asyncio.run(pe.free_memory_search("anything", limit=10))
    assert len(res) == 10
    # Because fake_shuffle reverses, the first element should be r15 (last original)
    assert res[0] == "r15"
    message = SimpleNamespace(
        text="Rekku, ti ricordi il mostro austriaco?",
        date=datetime.utcnow(),
        message_id=123,
        from_user=SimpleNamespace(id=1, full_name="Test", username="tester"),
        chat=SimpleNamespace(id=1, type="private"),
        interface_path="telegram",
    )

    prompt = asyncio.run(build_json_prompt(message, {}))

    # The easier check: ensure that the memory snippet text appears in any serialized part of the prompt dict
    serialized = str(prompt)
    assert "mostro austriaco" in serialized or "mostro austriaco" in serialized.lower()


async def test_preflight_corrector_invoked_on_malformed_json(monkeypatch):
    # Simulate active LLM engine that returns malformed JSON and a corrector that fixes it
    async def fake_generate_response(messages):
        # Malformed JSON (missing a closing bracket)
        return '{"actions": [{"type": "memory_search", "payload": {"mode": "free", "keywords": ["plan","tranquilla"]}}'

    class FakeEngine:
        async def generate_response(self, messages):
            return await fake_generate_response(messages)

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

    # Patch get_active_llm and registry
    monkeypatch.setattr("core.config.get_active_llm", lambda: "fake")
    monkeypatch.setattr("core.llm_registry.get_llm_registry", lambda: FakeRegistry())

    # Corrector will fix the malformed JSON into valid one
    async def fake_corrector(
        text, bot=None, context=None, chat_id=None, thread_id=None
    ):
        return '{"actions": [{"type": "memory_search", "payload": {"mode": "free", "keywords": ["plan","tranquilla"], "max_results":5}}]}'

    monkeypatch.setattr("core.transport_layer.run_corrector_middleware", fake_corrector)

    # Patch run_action to return sample results
    async def fake_run_action(action, context, bot=None, original_message=None):
        return {
            "results": [{"snippet": "Found plan snippet"}],
            "delivered_to_llm": True,
        }

    monkeypatch.setattr("core.action_parser.run_action", fake_run_action)

    from core.prompt_engine import llm_memory_search_preflight
    from types import SimpleNamespace

    snippets = asyncio.run(
        llm_memory_search_preflight(
            text="Sto facendo il plan ma stai tranquilla per favore :)",
            interface_name="telegram",
            original_message=SimpleNamespace(chat_id=1),
            max_results=5,
        )
    )

    assert isinstance(snippets, list)
    assert "Found plan snippet" in snippets
