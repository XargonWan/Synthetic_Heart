import pytest
from plugins.grillo.grillo_dream import GrilloDreamPlugin


@pytest.mark.asyncio
async def test_build_dream_prompt_contains_instructions():
    p = GrilloDreamPlugin()
    fragments = ["(chat:telegram_bot/1) Hello world", "(memory) Remember the red cat"]
    prompt = p._build_dream_prompt(fragments)

    assert "G.R.I.L.L.O. DREAM" in prompt
    assert "Fragments:" in prompt
    assert "create_personal_diary_entry" in prompt
    assert '"autonomous": true' in prompt
    assert "Rely SOLELY on the provided fragments" in prompt
    assert "Do NOT output any text outside a valid JSON object" in prompt
    assert "INSTRUCTIONS (dream):" in prompt
    assert "personal_thought" in prompt
    assert "emotions" in prompt


@pytest.mark.asyncio
async def test_collect_fragments_with_mocks(monkeypatch):
    p = GrilloDreamPlugin()

    async def mock_get_last_active_chats_verbose(n):
        return [(123, "Chat A"), (456, "Chat B")]

    async def mock_load_chat_history(interface_path):
        from collections import deque

        return deque([{"text": "hi there"}, {"text": "how are you?"}])

    async def mock_fetch_memories(limit):
        # Simulate DB rows
        return ["mem1", "mem2"]

    # Patch recent_chats and chat_history
    import core.recent_chats as recent_chats

    monkeypatch.setattr(
        recent_chats,
        "get_last_active_chats_verbose",
        mock_get_last_active_chats_verbose,
    )

    import core.chat_history_cache as chat_history_cache

    monkeypatch.setattr(chat_history_cache, "load_chat_history", mock_load_chat_history)

    # Patch DB call inside _collect_fragments by monkeypatching core.db.get_conn_ctx to a fake
    class DummyCursor:
        async def execute(self, *args, **kwargs):
            pass

        async def fetchall(self):
            return [("some memory",), ("another mem",)]

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

    fragments = await p._collect_fragments(4)
    assert isinstance(fragments, list)
    assert len(fragments) <= 4
    # fragments should contain markers like (chat: or (memory) and include sender metadata
    assert any(f.startswith("(chat:") or f.startswith("(memory)") for f in fragments)
    assert any("sender:" in f or "sender:" in f for f in fragments)


def test_seconds_until_next_run_returns_int():
    p = GrilloDreamPlugin()
    sec = p._seconds_until_next_run("05:00")
    assert isinstance(sec, int)
    assert 0 <= sec <= 24 * 3600


def test_recall_last_dream_is_a_recon_not_an_action():
    """recall_last_dream is a Recon contribution, not a standalone action."""
    p = GrilloDreamPlugin()
    # No longer advertised as an action.
    assert "recall_last_dream" not in p.get_supported_action_types()
    assert "recall_last_dream" not in p.get_supported_actions()
    assert "static_inject" in p.get_supported_action_types()
    # Now exposes the Recon plugin contract.
    assert p.get_recon_key() == "recall_last_dream"
    assert isinstance(p.get_recon_instruction(), str)


def test_extract_dream_text_from_action_envelope():
    """The dream text is pulled from the JSON action payload content."""
    envelope = (
        '{"actions": [{"type": "create_personal_diary_entry", '
        '"payload": {"content": "A corridor of code narrowed around me."}}]}'
    )
    assert (
        GrilloDreamPlugin._extract_dream_text(envelope)
        == "A corridor of code narrowed around me."
    )
    assert GrilloDreamPlugin._extract_dream_text(None) is None
    assert GrilloDreamPlugin._extract_dream_text("not json") is None


def test_extract_dream_text_strips_json_label_prefix():
    """A bare leading 'JSON' label (with optional colon) must be stripped before
    parsing so the real dream text is extracted instead of falling back to the
    shared daily diary recap row."""
    envelope = (
        'JSON\n{"actions": [{"type": "create_personal_diary_entry", '
        '"payload": {"content": "Glowing repositories filled the dream."}}]}'
    )
    assert (
        GrilloDreamPlugin._extract_dream_text(envelope)
        == "Glowing repositories filled the dream."
    )


def test_extract_dream_text_strips_markdown_fences():
    """Markdown code fences around the JSON envelope must also be handled."""
    envelope = (
        '```json\n{"actions": [{"type": "create_personal_diary_entry", '
        '"payload": {"content": "Half-built interfaces awaited."}}]}\n```'
    )
    assert (
        GrilloDreamPlugin._extract_dream_text(envelope)
        == "Half-built interfaces awaited."
    )


@pytest.mark.asyncio
async def test_parse_recon_response_skips_when_not_requested():
    """When the Recon call does not flag a dream request, contribute nothing."""
    p = GrilloDreamPlugin()
    assert await p.parse_recon_response({"wants_dream": False}) == []
    assert await p.parse_recon_response({}) == []
    assert await p.parse_recon_response(None) == []


@pytest.mark.asyncio
async def test_parse_recon_response_contributes_last_dream(monkeypatch):
    """When flagged, contribute the extracted last-dream text as a snippet."""
    import datetime as _dt

    p = GrilloDreamPlugin()

    envelope = (
        '{"actions": [{"type": "create_personal_diary_entry", '
        '"payload": {"content": "I dreamt of a red cat on the wire."}}]}'
    )
    executed_at = _dt.datetime(2026, 1, 1, 5, 0, 0)

    class DummyCursor:
        async def execute(self, *args, **kwargs):
            pass

        async def fetchone(self):
            return (envelope, None, executed_at)

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

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", lambda: DummyConn())

    result = await p.parse_recon_response({"wants_dream": True})
    assert len(result) == 1
    contrib = result[0]
    assert contrib["type"] == "snippet"
    assert contrib["source"] == "grillo_dream"
    assert "I dreamt of a red cat on the wire." in contrib["content"]


@pytest.mark.asyncio
async def test_parse_recon_response_no_dream_on_record(monkeypatch):
    """When no dream exists, contribute nothing even if requested."""
    p = GrilloDreamPlugin()

    class DummyCursor:
        async def execute(self, *args, **kwargs):
            pass

        async def fetchone(self):
            return None

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

    import core.db as cdb

    monkeypatch.setattr(cdb, "get_conn_ctx", lambda: DummyConn())

    assert await p.parse_recon_response({"wants_dream": True}) == []
