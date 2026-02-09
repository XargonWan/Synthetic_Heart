import os
import json
import tempfile
from pathlib import Path

from core import chat_archives
from core.chat_archives import create_archive, list_archives, load_archive, delete_archive


def test_create_list_load_delete_archive(tmp_path):
    # Create sample messages
    messages = [
        {"sender_name": "user", "sender_id": "abc123", "text": "Hello", "timestamp": "2025-01-01T00:00:00Z", "interface_path": "synth_webui/abc123"},
        {"sender_name": "self", "sender_id": "self", "text": "Hi there", "timestamp": "2025-01-01T00:00:01Z", "interface_path": "synth_webui/abc123"},
    ]
    # Force backups directory to be in tmp_path by setting CWD for this test
    # Ensure the module uses a temp backup dir for the test
    chat_archives.BACKUP_DIR = tmp_path / 'backups' / 'chat_archives'
    os.makedirs(chat_archives.BACKUP_DIR, exist_ok=True)
    # Monkey patch BACKUP_DIR in module via environment trick - not necessary if module uses fixed path
    # Create archive
    archive_info = create_archive('abc123', messages, name='test-archive')
    assert archive_info.get('id')
    assert 'path' in archive_info

    # List archives
    archives = list_archives()
    assert any(a['id'] == archive_info['id'] for a in archives)
    # The list entry should include the 'name' field and match the created name
    assert any(a['id'] == archive_info['id'] and a.get('name') == 'test-archive' for a in archives)

    # Load archive
    loaded = load_archive(archive_info['id'])
    assert loaded['session_id'] == 'abc123'
    assert len(loaded['messages']) == 2

    # Delete archive
    delete_archive(archive_info['id'])
    # Confirm deletion
    try:
        load_archive(archive_info['id'])
        raise AssertionError('Archive should have been deleted')
    except FileNotFoundError:
        pass
    # Create/rename flow
    archive_info2 = create_archive('abc123', messages, name='orig-name')
    assert archive_info2.get('id')
    from core.chat_archives import rename_archive
    meta = rename_archive(archive_info2['id'], 'new-name')
    assert meta.get('name') == 'new-name'
    # Clean up
    delete_archive(archive_info2['id'])
