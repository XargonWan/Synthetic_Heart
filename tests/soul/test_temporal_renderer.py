"""Tests for core.soul.time_resolution.TemporalRenderer."""

from datetime import datetime, timedelta, timezone

import pytest

from core.soul.time_resolution import TemporalRenderer


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def test_past_same_day(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    past = now - timedelta(hours=3)
    assert renderer.render_relative(past) == "3 hours ago"


def test_tomorrow(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    future = now + timedelta(hours=25)
    assert renderer.render_relative(future) == "tomorrow"


def test_yesterday(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    past = now - timedelta(hours=25)
    assert renderer.render_relative(past) == "yesterday"


def test_in_three_days(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    future = now + timedelta(days=3)
    assert renderer.render_relative(future) == "in 3 days"


def test_in_seven_days(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    future = now + timedelta(days=7)
    assert renderer.render_relative(future) == "next week"


def test_three_days_past(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    past = now - timedelta(days=3)
    assert renderer.render_relative(past) == "3 days ago"


def test_far_future(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    future = now + timedelta(days=50)
    text = renderer.render_relative(future)
    assert "month" in text.lower()


def test_far_past(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    past = now - timedelta(days=30)
    text = renderer.render_relative(past)
    assert "ago" in text


def test_none_timestamp(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    assert renderer.render_relative(None) == "ongoing"


def test_naive_timestamp(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    naive = now.replace(tzinfo=None) - timedelta(hours=3)
    assert renderer.render_relative(naive) == "3 hours ago"


def test_same_moment(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    assert renderer.render_relative(now) == "in a few minutes"


def test_near_future(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    near_future = now + timedelta(seconds=30)
    assert renderer.render_relative(near_future) == "in a few minutes"


def test_near_past(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    near_past = now - timedelta(seconds=30)
    assert renderer.render_relative(near_past) == "just now"


def test_last_week(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    past = now - timedelta(days=10)
    assert renderer.render_relative(past) == "last week"


def test_next_week(now: datetime) -> None:
    renderer = TemporalRenderer(now=now)
    future = now + timedelta(days=10)
    assert renderer.render_relative(future) == "next week"
