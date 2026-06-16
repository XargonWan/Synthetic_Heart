import pytest

from plugins.grillo.grillo_outreach import GrilloOutreachPlugin


@pytest.mark.asyncio
async def test_get_context_snippets_pulls_memories(monkeypatch):
    """_get_context_snippets should return both diary snippets and memory snippets (from `memories`)."""

    p = GrilloOutreachPlugin()

    class DummyCursor:
        def __init__(self):
            self._exec_count = 0

        async def execute(self, *args, **kwargs):
            # Track which query was executed (first -> ai_diary, second -> memories)
            self._exec_count += 1

        async def fetchall(self):
            # First fetchall -> ai_diary rows (content, interface, chat_id)
            if self._exec_count == 1:
                return [("diary content here", "telegram_bot", "12345")]
            # Second fetchall -> memories rows (content,)
            if self._exec_count == 2:
                return [("a memorable event happened",), ("another memory",)]
            return []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class DummyConn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return DummyCursor()

    def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    snippets = await p._get_context_snippets(limit=2)

    # Should include at least one diary snippet and at least one memory snippet
    assert any(s.startswith("[memory]") for s in snippets)
    assert any(
        s.startswith("[telegram_bot]") or s.startswith("[unknown]") or "diary" in s
        for s in snippets
    )


@pytest.mark.asyncio
async def test_generate_outreach_beat_records_target_metadata(monkeypatch):
    p = GrilloOutreachPlugin()
    captured = {}

    async def fake_get_target_interface_and_chat():
        return "telegram_bot", "12345"

    async def fake_get_context_snippets(limit: int = 5):
        return ["one", "two"]

    async def fake_create_activity_log(*args, **kwargs):
        captured["kwargs"] = kwargs
        return 77

    async def fake_enqueue_low_priority(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        captured["context_memory"] = context_memory
        return None

    monkeypatch.setattr(
        p, "_get_target_interface_and_chat", fake_get_target_interface_and_chat
    )
    monkeypatch.setattr(p, "_get_context_snippets", fake_get_context_snippets)
    monkeypatch.setattr(
        "plugins.grillo.grillo_impl.GrilloPlugin.create_activity_log",
        fake_create_activity_log,
    )
    monkeypatch.setattr(
        "core.message_queue.enqueue_low_priority",
        fake_enqueue_low_priority,
    )

    await p._generate_outreach_beat()

    assert captured["kwargs"]["metadata"]["origin"] == "grillo_outreach"
    assert captured["kwargs"]["metadata"]["target_interface"] == "telegram_bot"
    assert captured["kwargs"]["metadata"]["target_chat_id"] == "12345"
    assert captured["kwargs"]["metadata"]["context_count"] == 2
