import pytest
from unittest.mock import AsyncMock, patch

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
        patch("core.webui.get_conn_ctx", AsyncMock()),
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
