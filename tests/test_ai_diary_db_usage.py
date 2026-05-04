import asyncio
from datetime import datetime
import pytest

import plugins.ai_diary as ai_diary


class DummyCursor:
    def __init__(self):
        self.executed = []
        self.lastrowid = 123

    async def execute(self, q, params=None):
        self.executed.append((q, params))

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyConn:
    def __init__(self, cursor):
        self._cursor = cursor

    async def cursor(self):
        return self._cursor

    async def commit(self):
        return None


class DummyCtx:
    def __init__(self):
        self.conn = DummyConn(DummyCursor())
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        self.entered = False
        return False


@pytest.mark.asyncio
async def test_add_diary_entry_async_uses_get_db(monkeypatch):
    monkeypatch.setenv("SYNTH_TESTING", "1")
    # Ensure plugin is enabled otherwise it may attempt table creation
    ai_diary.PLUGIN_ENABLED = True

    dummy = DummyCtx()

    def fake_get_db():
        return dummy

    monkeypatch.setattr(ai_diary, "get_db", fake_get_db)

    await ai_diary.add_diary_entry_async(content="test entry", personal_thought="p")

    assert dummy.entered is False  # context should have exited
    assert isinstance(dummy.conn._cursor, DummyCursor)
    assert dummy.conn._cursor.executed, "Expected DB execute to have been called"


def test_run_falls_back_to_asyncio_run_when_no_loop(monkeypatch):
    monkeypatch.setattr(
        asyncio,
        "get_event_loop",
        lambda: (_ for _ in ()).throw(RuntimeError("no loop")),
    )
    monkeypatch.setattr(asyncio, "run", lambda coro: "ok")

    res = ai_diary._run("noop")
    assert res == "ok"


def test_clip_for_column_noop_when_under_limit():
    text = "hello"
    assert ai_diary._clip_for_column(text, 10) == "hello"


def test_clip_for_column_truncates_with_marker():
    text = "x" * 120
    clipped = ai_diary._clip_for_column(text, 64)
    assert clipped is not None
    assert len(clipped) <= 64
    assert clipped.endswith("...[truncated]")


@pytest.mark.asyncio
async def test_get_user_message_column_limit_fallback_when_schema_missing():
    class CursorNoSchema:
        async def execute(self, q, params=None):
            return None

        async def fetchone(self):
            return None

    limit = await ai_diary._get_user_message_column_limit(CursorNoSchema())
    assert limit == 255


def test_is_user_message_overflow_error_detects_target_column():
    err = Exception("(1406, \"Data too long for column 'user_message' at row 1\")")
    assert ai_diary._is_user_message_overflow_error(err) is True


def test_is_user_message_overflow_error_ignores_other_errors():
    err = Exception("(1406, \"Data too long for column 'content' at row 1\")")
    assert ai_diary._is_user_message_overflow_error(err) is False


@pytest.mark.asyncio
async def test_upsert_retries_insert_when_user_message_overflows(monkeypatch):
    class OverflowRetryCursor:
        def __init__(self):
            self.executed = []
            self.lastrowid = 321
            self._overflow_raised = False
            self._last_query = ""

        async def execute(self, q, params=None):
            self._last_query = q
            self.executed.append((q, params))
            if "INSERT INTO ai_diary" in q and not self._overflow_raised:
                self._overflow_raised = True
                raise Exception(
                    "(1406, \"Data too long for column 'user_message' at row 1\")"
                )

        async def fetchone(self):
            if "INFORMATION_SCHEMA.COLUMNS" in self._last_query:
                return ("varchar", 255)
            if "FROM ai_diary WHERE DATE(timestamp) = CURDATE()" in self._last_query:
                return None
            return None

    class OverflowRetryConn:
        def __init__(self, cursor):
            self._cursor = cursor
            self.commits = 0

        async def cursor(self):
            return self._cursor

        async def commit(self):
            self.commits += 1

    class OverflowRetryCtx:
        def __init__(self):
            self.cursor = OverflowRetryCursor()
            self.conn = OverflowRetryConn(self.cursor)

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    ctx = OverflowRetryCtx()
    monkeypatch.setattr(ai_diary, "get_db", lambda: ctx)

    entry_id = await ai_diary._upsert_diary_impl(
        content="content",
        personal_thought="thought",
        emotions=[],
        interaction_summary="summary",
        user_message="x" * 5000,
        context_tags=[],
        involved_users=[],
        interface="synth_webui",
        chat_id="chat",
        thread_id="thread",
    )

    assert entry_id == 321
    insert_calls = [q for q, _ in ctx.cursor.executed if "INSERT INTO ai_diary" in q]
    assert len(insert_calls) == 2
    assert ctx.conn.commits == 1


