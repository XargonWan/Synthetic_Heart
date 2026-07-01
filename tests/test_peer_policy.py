from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import peer_policy


def test_get_peer_ids_and_names_from_synth_peers(monkeypatch):
    rows = [
        {"id": 8243553794, "name": "Aria"},
        {"id": 1122334455, "name": "Sol"},
    ]
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: rows if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({8243553794, 1122334455})
    assert peer_policy.get_peer_names() == {8243553794: "Aria", 1122334455: "Sol"}


def test_get_peer_ids_accepts_json_string(monkeypatch):
    raw = '[{"id": 42, "name": "Nova"}]'
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: raw if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({42})
    assert peer_policy.get_peer_names() == {42: "Nova"}


def test_malformed_synth_peers_fails_open_to_empty(monkeypatch):
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: "not valid json" if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset()
    assert peer_policy.get_peer_names() == {}


def test_row_without_name_still_yields_id_but_no_name(monkeypatch):
    rows = [{"id": 7}]
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: rows if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({7})
    assert peer_policy.get_peer_names() == {}


def test_row_with_non_integer_id_is_skipped(monkeypatch):
    rows = [{"id": "not-a-number", "name": "Broken"}, {"id": 5, "name": "Good"}]
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: rows if key == "SYNTH_PEERS" else default,
    )

    assert peer_policy.get_peer_ids() == frozenset({5})
    assert peer_policy.get_peer_names() == {5: "Good"}


def _enable_peer_mode(monkeypatch, names):
    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: True)
    monkeypatch.setattr("core.peer_policy.get_peer_names", lambda: names)


def test_get_relay_wait_peer_returns_preceding_peer(monkeypatch):
    """'2B, ... 2D, ...' -- 2D's own instance should wait on 2B (id 111)."""
    _enable_peer_mode(monkeypatch, {111: "2B"})
    monkeypatch.setattr("core.mention_utils.get_current_aliases", lambda: ["2D", "Dee"])

    peer_id = peer_policy.get_relay_wait_peer("Hey 2b, and Dee, introduce yourselves")
    assert peer_id == 111


def test_get_relay_wait_peer_none_when_own_name_first(monkeypatch):
    """'2D, ... 2B, ...' -- 2D is mentioned first, so 2D shouldn't wait on anyone."""
    _enable_peer_mode(monkeypatch, {111: "2B"})
    monkeypatch.setattr("core.mention_utils.get_current_aliases", lambda: ["2D", "Dee"])

    peer_id = peer_policy.get_relay_wait_peer("Dee, say hi to 2b")
    assert peer_id is None


def test_get_relay_wait_peer_none_when_this_bot_not_mentioned(monkeypatch):
    _enable_peer_mode(monkeypatch, {111: "2B"})
    monkeypatch.setattr("core.mention_utils.get_current_aliases", lambda: ["2D", "Dee"])

    peer_id = peer_policy.get_relay_wait_peer("Hey 2b, how are you?")
    assert peer_id is None


def test_get_relay_wait_peer_none_when_peer_mode_disabled(monkeypatch):
    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: False)

    peer_id = peer_policy.get_relay_wait_peer("Hey 2b, and Dee, introduce yourselves")
    assert peer_id is None


class _DummyCursor:
    def __init__(self, responses):
        self._responses = responses
        self.executed_sql: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, q, params=None):
        self.executed_sql.append(q)

    async def fetchone(self):
        found = self._responses.pop(0) if self._responses else False
        return (1 if found else 0,)


class _DummyConn:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    def cursor(self):
        return self._cursor


@pytest.mark.asyncio
async def test_peer_already_responded_queries_timestamp_column(monkeypatch):
    """Regression guard: the query must use the real `timestamp` column, not
    the nonexistent `timestamptz` (which silently fail-opened to False)."""
    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: True)
    cursor = _DummyCursor(responses=[True])
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _DummyConn(cursor))

    result = await peer_policy.peer_already_responded(
        "telegram_bot/-123",
        since=datetime.now(timezone.utc),
        peer_ids=frozenset({111}),
    )

    assert result is True
    assert any("timestamp >" in sql for sql in cursor.executed_sql)
    assert not any("timestamptz" in sql for sql in cursor.executed_sql)


@pytest.mark.asyncio
async def test_wait_for_peer_reply_returns_true_once_peer_responds(monkeypatch):
    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: True)
    cursor = _DummyCursor(responses=[False, False, True])
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _DummyConn(cursor))

    result = await peer_policy.wait_for_peer_reply(
        "telegram_bot/-123",
        peer_id=111,
        since=datetime.now(timezone.utc),
        timeout_seconds=10,
        poll_interval=0,
    )

    assert result is True


@pytest.mark.asyncio
async def test_wait_for_peer_reply_fails_open_on_timeout(monkeypatch):
    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: True)
    cursor = _DummyCursor(responses=[])  # peer never responds
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _DummyConn(cursor))

    result = await peer_policy.wait_for_peer_reply(
        "telegram_bot/-123",
        peer_id=111,
        since=datetime.now(timezone.utc),
        timeout_seconds=0.05,
        poll_interval=0.02,
    )

    assert result is False


