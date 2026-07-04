import pytest
from unittest.mock import AsyncMock

from core.webui import SynthWebUIInterface
from unittest.mock import patch


@pytest.mark.asyncio
async def test_append_history_stores_synth_as_self():
    webui = SynthWebUIInterface()
    session_id = "test_session_map"
    webui.connections[session_id] = AsyncMock()

    # Append a synth message
    with patch(
        "core.chat_history_cache.save_chat_message", AsyncMock(return_value=True)
    ) as mock_save:
        await webui._append_history(session_id, "synth", "Hello from synth")
        # save_chat_message should have been called and the db sender name should be 'self'
        mock_save.assert_called_once()
        called_args, called_kwargs = mock_save.call_args
        # message_text is the positional second argument
        assert called_args[1] == "Hello from synth"
        assert called_kwargs.get("sender_name") == "self"


@pytest.mark.asyncio
async def test_append_history_stores_user_as_user():
    webui = SynthWebUIInterface()
    session_id = "test_session_map2"
    webui.connections[session_id] = AsyncMock()

    # Append a user message
    with patch(
        "core.chat_history_cache.save_chat_message", AsyncMock(return_value=True)
    ) as mock_save:
        await webui._append_history(session_id, "user", "Hello from user")
        mock_save.assert_called_once()
        called_args, called_kwargs = mock_save.call_args
        assert called_args[1] == "Hello from user"
        assert called_kwargs.get("sender_name") == "user"


@pytest.mark.asyncio
async def test_replay_history_maps_sender_name_to_synth():
    webui = SynthWebUIInterface()
    session_id = "test_replay_map"
    mock_ws = AsyncMock()
    mock_ws.send_json = AsyncMock()
    webui.connections[session_id] = mock_ws

    from collections import deque

    webui.message_history[session_id] = deque(
        [
            {"sender_name": "bob", "text": "hi"},
            {"sender_name": "self", "text": "bot reply"},
        ]
    )

    await webui._replay_history(session_id)
    assert mock_ws.send_json.call_count == 2
    calls = mock_ws.send_json.call_args_list
    first = calls[0][0][0]
    second = calls[1][0][0]
    assert first["sender"] == "user"
    assert second["sender"] == "synth"


@pytest.mark.asyncio
async def test_append_history_skip_history():
    webui = SynthWebUIInterface()
    session_id = "test_session_skip"
    webui.connections[session_id] = AsyncMock()

    # Append a synth message with skip_history=True
    with patch(
        "core.chat_history_cache.save_chat_message", AsyncMock(return_value=True)
    ) as mock_save:
        await webui._append_history(
            session_id, "synth", "Hello from synth", skip_history=True
        )
        # save_chat_message should NOT have been called
        mock_save.assert_not_called()
