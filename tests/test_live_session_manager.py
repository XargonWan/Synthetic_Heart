import asyncio
from types import SimpleNamespace

import pytest

from core import live_session_manager


class DummyCursor:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None
        self.last_params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, q, params):
        self.last_query = q
        self.last_params = params

    async def fetchall(self):
        return self.rows


class DummyConn:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def cursor(self):
        return DummyCursor(self._rows)


@pytest.mark.asyncio
async def test_history_sync_loop(monkeypatch):
    """Loop should load new guild messages, forward and replicate them."""
    # ensure LiveSessionManager can be instantiated without genai
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")

    # create a fake active session state for guild 999
    state = SimpleNamespace(is_active=True, last_injected_ts=None)
    mgr._sessions[999] = state

    # stub send_text to record sends
    sent = []

    async def fake_send(gid, text):
        sent.append((gid, text))

    mgr.send_text = fake_send

    # stub chat history functions
    msgs = [
        {
            "text": "hi",
            "sender_name": "A",
            "sender_id": "1",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ]

    def fake_load(gid, since=None, limit=100):
        assert gid == 999
        return asyncio.Future()

    # we make fake async function manually
    async def fake_load_async(gid, since=None, limit=100):
        return msgs

    async def fake_save(
        interface_path, message_text, sender_name=None, sender_id=None, timestamp=None
    ):
        # just record that replication happened
        sent.append(("replicate", interface_path, message_text))
        return True

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history_for_guild", fake_load_async
    )
    monkeypatch.setattr("core.chat_history_cache.save_chat_message", fake_save)

    # shorten interval so loop iterates quickly
    mgr.history_sync_interval = 0.01
    # run loop briefly
    task = asyncio.create_task(mgr._history_sync_loop(999))
    await asyncio.sleep(0.05)
    # deactivate to exit
    mgr._sessions[999].is_active = False
    await task

    # verify that send_text and save_chat_message were called
    assert (999, "hi") in sent
    assert any(item[0] == "replicate" for item in sent)


@pytest.mark.asyncio
async def test_send_context_update(monkeypatch):
    """send_context_update should forward text to the session and log info."""
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")
    logged = []
    # stub a session object
    class DummySession:
        async def send_client_content(self, turns=None, turn_complete=False):
            logged.append((turns, turn_complete))

    state = SimpleNamespace(is_active=True, _session=DummySession(), generating=False, pending_context_updates=[])
    mgr._sessions[42] = state

    # immediate send when not generating
    await mgr.send_context_update(42, "note")
    assert logged, "session method should be invoked"
    turns, tc = logged[0]
    assert tc is False
    assert isinstance(turns, live_session_manager.types.Content)
    assert turns.role == "system"
    assert "note" in turns.parts[0].text

    # now simulate model generating, buffer two updates and flush
    logged.clear()
    state.generating = True
    await mgr.send_context_update(42, "buffer1")
    await mgr.send_context_update(42, "buffer2")
    # nothing sent yet
    assert logged == []
    assert state.pending_context_updates == ["buffer1", "buffer2"]

    # flush explicitly using helper
    state.generating = False
    await mgr._flush_pending_updates(42)
    # verify flush submitted both updates to session
    assert len(logged) == 2