class _TimestampCursor:
    """Cursor stub for MAX(timestamp)-style queries (single-row, single-column)."""

    def __init__(self, value):
        self._value = value
        self.executed_sql: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def execute(self, q, params=None):
        self.executed_sql.append(q)

    async def fetchone(self):
        return (self._value,)


class _DummyChat:
    def __init__(self, id=-123):
        self.id = id


class _DummyPeerMessage:
    def __init__(self, text, chat_id=-123, reply_to_message=None):
        self.text = text
        self.chat = _DummyChat(chat_id)
        self.reply_to_message = reply_to_message


def _enable_mention_only(monkeypatch, cooldown=20.0):
    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: True)
    monkeypatch.setattr("core.peer_policy.get_peer_policy", lambda: "mention_only")
    monkeypatch.setattr(
        "core.peer_policy._read_config",
        lambda key, default: (
            cooldown if key == "SYNTH_PEER_MENTION_COOLDOWN_SECONDS" else default
        ),
    )
    monkeypatch.setattr("core.mention_utils.is_synth_mentioned", lambda text: True)


@pytest.mark.asyncio
async def test_should_respond_to_peer_suppresses_within_cooldown(monkeypatch):
    """A peer message that mentions our alias must be suppressed if we just
    replied in this chat -- this is the fix for the back-to-back double
    response reported when a human message and a peer message both mention
    this bot's alias within seconds of each other."""
    _enable_mention_only(monkeypatch)
    cursor = _TimestampCursor(datetime.now(timezone.utc))
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _DummyConn(cursor))

    msg = _DummyPeerMessage("Hey Dee, how's it going?")
    result = await peer_policy.should_respond_to_peer(msg, "my_bot", 999)

    assert result is False


@pytest.mark.asyncio
async def test_should_respond_to_peer_allows_after_cooldown_expires(monkeypatch):
    _enable_mention_only(monkeypatch)
    stale = datetime.now(timezone.utc) - timedelta(seconds=60)
    cursor = _TimestampCursor(stale)
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _DummyConn(cursor))

    msg = _DummyPeerMessage("Hey Dee, how's it going?")
    result = await peer_policy.should_respond_to_peer(msg, "my_bot", 999)

    assert result is True


@pytest.mark.asyncio
async def test_should_respond_to_peer_allows_when_never_replied(monkeypatch):
    _enable_mention_only(monkeypatch)
    cursor = _TimestampCursor(None)
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _DummyConn(cursor))

    msg = _DummyPeerMessage("Hey Dee, how's it going?")
    result = await peer_policy.should_respond_to_peer(msg, "my_bot", 999)

    assert result is True


@pytest.mark.asyncio
async def test_should_respond_to_peer_cooldown_disabled_skips_db_check(monkeypatch):
    _enable_mention_only(monkeypatch, cooldown=0)

    def _boom():
        raise AssertionError("DB should not be queried when cooldown is disabled")

    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _boom())

    msg = _DummyPeerMessage("Hey Dee, how's it going?")
    result = await peer_policy.should_respond_to_peer(msg, "my_bot", 999)

    assert result is True


@pytest.mark.asyncio
async def test_should_respond_to_peer_reply_chain_break_precedes_cooldown_check(
    monkeypatch,
):
    """Reply-to-us suppression must short-circuit before the cooldown DB
    lookup even runs."""
    _enable_mention_only(monkeypatch)

    def _boom():
        raise AssertionError("DB should not be queried for a reply-chain break")

    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _boom())

    reply_to = SimpleNamespace(from_user=SimpleNamespace(id=999))
    msg = _DummyPeerMessage("Hey Dee!", reply_to_message=reply_to)
    result = await peer_policy.should_respond_to_peer(msg, "my_bot", 999)

    assert result is False


@pytest.mark.asyncio
async def test_self_replied_recently_fails_open_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _boom())

    result = await peer_policy._self_replied_recently(-123, 20.0)

    assert result is False


@pytest.mark.asyncio
async def test_wait_for_peer_reply_wakes_immediately_on_notify(monkeypatch):
    """notify_message_arrived() must short-circuit the poll interval instead
    of forcing the wait to sit out the full interval every cycle."""
    import asyncio
    import time

    monkeypatch.setattr("core.peer_policy.is_peer_mode_enabled", lambda: True)
    cursor = _DummyCursor(responses=[False, True])
    monkeypatch.setattr("core.db.get_conn_ctx", lambda: _DummyConn(cursor))

    async def _notify_soon():
        await asyncio.sleep(0.05)
        peer_policy.notify_message_arrived("telegram_bot/-123")

    start = time.monotonic()
    asyncio.create_task(_notify_soon())
    result = await peer_policy.wait_for_peer_reply(
        "telegram_bot/-123",
        peer_id=111,
        since=datetime.now(timezone.utc),
        timeout_seconds=10,
        poll_interval=5,
    )
    elapsed = time.monotonic() - start

    assert result is True
    # Should wake on the notify (~0.05s), not sit out the 5s poll interval.
    assert elapsed < 1.0
