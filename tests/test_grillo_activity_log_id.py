"""The activity log id handed to a beat must be a row that really exists.

A beat logs its prompt when it is enqueued and writes its response back BY ID
when it finishes, so a stale or never-committed id makes the beat overwrite
another beat's row instead of logging its own. That is exactly what happened
live: the 06:23 beat updated row 8907 and the 07:23 beat updated row 8910, both
belonging to earlier beats, while those beats themselves left no row.
"""

import pytest

from plugins.grillo.grillo_impl import GrilloPlugin


class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeCursor:
    """Returns ``lastrowid`` on insert and ``verify_row`` on the verify SELECT."""

    def __init__(self, lastrowid, verify_row):
        self.lastrowid = lastrowid
        self._verify_row = verify_row
        self.statements = []

    async def execute(self, sql, params=None):
        self.statements.append((sql, params))
        return None

    async def fetchone(self):
        return self._verify_row


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return _AsyncCtx(self._cursor)

    async def commit(self):
        self.commits += 1


def _install(monkeypatch, cursor):
    conn = _FakeConn(cursor)

    def _get_conn_ctx():
        return _AsyncCtx(conn)

    monkeypatch.setattr("core.db.get_conn_ctx", _get_conn_ctx)
    monkeypatch.setattr("core.db._get_db_type", lambda: "postgres")
    return conn


@pytest.mark.asyncio
async def test_verified_id_is_returned(monkeypatch):
    cursor = _FakeCursor(lastrowid=9001, verify_row={"id": 9001})
    _install(monkeypatch, cursor)

    result = await GrilloPlugin.create_activity_log(
        beat_type="observer", prompt_text="hi", metadata={}
    )

    assert result == 9001
    assert any(
        "SELECT id FROM grillo_activity_log" in sql for sql, _ in cursor.statements
    )


@pytest.mark.asyncio
async def test_unpersisted_id_is_discarded_not_returned(monkeypatch):
    """The stale-id case: the insert reports an id no row backs."""
    cursor = _FakeCursor(lastrowid=8910, verify_row=None)
    _install(monkeypatch, cursor)

    result = await GrilloPlugin.create_activity_log(
        beat_type="observer", prompt_text="hi", metadata={}
    )

    # The guard also warns ("id 8910 ... was not persisted") on the app logger,
    # which writes to the process log rather than to the captured stream.
    assert result is None


@pytest.mark.asyncio
async def test_missing_id_returns_none_without_verifying(monkeypatch):
    cursor = _FakeCursor(lastrowid=None, verify_row=None)
    _install(monkeypatch, cursor)

    result = await GrilloPlugin.create_activity_log(
        beat_type="observer", prompt_text="hi", metadata={}
    )

    assert result is None
    assert not any(
        "SELECT id FROM grillo_activity_log" in sql for sql, _ in cursor.statements
    )


@pytest.mark.asyncio
async def test_connection_failure_returns_none(monkeypatch):
    def _boom():
        raise RuntimeError("pool is gone")

    monkeypatch.setattr("core.db.get_conn_ctx", _boom)

    result = await GrilloPlugin.create_activity_log(
        beat_type="observer", prompt_text="hi", metadata={}
    )

    assert result is None
