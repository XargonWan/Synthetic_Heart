"""Recent system events: restart / toggle log, offline estimate and rendering.

The database is never touched: ``core.db.get_conn_ctx`` is made to fail, which
exercises the in-memory fallback the module promises, and the offline estimate
reads are replaced with canned values.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Iterator

import pytest

from core import system_events as se
from core.prompt_engine import _build_context_summary


@pytest.fixture(autouse=True)
def no_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import core.db as db

    @asynccontextmanager
    async def broken_conn() -> AsyncIterator[None]:
        raise RuntimeError("database offline (test)")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "get_conn_ctx", broken_conn)
    monkeypatch.setattr(se, "_table_ready", False)
    monkeypatch.setattr(se, "_heartbeat_paused", False)
    se._mem_events.clear()
    yield
    se._mem_events.clear()


def _utc(minutes_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def test_events_fall_back_to_memory_and_come_back_oldest_first() -> None:
    async def scenario() -> list[dict[str, Any]]:
        await se.record_event("boot", detail="offline ~7 min")
        await se.record_component_toggle("blocklist", "plugin", False)
        return await se.recent_events()

    events = asyncio.run(scenario())
    assert [e["kind"] for e in events] == ["boot", "component_disabled"]
    assert events[1]["component"] == "blocklist"


def test_only_the_last_few_events_inside_the_window_are_kept() -> None:
    async def scenario() -> list[dict[str, Any]]:
        await se.record_event("boot")  # pushed outside the 6 h window below
        se._mem_events[0]["ts"] = _utc(600)
        for index in range(8):
            await se.record_component_toggle(f"p{index}", "plugin", True)
        return await se.recent_events(limit=5, window_minutes=360)

    events = asyncio.run(scenario())
    assert [e["component"] for e in events] == ["p3", "p4", "p5", "p6", "p7"]


def test_no_events_means_no_block() -> None:
    assert asyncio.run(se.get_system_events_block()) == ""


def test_block_lines_read_naturally() -> None:
    async def scenario() -> str:
        await se.record_event("boot", detail="offline ~7 min, unexpected stop")
        await se.record_component_toggle("blocklist", "plugin", False)
        await se.record_component_toggle("telegram", "interface", True)
        return await se.get_system_events_block()

    lines = asyncio.run(scenario()).splitlines()
    assert len(lines) == 3
    assert lines[0].endswith("synth restarted (offline ~7 min, unexpected stop)")
    assert lines[1].endswith("plugin blocklist disabled")
    assert lines[2].endswith("interface telegram enabled")
    assert all(line.startswith("- ") for line in lines)


def test_prompt_renders_the_block_under_its_heading() -> None:
    text = _build_context_summary(
        {
            "system_events": "- 14:02 synth restarted",
            "active_interfaces": "Connected: telegram, telegram_bot.",
        }
    )
    assert "[Recent system events]\n- 14:02 synth restarted" in text
    assert "[Interfaces]\nConnected: telegram, telegram_bot." in text


def test_prompt_without_the_keys_has_neither_heading() -> None:
    text = _build_context_summary({})
    assert "[Recent system events]" not in text
    assert "[Interfaces]" not in text


# -- offline estimate -------------------------------------------------------


def _estimate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    heartbeat_age: int | None,
    shutdown_age: int | None,
    action_ages: dict[str, int | None] | None = None,
) -> dict[str, Any] | None:
    """Run the estimate against canned *ages in seconds* (the DB computes them)."""

    async def fake_fetch(sql: str) -> Any:
        if "kind = 'heartbeat'" in sql:
            return heartbeat_age
        if "kind = 'shutdown'" in sql:
            return shutdown_age
        for table, age in (action_ages or {}).items():
            if table in sql:
                if age is None:
                    raise RuntimeError("no such table")
                return age
        return None

    async def ready() -> None:
        return None

    monkeypatch.setattr(se, "_fetch_one", fake_fetch)
    monkeypatch.setattr(se, "ensure_system_events_table", ready)
    monkeypatch.setattr(se, "_age_seconds_sql", lambda expr: expr)
    return asyncio.run(se.estimate_offline())


def test_a_crash_is_estimated_from_the_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    estimate = _estimate(monkeypatch, heartbeat_age=7 * 60, shutdown_age=500 * 60)
    assert estimate is not None
    assert estimate["basis"] == "heartbeat"
    assert estimate["clean"] is False
    assert 6 * 60 <= estimate["seconds"] <= 8 * 60
    assert "unexpected stop" in se.describe_offline(estimate)


def test_a_graceful_stop_is_reported_as_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    # The heartbeat is frozen before the shutdown event is written.
    estimate = _estimate(monkeypatch, heartbeat_age=31 * 60, shutdown_age=30 * 60)
    assert estimate is not None
    assert estimate["basis"] == "shutdown"
    assert estimate["clean"] is True
    text = se.describe_offline(estimate)
    assert text.startswith("offline ~30 min")
    assert "clean shutdown" in text


def test_without_a_heartbeat_the_last_action_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    estimate = _estimate(
        monkeypatch,
        heartbeat_age=None,
        shutdown_age=None,
        action_ages={"chat_history_cache": 3 * 3600, "turn_reason_trail": None},
    )
    assert estimate is not None
    assert estimate["basis"] == "last_action"
    text = se.describe_offline(estimate)
    assert text.startswith("offline ~3 h")
    assert "estimated" in text


def test_no_evidence_claims_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    estimate = _estimate(monkeypatch, heartbeat_age=None, shutdown_age=None)
    assert estimate is None
    assert se.describe_offline(estimate) == ""


def test_a_quick_restart_is_not_worth_a_mention() -> None:
    assert se.describe_offline({"seconds": 45, "basis": "heartbeat"}) == ""


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(7 * 60, "~7 min"), (5 * 3600, "~5 h"), (4 * 86400, "~4 days")],
)
def test_durations_scale(seconds: int, expected: str) -> None:
    text = se.describe_offline({"seconds": seconds, "basis": "last_action"})
    assert expected in text


def test_shutdown_freezes_the_heartbeat() -> None:
    async def scenario() -> None:
        await se.record_shutdown("restart requested")
        await se.touch_heartbeat()  # must be a no-op after the stop was recorded

    asyncio.run(scenario())
    assert se._heartbeat_paused is True
    assert [e["kind"] for e in se._mem_events] == ["shutdown"]


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgres", "EXTRACT(EPOCH FROM (NOW() - ts))"),
        ("mariadb", "TIMESTAMPDIFF(SECOND, ts, NOW())"),
    ],
)
def test_the_age_sql_follows_the_database_dialect(
    monkeypatch: pytest.MonkeyPatch, dialect: str, expected: str
) -> None:
    import core.db as db

    monkeypatch.setattr(db, "_get_db_type", lambda: dialect)
    assert se._age_seconds_sql("ts") == expected


# -- user-changed variables --------------------------------------------------


def test_a_user_change_of_a_variable_is_logged_with_old_and_new() -> None:
    async def scenario() -> str:
        await se.record_variable_change("TTS_VOICE", "anna", "luca")
        return await se.get_system_events_block()

    assert asyncio.run(scenario()).endswith(
        "user changed setting TTS_VOICE: anna -> luca"
    )


def test_a_sensitive_variable_never_leaks_its_value() -> None:
    async def scenario() -> str:
        await se.record_variable_change(
            "OPENAI_API_KEY", "sk-old-secret", "sk-new-secret", sensitive=True
        )
        return await se.get_system_events_block()

    block = asyncio.run(scenario())
    assert "OPENAI_API_KEY" in block
    assert "value hidden" in block
    assert "secret" not in block


def test_an_unchanged_value_logs_nothing() -> None:
    asyncio.run(se.record_variable_change("TTS_VOICE", "anna", "anna"))
    assert list(se._mem_events) == []


def test_a_burst_of_edits_to_one_variable_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(se, "_var_edits", {})

    async def scenario() -> str:
        await se.record_variable_change("VOLUME", 10, 20)
        await se.record_variable_change("VOLUME", 20, 35)
        await se.record_variable_change("VOLUME", 35, 50)
        return await se.get_system_events_block()

    lines = asyncio.run(scenario()).splitlines()
    assert len(lines) == 1
    assert lines[0].endswith("user changed setting VOLUME: 10 -> 50")


def test_edits_to_different_variables_stay_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(se, "_var_edits", {})

    async def scenario() -> list[str]:
        await se.record_variable_change("A", 1, 2)
        await se.record_variable_change("B", 3, 4)
        return (await se.get_system_events_block()).splitlines()

    assert len(asyncio.run(scenario())) == 2


def test_long_values_are_trimmed() -> None:
    async def scenario() -> str:
        await se.record_variable_change("PROMPT", "x" * 500, "y" * 500)
        return await se.get_system_events_block()

    line = asyncio.run(scenario())
    assert len(line) < 200
    assert "…" in line


def test_a_burst_that_ends_where_it_began_leaves_no_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(se, "_var_edits", {})

    async def scenario() -> str:
        await se.record_variable_change("VOLUME", 5, 6)
        await se.record_variable_change("VOLUME", 6, 5)
        return await se.get_system_events_block()

    assert asyncio.run(scenario()) == ""
