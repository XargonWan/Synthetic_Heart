"""Tests for the G.R.I.L.L.O. Weekly Review beat.

Covers the pure scheduling math (`_seconds_until_next_run`) and the pure prompt
builder (`_build_review_prompt`) — no DB, LLM, or message-queue dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import plugins.grillo.grillo_weekly_review as wr
from plugins.grillo.grillo_weekly_review import GrilloWeeklyReviewPlugin


@pytest.fixture
def plugin() -> GrilloWeeklyReviewPlugin:
    """A fresh plugin instance.

    Construction registers exposed vars and calls register_plugin; both
    registries tolerate re-registration, so this is safe in tests.
    """
    return GrilloWeeklyReviewPlugin()


# ----------------------------------------------------------- scheduling math


class _FrozenClock:
    """Proxies the real ``datetime`` module attr but pins ``now()``."""

    def __init__(self, frozen: datetime) -> None:
        self._frozen = frozen

    def now(self, tz=None):  # noqa: ANN001, ANN201
        return self._frozen

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(datetime, name)


def _seconds(
    plugin: GrilloWeeklyReviewPlugin,
    monkeypatch: pytest.MonkeyPatch,
    day: str,
    hhmm: str,
    now: datetime,
) -> int:
    """Compute _seconds_until_next_run with a frozen `now`."""
    monkeypatch.setattr(wr, "datetime", _FrozenClock(now))
    return plugin._seconds_until_next_run(day, hhmm)


def test_seconds_until_next_run_same_day_later_today(
    plugin: GrilloWeeklyReviewPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sunday 2026-01-04 is a Sunday (weekday()==6). Now 01:00, target 02:00.
    now = datetime(2026, 1, 4, 1, 0, 0, tzinfo=timezone.utc)
    assert _seconds(plugin, monkeypatch, "Sunday", "02:00", now) == 1 * 3600


def test_seconds_until_next_run_same_day_already_passed(
    plugin: GrilloWeeklyReviewPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sunday 04:00, target Sunday 02:00 already passed -> next week.
    now = datetime(2026, 1, 4, 4, 0, 0, tzinfo=timezone.utc)
    assert (
        _seconds(plugin, monkeypatch, "Sunday", "02:00", now)
        == 7 * 24 * 3600 - 2 * 3600
    )


def test_seconds_until_next_run_days_ahead(
    plugin: GrilloWeeklyReviewPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-01-05 is a Monday (weekday()==0). Target Wednesday 02:00.
    now = datetime(2026, 1, 5, 2, 0, 0, tzinfo=timezone.utc)
    assert _seconds(plugin, monkeypatch, "Wednesday", "02:00", now) == 2 * 24 * 3600


def test_seconds_until_next_run_bad_time_defaults_to_0200(
    plugin: GrilloWeeklyReviewPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 1, 4, 1, 0, 0, tzinfo=timezone.utc)
    # Garbage time -> falls back to 02:00, so 1h from 01:00 on Sunday.
    assert _seconds(plugin, monkeypatch, "Sunday", "not-a-time", now) == 1 * 3600


def test_seconds_until_next_run_unknown_day_defaults_to_sunday(
    plugin: GrilloWeeklyReviewPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 1, 4, 1, 0, 0, tzinfo=timezone.utc)
    # Unknown day name -> Sunday (index 6). Now is Sunday 01:00 -> 1h.
    assert _seconds(plugin, monkeypatch, "Blursday", "02:00", now) == 1 * 3600


# ------------------------------------------------------------- prompt builder


def test_build_review_prompt_contains_goal_instructions() -> None:
    prompt = GrilloWeeklyReviewPlugin._build_review_prompt("", "")

    assert "goal_set" in prompt
    assert "goal_update" in prompt
    assert '"scope": "none"' in prompt
    assert '"game": "none"' in prompt
    assert '"world": "none"' in prompt
    assert '"status": "done"' in prompt


def test_build_review_prompt_injects_diary_and_goals() -> None:
    diary = "Sunday: wrote a poem about a horse."
    goals = "- [active] write a poem about a horse"

    prompt = GrilloWeeklyReviewPlugin._build_review_prompt(diary, goals)

    assert "wrote a poem about a horse." in prompt
    assert "write a poem about a horse" in prompt
    assert "goal_set" in prompt


def test_build_review_prompt_empty_material_placeholders() -> None:
    prompt = GrilloWeeklyReviewPlugin._build_review_prompt("", "")

    assert "(no diary entries this week)" in prompt
    assert "(no personal goals on record)" in prompt


def test_build_review_prompt_is_private_review() -> None:
    prompt = GrilloWeeklyReviewPlugin._build_review_prompt("", "")

    assert "do NOT speak to any user" in prompt
    assert "WEEKLY LIFE REVIEW" in prompt
