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
