# tests/test_chat_archives_db.py
import pytest

from core.chat_archives_db import (
    create_archive,
    list_archives,
    load_archive,
    delete_archive,
)


@pytest.mark.asyncio
async def test_db_archive_create_list_load_delete():
    messages = [
        {
            "sender_name": "user",
            "sender_id": "abc123",
            "text": "Hello DB!",
            "timestamp": "2025-01-01T00:00:00Z",
            "interface_path": "synth_webui/test",
        },
    ]
    meta = {"camera": {"x": 0}}
    # Create
    arch = await create_archive("test-db", messages, name="db-test", metadata=meta)
    assert arch.get("id")
    aid = arch["id"]

    # List
    archives = await list_archives()
    assert any(a["id"] == aid for a in archives)

    # Load
    loaded = await load_archive(aid)
    assert loaded["id"] == aid
    assert len(loaded["messages"]) == 1
    assert loaded["messages"][0]["text"] == "Hello DB!"

    # Delete
    await delete_archive(aid)
    with pytest.raises(FileNotFoundError):
        await load_archive(aid)
