# tests/test_chat_archives_db.py
import json
from datetime import datetime, timezone

import pytest

from core import chat_archives_db
from core.chat_archives_db import (
    create_archive,
    list_archives,
    load_archive,
    delete_archive,
)


class _FakeArchiveCursor:
    def __init__(self, state):
        self._state = state
        self._rows = []
        self._row = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, params=None):
        normalized = " ".join(query.split()).lower()
        if normalized.startswith("create table if not exists chat_archives"):
            return None

        if normalized.startswith("insert into chat_archives"):
            archive_id, session_id, name, messages, metadata = params
            self._state[archive_id] = {
                "id": archive_id,
                "session_id": session_id,
                "name": name,
                "messages": messages,
                "metadata": metadata,
                "created_at": datetime.now(timezone.utc),
            }
            return None

        if normalized.startswith("select id, session_id, name, created_at"):
            session_id = params[0] if params else None
            rows = []
            for record in self._state.values():
                if session_id and record["session_id"] != session_id:
                    continue
                rows.append(
                    (
                        record["id"],
                        record["session_id"],
                        record["name"],
                        record["created_at"],
                        len(json.loads(record["messages"])),
                    )
                )
            self._rows = rows
            return None

        if normalized.startswith(
            "select id, session_id, name, messages, metadata, created_at"
        ):
            archive_id = params[0]
            record = self._state.get(archive_id)
            self._row = None
            if record is not None:
                self._row = (
                    record["id"],
                    record["session_id"],
                    record["name"],
                    record["messages"],
                    record["metadata"],
                    record["created_at"],
                )
            return None

        if normalized.startswith("delete from chat_archives"):
            self._state.pop(params[0], None)
            return None

        raise AssertionError(f"Unexpected query: {query}")

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._row


class _FakeArchiveConn:
    def __init__(self, state):
        self._state = state

    def cursor(self, *args, **kwargs):
        return _FakeArchiveCursor(self._state)


class _FakeArchiveCtx:
    def __init__(self, state):
        self._state = state

    async def __aenter__(self):
        return _FakeArchiveConn(self._state)

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_db_archive_create_list_load_delete(monkeypatch):
    state = {}
    monkeypatch.setattr(
        chat_archives_db, "get_conn_ctx", lambda: _FakeArchiveCtx(state)
    )

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
