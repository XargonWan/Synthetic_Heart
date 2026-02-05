import pytest
from unittest.mock import AsyncMock

from core.webui import SynthWebUIInterface
from unittest.mock import patch, AsyncMock


class DummyRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_restore_archive_replays_to_websocket_and_deletes_archive():
    webui = SynthWebUIInterface()
    session_id = 'test_session_restore'
    mock_ws = AsyncMock()
    mock_ws.send_json = AsyncMock()
    webui.connections[session_id] = mock_ws

    messages = [
        {"sender_name": "alice", "sender_id": "u1", "text": "Hello Alice"},
        {"sender_name": "self", "sender_id": "bot1", "text": "Hello back"},
    ]

    # Prepare a fake archive (mock DB access to avoid using real DB in tests)
    aid = 'archive-test-1'
    archive_payload = {
        'id': aid,
        'session_id': 'test-restore',
        'name': 'restore-test',
        'messages': messages,
        'metadata': {'camera': {'x': 1}},
    }

    # Prepare request to restore this archive
    payload = {"archive_id": aid, "session_id": session_id}
    req = DummyRequest(payload)

    # Patch load_archive and delete_archive to avoid DB access and patch save_chat_message
    from collections import deque
    cached_msgs = deque()
    cached_msgs.append({'sender_name': 'alice', 'sender_id': 'u1', 'text': 'Hello Alice', 'timestamp': '2025-01-01T00:00:00Z', 'interface_path': f'synth_webui/{session_id}'})
    cached_msgs.append({'sender_name': 'self', 'sender_id': 'bot1', 'text': 'Hello back', 'timestamp': '2025-01-01T00:00:01Z', 'interface_path': f'synth_webui/{session_id}'})
    with patch('core.chat_archives_db.load_archive', AsyncMock(return_value=archive_payload)) as mock_load, \
         patch('core.chat_archives_db.create_archive', AsyncMock(return_value={'id': 'arch-created'})) as mock_create, \
         patch('core.chat_archives_db.delete_archive', AsyncMock()) as mock_delete, \
         patch('core.chat_history_cache.save_chat_message', AsyncMock(return_value=True)) as mock_save, \
         patch('core.chat_history_cache.load_chat_history', AsyncMock(return_value=cached_msgs)), \
         patch('core.chat_history_cache.clear_chat_history', AsyncMock()), \
         patch('core.session_meta.set_session_meta', AsyncMock()):
        resp = await webui.restore_chat_archive(req)
    # JSONResponse -> body is bytes, need to parse
    assert resp.status_code == 200
    body = resp.body.decode('utf-8') if hasattr(resp, 'body') else None
    assert body is not None
    import json as _json
    o = _json.loads(body)
    assert o.get('saved_count') == 2
    assert o.get('deleted_archive_id') == aid
    # The API should not return the raw 'messages' payload to avoid duplication
    assert '"messages":' not in body

    # send_json should have been called twice, once for each message
    # send_json should have been called twice, once for each message
    assert mock_ws.send_json.call_count == 2

    # Check call args for sender mapping: first 'user', second 'synth'
    calls = mock_ws.send_json.call_args_list
    first = calls[0][0][0]
    second = calls[1][0][0]
    assert first['type'] == 'message'
    assert first['sender'] == 'user'
    assert first['text'] == 'Hello Alice'
    assert second['sender'] == 'synth'
    assert second['text'] == 'Hello back'

    # The archive's delete archive method should have been called
    assert mock_delete.called


