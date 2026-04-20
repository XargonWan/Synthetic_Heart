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

    # stub send_context_update to record sends (history sync now uses
    # context updates instead of send_text to avoid triggering model
    # responses for every synced message)
    sent = []

    async def fake_context_update(gid: int, text: str) -> None:
        sent.append((gid, text))

    mgr.send_context_update = fake_context_update

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

    # verify that send_context_update and save_chat_message were called
    assert any(
        gid == 999 and "hi" in text for gid, text in sent if isinstance(text, str)
    )
    assert any(item[0] == "replicate" for item in sent)


@pytest.mark.asyncio
async def test_send_context_update(monkeypatch):
    """send_context_update should forward text via send_client_content with role=system."""
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")
    logged: list[dict] = []

    # stub a session object — send_context_update uses send_client_content
    class DummySession:
        async def send_client_content(self, **kwargs) -> None:
            turns = kwargs.get("turns")
            text = ""
            role = ""
            if turns and hasattr(turns, "parts"):
                role = getattr(turns, "role", "")
                for p in turns.parts:
                    if hasattr(p, "text"):
                        text = p.text
            logged.append(
                {
                    "text": text,
                    "role": role,
                    "turn_complete": kwargs.get("turn_complete"),
                }
            )

    state = SimpleNamespace(
        is_active=True,
        generating=False,
        pending_context_updates=[],
        _session=DummySession(),
    )
    mgr._sessions[42] = state

    await mgr.send_context_update(42, "note")
    assert logged, "session method should be invoked"
    assert logged[0]["text"] == "[System Context Update] note"
    assert logged[0]["role"] == "user"
    assert logged[0]["turn_complete"] is False
