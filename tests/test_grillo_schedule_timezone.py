"""GRILLO's clock-time settings are wall-clock times in the configured timezone.

They were computed as UTC while their labels said local, so "03:00" ran at 04:00 or
05:00 local time. The database and the scheduler arithmetic stay UTC; only the
moment the setting is read is local.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import plugins.grillo.grillo_compactor as grillo_compactor
import plugins.grillo.grillo_growth as grillo_growth
import plugins.grillo.grillo_weekly_review as grillo_weekly_review

TOKYO = ZoneInfo("Asia/Tokyo")


def _lands_on(seconds: int, tz: ZoneInfo) -> datetime:
    # The scheduler floors to whole seconds, so it wakes up to a second before the
    # target; one second of slack puts the wake-up inside the target minute.
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds + 1)).astimezone(tz)


def test_compactor_runs_at_the_configured_local_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grillo_compactor, "get_local_timezone", lambda: TOKYO)
    plugin = grillo_compactor.GrilloCompactorPlugin.__new__(
        grillo_compactor.GrilloCompactorPlugin
    )

    seconds = plugin._seconds_until_next_run("03:00")

    assert 0 < seconds <= 24 * 3600 + 5
    due = _lands_on(seconds, TOKYO)
    assert (due.hour, due.minute) == (3, 0)


def test_weekly_review_runs_on_the_local_weekday_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grillo_weekly_review, "get_local_timezone", lambda: TOKYO)

    seconds = grillo_weekly_review.GrilloWeeklyReviewPlugin._seconds_until_next_run(
        "friday", "21:30"
    )

    due = _lands_on(seconds, TOKYO)
    assert (due.weekday(), due.hour, due.minute) == (4, 21, 30)


def test_growth_runs_on_the_local_weekday_and_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(grillo_growth, "get_local_timezone", lambda: TOKYO)
    plugin = grillo_growth.GrilloGrowthPlugin.__new__(grillo_growth.GrilloGrowthPlugin)

    seconds = plugin._seconds_until_next_run("monday", "06:15")

    due = _lands_on(seconds, TOKYO)
    assert (due.weekday(), due.hour, due.minute) == (0, 6, 15)