@pytest.mark.asyncio
async def test_restore_archive_with_empty_messages_keeps_archive():
    webui = SynthWebUIInterface()
    session_id = 'test_session_restore_2'
    mock_ws = AsyncMock()
    mock_ws.send_json = AsyncMock()
    webui.connections[session_id] = mock_ws

    messages = [
        {"sender_name": "bob", "sender_id": "u2", "text": ""},
        {"sender_name": "self", "sender_id": "bot2", "text": ""},
    ]

    aid = 'archive-empty-test'
    archive_payload = {
        'id': aid,
        'session_id': 'test-restore-empty',
        'name': 'restore-empty-test',
        'messages': messages,
        'metadata': None,
    }

    payload = {"archive_id": aid, "session_id": session_id}
    req = DummyRequest(payload)

    # Patch DB calls and simulate save_chat_message returning False for empty messages
    async def fake_save(interface_path, message_text, sender_name=None, sender_id=None, timestamp=None):
        return False

    with patch('core.chat_archives_db.load_archive', AsyncMock(return_value=archive_payload)) as mock_load_empty, \
         patch('core.chat_archives_db.create_archive', AsyncMock(return_value={'id': 'arch-created-empty'})) as mock_create_empty, \
         patch('core.chat_archives_db.delete_archive', AsyncMock()) as mock_delete_empty, \
         patch('core.chat_history_cache.save_chat_message', AsyncMock(side_effect=fake_save)) as mock_save_empty, \
         patch('core.chat_history_cache.load_chat_history', AsyncMock(return_value=[])), \
         patch('core.chat_history_cache.clear_chat_history', AsyncMock()), \
            patch('core.session_meta.set_session_meta', AsyncMock()) as mock_set_meta_empty:
        resp = await webui.restore_chat_archive(req)
    assert resp.status_code == 200
    body = resp.body.decode('utf-8') if hasattr(resp, 'body') else None
    assert body is not None
    import json as _json
    o = _json.loads(body)
    assert o.get('saved_count') == 0
    # Should not return raw messages to avoid duplication
    assert '"messages":' not in body
    # The mocked delete should not have been called because saved_count == 0
    assert not mock_delete_empty.called

    # No messages should have been sent since they were empty and not saved
    # However _replay_history still sends messages from message_history if any; considered saved_count=0 so history should be empty and no send_json should occur
    assert mock_ws.send_json.call_count == 0

    # set_session_meta should have been called to clear the processing flag
    interface_path = f"synth_webui/{session_id}"
    mock_set_meta_empty.assert_called_with(interface_path, {'processing': False})

    # No real DB cleanup required; the mocked delete was not called for empty archive


    @pytest.mark.asyncio
    async def test_restore_archive_with_synth_alias_is_normalized_and_replayed_as_synth():
        webui = SynthWebUIInterface()
        session_id = 'test_session_restore_3'
        mock_ws = AsyncMock()
        mock_ws.send_json = AsyncMock()
        webui.connections[session_id] = mock_ws

        messages = [
            {"sender_name": "alice", "sender_id": "u3", "text": "Hello Alice again"},
            {"sender_name": "synth", "sender_id": "bot3", "text": "Hello back again"},
        ]

        aid = 'archive-synth-alias'
        archive_payload = {
            'id': aid,
            'session_id': 'test-restore-synth',
            'name': 'restore-synth-test',
            'messages': messages,
            'metadata': None,
        }

        payload = {"archive_id": aid, "session_id": session_id}
        req = DummyRequest(payload)

        # We'll capture the sender_name argument passed to save_chat_message
        async def fake_save(interface_path, message_text, sender_name=None, sender_id=None, timestamp=None):
            # Return True for saved message
            return True

        from collections import deque
        # After saving, load_chat_history should return messages normalized to 'self' for synth
        cached_msgs = deque()
        cached_msgs.append({'sender_name': 'alice', 'sender_id': 'u3', 'text': 'Hello Alice again', 'timestamp': '2025-01-01T00:00:02Z', 'interface_path': f'synth_webui/{session_id}'})
        cached_msgs.append({'sender_name': 'self', 'sender_id': 'bot3', 'text': 'Hello back again', 'timestamp': '2025-01-01T00:00:03Z', 'interface_path': f'synth_webui/{session_id}'})

        with patch('core.chat_archives_db.load_archive', AsyncMock(return_value=archive_payload)) as mock_load, \
             patch('core.chat_archives_db.create_archive', AsyncMock(return_value={'id': 'arch-created'})) as mock_create, \
             patch('core.chat_archives_db.delete_archive', AsyncMock()) as mock_delete, \
             patch('core.chat_history_cache.save_chat_message', AsyncMock(side_effect=fake_save)) as mock_save, \
             patch('core.chat_history_cache.load_chat_history', AsyncMock(return_value=cached_msgs)), \
             patch('core.chat_history_cache.clear_chat_history', AsyncMock()), \
             patch('core.session_meta.set_session_meta', AsyncMock()):
            resp = await webui.restore_chat_archive(req)

        assert resp.status_code == 200
        body = resp.body.decode('utf-8') if hasattr(resp, 'body') else None
        import json as _json
        o = _json.loads(body)
        assert o.get('saved_count') == 2

        # The saved messages must have used sender_name='self' for the synth alias
        mock_save.assert_called()
        # verify the calls included a 'sender_name' kwarg equal to 'self' for the second message
        saved_calls = mock_save.call_args_list
        # Extract the kwargs of the second call (index 1)
        _, kwargs2 = saved_calls[1]
        assert kwargs2.get('sender_name') == 'self'

        # The replay should have sent the second message as synth (replay mapping)
        assert mock_ws.send_json.call_count == 2
        calls = mock_ws.send_json.call_args_list
        second = calls[1][0][0]
        assert second['sender'] == 'synth'


@pytest.mark.asyncio
async def test_archive_clears_processing_meta():
    from core.webui import SynthWebUIInterface
    webui = SynthWebUIInterface()
    session_id = 'archive_meta_session'
    # Prepare dummy payload
    payload = {"session_id": session_id, "name": "test-archive-meta"}
    req = DummyRequest(payload)

    # Patch DB and cache calls
    with patch('core.chat_history_cache.load_chat_history', AsyncMock(return_value=[])), \
         patch('core.chat_archives_db.create_archive', AsyncMock(return_value={'id': 'arch-meta-id', 'path': '/tmp/arch'})) as mock_create, \
         patch('core.chat_history_cache.clear_chat_history', AsyncMock()), \
         patch('core.chat_context_manager.clear_chat_context', AsyncMock()), \
         patch('core.session_meta.set_session_meta', AsyncMock()) as mock_set_meta:
        resp = await webui.archive_chat(req)

    assert resp.status_code == 200
    # Since there are no messages, create_archive should NOT have been called
    assert not mock_create.called
    # set_session_meta should still be called with processing=False
    interface_path = f"synth_webui/{session_id}"
    mock_set_meta.assert_called_with(interface_path, {'processing': False})
    # Response should indicate zero saved messages
    body = resp.body.decode('utf-8') if hasattr(resp, 'body') else None
    assert body is not None
    import json as _json
    o = _json.loads(body)
    assert o.get('saved_count') == 0

