import pytest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from typing import Any, cast

from core.webui import SynthWebUIInterface


@pytest.mark.asyncio
async def test_send_message_persists_when_no_websocket():
    webui = SynthWebUIInterface()
    session_id = "no-ws-session"

    # Ensure connections has no websocket for this session
    if session_id in webui.connections:
        webui.connections.pop(session_id)

    text = "Persisted message"

    with (
        patch(
            "core.chat_context_manager.save_response_message", AsyncMock()
        ) as mock_save,
        patch("core.chat_history_cache.save_chat_message", AsyncMock()),
    ):
        # Call send_message with interface_path
        await webui.send_message(
            {"interface_path": f"synth_webui/{session_id}", "text": text}
        )

    # The save_response_message should have been called
    assert mock_save.called
    # Verify that in-memory history contains the message
    msgs = webui.message_history.get(session_id)
    assert msgs is not None
    found = any(m.get("text") == text for m in msgs)
    assert found


@pytest.mark.asyncio
async def test_execute_action_uses_original_message_interface_path_when_context_missing():
    webui = SynthWebUIInterface()
    payload = {
        "text": "Hello from WebUI",
    }
    action = {"type": "message_synth_webui", "payload": payload}
    original_message = type("M", (), {"interface_path": "synth_webui/webui_default"})()

    with patch.object(webui, "send_message", AsyncMock()) as mock_send:
        await webui.execute_action(
            action, context={}, bot=None, original_message=original_message
        )

    assert mock_send.called
    sent_payload = mock_send.call_args.args[0]
    assert sent_payload["interface_path"] == "synth_webui/webui_default"
    assert sent_payload["text"] == "Hello from WebUI"


@pytest.mark.asyncio
async def test_send_message_serializes_non_json_metadata_for_websocket():
    webui = SynthWebUIInterface()
    session_id = "ws-session"
    websocket = AsyncMock()
    webui.connections[session_id] = websocket

    with (
        patch.object(webui, "_webui_clear_pending_thinking", AsyncMock()),
        patch.object(webui, "_append_history", AsyncMock()),
        patch("core.chat_context_manager.save_response_message", AsyncMock()),
    ):
        await webui.send_message(
            {
                "interface_path": f"synth_webui/{session_id}",
                "text": "hello",
                "metadata": {"tts_url": SimpleNamespace(url="https://example/tts")},
            }
        )

    assert websocket.send_json.called
    sent_payload = websocket.send_json.call_args.args[0]
    assert sent_payload["tts_url"] == "namespace(url='https://example/tts')"


@pytest.mark.asyncio
async def test_send_message_prefers_payload_content_over_positional_object_text():
    webui = SynthWebUIInterface()
    session_id = "ws-content-session"
    websocket = AsyncMock()
    webui.connections[session_id] = websocket

    with (
        patch.object(webui, "_webui_clear_pending_thinking", AsyncMock()),
        patch.object(webui, "_append_history", AsyncMock()),
        patch("core.chat_context_manager.save_response_message", AsyncMock()),
    ):
        await webui.send_message(
            {
                "interface_path": f"synth_webui/{session_id}",
                "content": "expected content",
            },
            cast(Any, SimpleNamespace(text="wrong positional text")),
        )

    assert websocket.send_json.called
    sent_payload = websocket.send_json.call_args.args[0]
    assert sent_payload["text"] == "expected content"
