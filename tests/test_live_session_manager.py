import asyncio
from types import SimpleNamespace
from typing import Any, cast

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
    cast(Any, mgr)._sessions[999] = state

    # stub send_context_update to record sends (history sync now uses
    # context updates instead of send_text to avoid triggering model
    # responses for every synced message)
    sent = []

    async def fake_context_update(gid: int, text: str) -> None:
        sent.append((gid, text))

    cast(Any, mgr).send_context_update = fake_context_update

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
    mgr.history_sync_interval = 0
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
    """send_context_update should forward text via send_realtime_input."""
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")
    logged: list[str] = []

    # stub a session object — send_context_update now uses send_realtime_input
    class DummySession:
        async def send_realtime_input(self, **kwargs) -> None:
            logged.append(kwargs.get("text", ""))

    state = SimpleNamespace(
        is_active=True,
        generating=False,
        pending_context_updates=[],
        _session=DummySession(),
    )
    cast(Any, mgr)._sessions[42] = state

    await mgr.send_context_update(42, "note")
    assert logged, "session method should be invoked"
    assert logged[0] == "[System Context Update] note"


def test_extract_model_turn_payload_prefers_structured_parts():
    audio_bytes = b"\x01\x02\x03\x04"
    message = SimpleNamespace(
        data=None,
        text=None,
        server_content=SimpleNamespace(
            model_turn=SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        inline_data=SimpleNamespace(
                            mime_type="audio/pcm;rate=24000",
                            data=audio_bytes,
                        ),
                        text=None,
                    ),
                    SimpleNamespace(inline_data=None, text="hello from model"),
                ]
            )
        ),
    )

    audio, text = live_session_manager._extract_model_turn_payload(message)

    assert audio == audio_bytes
    assert text == "hello from model"


def test_build_initial_kick_text_uses_summon_text():
    kick = live_session_manager._build_initial_kick_text(
        is_reconnect=False,
        initial_user_message="come here, I need you in voice",
        initial_user_name="Alice",
    )

    assert "come here, I need you in voice" in kick
    assert "Alice" in kick
    assert "generic greeting" in kick


def test_build_initial_kick_text_for_reconnect_includes_history():
    kick = live_session_manager._build_initial_kick_text(
        is_reconnect=True,
        history_snippet="\n\nRecent conversation context:\nuser: hi",
    )

    assert "Voice session refreshed" in kick
    assert "Recent conversation context" in kick


@pytest.mark.asyncio
async def test_send_context_update_buffers_while_generating_and_flushes(monkeypatch):
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")
    logged: list[str] = []

    class DummySession:
        async def send_realtime_input(self, **kwargs) -> None:
            logged.append(kwargs.get("text", ""))

    state = SimpleNamespace(
        is_active=True,
        generating=True,
        pending_context_updates=[],
        _session=DummySession(),
    )
    cast(Any, mgr)._sessions[77] = state

    await mgr.send_context_update(77, "late note")

    assert logged == []
    assert state.pending_context_updates == ["late note"]

    state.generating = False
    await mgr._flush_pending_updates(77)

    assert logged == ["[System Context Update] late note"]
    assert state.pending_context_updates == []


@pytest.mark.asyncio
async def test_send_context_update_waits_for_playback_to_finish(monkeypatch):
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")
    logged: list[str] = []

    class DummySession:
        async def send_realtime_input(self, **kwargs) -> None:
            logged.append(kwargs.get("text", ""))

    state = SimpleNamespace(
        is_active=True,
        generating=False,
        pending_context_updates=[],
        _session=DummySession(),
    )
    cast(Any, mgr)._sessions[88] = state

    playing = True
    mgr.set_playback_active_callback(88, lambda: playing)

    await mgr.send_context_update(88, "speak later")
    await asyncio.sleep(0.15)

    assert logged == []
    assert state.pending_context_updates == ["speak later"]

    playing = False
    await asyncio.sleep(0.2)

    assert logged == ["[System Context Update] speak later"]
    assert state.pending_context_updates == []


@pytest.mark.asyncio
async def test_receive_loop_marks_generation_before_flush(monkeypatch):
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")
    generation_states: list[bool] = []
    state: live_session_manager.LiveSessionState | None = None

    async def fake_turn_complete(gid: int, user: str, model: str) -> None:
        generation_states.append(cast(Any, mgr)._sessions[gid].generating)
        assert state is not None
        state.is_active = False

    mgr.set_turn_complete_callback(fake_turn_complete)

    class DummySession:
        def receive(self):
            async def _iterator():
                yield SimpleNamespace(
                    server_content=SimpleNamespace(
                        model_turn=SimpleNamespace(parts=[]),
                        turn_complete=True,
                        interrupted=False,
                        generation_complete=True,
                        input_transcription=None,
                        output_transcription=SimpleNamespace(text="hello there"),
                    ),
                    tool_call=None,
                    data=None,
                    text=None,
                    session_resumption_update=None,
                    go_away=None,
                )

            return _iterator()

    state = live_session_manager.LiveSessionState(
        session_id="live_5_123",
        guild_id=5,
        channel_id=6,
    )
    state.is_active = True
    state._session = DummySession()
    cast(Any, mgr)._sessions[5] = state
    cast(Any, mgr)._session_epochs[5] = 1

    task = asyncio.create_task(mgr._receive_loop(5))
    await task

    assert generation_states == [True]


@pytest.mark.asyncio
async def test_send_tool_response_uses_function_response_list(monkeypatch):
    monkeypatch.setattr(live_session_manager, "_HAS_GENAI_SDK", True)
    mgr = live_session_manager.LiveSessionManager(api_key="x")
    calls: list[object] = []

    class DummySession:
        async def send_tool_response(self, **kwargs) -> None:
            calls.append(kwargs["function_responses"])

    state = SimpleNamespace(
        session_id="live_1_123",
        is_active=True,
        _session=DummySession(),
    )
    cast(Any, mgr)._sessions[1] = state

    await mgr.send_tool_response(
        session_id="live_1_123",
        call_id="call-1",
        name="get_emotion_state",
        result={"status": "ok", "value": 1},
    )

    assert calls, "tool response should be forwarded"
    assert isinstance(calls[0], list)
    assert len(calls[0]) == 1