@pytest.mark.asyncio
async def test_upsert_refreshes_origin_fields_from_real_message_context(monkeypatch):
    class UpdateOriginCursor:
        def __init__(self):
            self.executed = []
            self._last_query = ""

        async def execute(self, q, params=None):
            self._last_query = q
            self.executed.append((q, params))

        async def fetchone(self):
            if "INFORMATION_SCHEMA.COLUMNS" in self._last_query:
                return ("text", None)
            if "FROM ai_diary WHERE DATE(timestamp) = CURDATE()" in self._last_query:
                return (
                    99,
                    "existing content",
                    "existing thought",
                    "existing summary",
                    "existing user",
                    "[]",
                    "[]",
                    "[]",
                    "grillo",
                    "-1",
                    None,
                )
            return None

    class UpdateOriginConn:
        def __init__(self, cursor):
            self._cursor = cursor
            self.commits = 0

        async def cursor(self):
            return self._cursor

        async def commit(self):
            self.commits += 1

    class UpdateOriginCtx:
        def __init__(self):
            self.cursor = UpdateOriginCursor()
            self.conn = UpdateOriginConn(self.cursor)

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    ctx = UpdateOriginCtx()
    monkeypatch.setattr(ai_diary, "get_db", lambda: ctx)

    entry_id = await ai_diary._upsert_diary_impl(
        content="content",
        personal_thought="thought",
        emotions=[],
        interaction_summary="summary",
        user_message="user",
        context_tags=[],
        involved_users=[],
        interface="telegram_bot",
        chat_id="123",
        thread_id="777",
    )

    assert entry_id == 99
    update_calls = [
        params for q, params in ctx.cursor.executed if "UPDATE ai_diary" in q
    ]
    assert len(update_calls) == 1
    assert update_calls[0][7] == "telegram_bot"
    assert update_calls[0][8] == "123"
    assert update_calls[0][9] == "777"
    assert ctx.conn.commits == 1


@pytest.mark.asyncio
async def test_upsert_does_not_replace_real_interface_with_diary_merge(monkeypatch):
    class UpdateOriginCursor:
        def __init__(self):
            self.executed = []
            self._last_query = ""

        async def execute(self, q, params=None):
            self._last_query = q
            self.executed.append((q, params))

        async def fetchone(self):
            if "INFORMATION_SCHEMA.COLUMNS" in self._last_query:
                return ("text", None)
            if "FROM ai_diary WHERE DATE(timestamp) = CURDATE()" in self._last_query:
                return (
                    99,
                    "existing content",
                    "existing thought",
                    "existing summary",
                    "existing user",
                    "[]",
                    "[]",
                    "[]",
                    "telegram_bot",
                    "123",
                    "777",
                )
            return None

    class UpdateOriginConn:
        def __init__(self, cursor):
            self._cursor = cursor
            self.commits = 0

        async def cursor(self):
            return self._cursor

        async def commit(self):
            self.commits += 1

    class UpdateOriginCtx:
        def __init__(self):
            self.cursor = UpdateOriginCursor()
            self.conn = UpdateOriginConn(self.cursor)

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, exc_type, exc, tb):
            return False

    ctx = UpdateOriginCtx()
    monkeypatch.setattr(ai_diary, "get_db", lambda: ctx)

    entry_id = await ai_diary._upsert_diary_impl(
        content="merged content",
        personal_thought="thought",
        emotions=[],
        interaction_summary="summary",
        user_message="user",
        context_tags=[],
        involved_users=[],
        interface="diary_merge",
        chat_id="123",
        thread_id="777",
    )

    assert entry_id == 99
    update_calls = [
        params for q, params in ctx.cursor.executed if "UPDATE ai_diary" in q
    ]
    assert len(update_calls) == 1
    assert update_calls[0][7] == "telegram_bot"
    assert update_calls[0][8] == "123"
    assert update_calls[0][9] == "777"


