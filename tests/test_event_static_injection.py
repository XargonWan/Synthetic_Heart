from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plugins.event_plugin import EventPlugin


def _make_plugin() -> EventPlugin:
    """Build an EventPlugin without running __init__ (avoids plugin registration)."""
    return object.__new__(EventPlugin)


def _row(next_run: datetime, description: str) -> dict:
    """Build a scheduled_events row dict matching _fetch_upcoming_event_rows()."""
    return {
        "id": 1,
        "date": next_run.date(),
        "time": next_run.strftime("%H:%M"),
        "recurrence_type": "none",
        "next_run": next_run,
        "description": description,
        "created_at": datetime.now(timezone.utc),
        "created_by": "synth",
        "uid": f"synth-{description}",
        "rrule": None,
        "tzid": "UTC",
        "source": "synth",
    }


@pytest.mark.asyncio
async def test_get_static_injection_includes_upcoming_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin()

    now = datetime.now(timezone.utc)
    soon = (now + timedelta(days=1)).replace(microsecond=0)
    later = (now + timedelta(days=2)).replace(microsecond=0)

    rows = [
        _row(soon, "Palworld 1.0 release"),
        _row(later, "Birra con Mario"),
    ]

    async def fake_fetch() -> list[dict]:
        return rows

    monkeypatch.setattr(plugin, "_fetch_upcoming_event_rows", fake_fetch)
    monkeypatch.setattr(
        "core.time_zone_utils.get_local_timezone", lambda: timezone.utc
    )

    result = await plugin.get_static_injection()

    assert "upcoming_events" in result
    block = result["upcoming_events"]
    assert "Palworld 1.0 release" in block
    assert "Birra con Mario" in block
    # Chronological order: the day-1 event precedes the day-2 event.
    assert block.index("Palworld 1.0 release") < block.index("Birra con Mario")


@pytest.mark.asyncio
async def test_get_static_injection_empty_when_no_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin()

    async def fake_fetch() -> list[dict]:
        return []

    monkeypatch.setattr(plugin, "_fetch_upcoming_event_rows", fake_fetch)
    monkeypatch.setattr(
        "core.time_zone_utils.get_local_timezone", lambda: timezone.utc
    )

    result = await plugin.get_static_injection()

    assert result == {}


@pytest.mark.asyncio
async def test_get_static_injection_respects_max_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _make_plugin()

    now = datetime.now(timezone.utc)
    rows = [
        _row((now + timedelta(hours=6 * (i + 1))).replace(microsecond=0), f"Event {i}")
        for i in range(10)
    ]

    async def fake_fetch() -> list[dict]:
        return rows

    monkeypatch.setattr(plugin, "_fetch_upcoming_event_rows", fake_fetch)
    monkeypatch.setattr(
        "core.time_zone_utils.get_local_timezone", lambda: timezone.utc
    )

    result = await plugin.get_static_injection()

    # Default max is 5; only the first 5 chronological events appear.
    block = result["upcoming_events"]
    assert block.count("- ") >= 5
    assert "Event 0" in block
    assert "Event 9" not in block
