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

    async def mock_get_conn_ctx():
        return DummyConn()

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", mock_get_conn_ctx)

    snippets = await p._get_context_snippets(limit=2)

    # Should include at least one diary snippet and at least one memory snippet
    assert any(s.startswith("[memory]") for s in snippets)
    assert any(s.startswith("[telegram_bot]") or s.startswith("[unknown]") or "diary" in s for s in snippets)