@pytest.mark.asyncio
async def test_on_debrief_uses_postgres_string_agg(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_fetchall(query, params=None):
        captured["query"] = query
        captured["params"] = params
        return []

    ai_diary.PLUGIN_ENABLED = True
    monkeypatch.setattr(ai_diary, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(ai_diary, "_fetchall", fake_fetchall)

    plugin = object.__new__(ai_diary.DiaryPlugin)
    await plugin.on_debrief([], [], [], {}, object())

    assert "string_agg" in str(captured["query"])
    assert "GROUP_CONCAT" not in str(captured["query"])


@pytest.mark.asyncio
async def test_on_debrief_enqueues_merge_source_ids(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_fetchall(query, params=None):
        return [
            {
                "id": 42,
                "combined": "one\n\n---\n\ntwo",
                "row_count": 2,
                "source_ids": "41,42",
                "first_timestamp": "2026-04-18T21:00:00+00:00",
            }
        ]

    async def fake_enqueue_low_priority(
        _priority,
        _message,
        context_memory=None,
        interface_id=None,
        original_message=None,
    ):
        captured["context_memory"] = context_memory
        captured["interface_id"] = interface_id

    ai_diary.PLUGIN_ENABLED = True
    monkeypatch.setattr(ai_diary, "_get_db_type", lambda: "postgres")
    monkeypatch.setattr(ai_diary, "_fetchall", fake_fetchall)
    monkeypatch.setattr(
        "core.message_queue.enqueue_low_priority",
        fake_enqueue_low_priority,
    )

    plugin = object.__new__(ai_diary.DiaryPlugin)
    await plugin.on_debrief([], [], [], {}, object())

    assert captured["interface_id"] == "diary_merge"
    assert captured["context_memory"]["diary_merge_source_ids"] == [41, 42]
    assert (
        captured["context_memory"]["diary_merge_timestamp"]
        == "2026-04-18T21:00:00+00:00"
    )


def test_update_diary_entry_archives_merged_source_rows(monkeypatch):
    executed: list[tuple[str, tuple]] = []
    archived: dict[str, object] = {}

    async def fake_execute(query, params=()):
        executed.append((query, params))
        return None

    monkeypatch.setattr(ai_diary, "_execute", fake_execute)
    monkeypatch.setattr(ai_diary, "_run", lambda coro: asyncio.run(coro))

    def fake_archive_diary_entries(entry_ids):
        archived["ids"] = list(entry_ids)
        return {"success": True, "archived_count": len(entry_ids)}

    monkeypatch.setattr(ai_diary, "archive_diary_entries", fake_archive_diary_entries)

    plugin = object.__new__(ai_diary.DiaryPlugin)
    result = plugin.execute_action(
        {
            "type": "update_diary_entry",
            "payload": {"id": 42, "content": "merged prose"},
        },
        {
            "diary_merge_source_ids": [41, 42, 41],
            "diary_merge_timestamp": "2026-04-18T21:00:00+00:00",
        },
        None,
        None,
    )

    assert result["success"] is True
    assert executed == [
        (
            "UPDATE ai_diary SET content=%s, timestamp=%s WHERE id=%s",
            ("merged prose", datetime.fromisoformat("2026-04-18T21:00:00+00:00"), 42),
        )
    ]
    assert archived["ids"] == [41]
