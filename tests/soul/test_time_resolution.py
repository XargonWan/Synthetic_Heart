from datetime import date

from core.soul.time_resolution import AbsoluteTimeResolver


def test_resolve_simple_temporal_terms() -> None:
    resolver = AbsoluteTimeResolver(current_date=date(2026, 4, 17))
    text = "We met yesterday and plan to talk next week on Tuesday."

    resolved = resolver.resolve_text(text)

    assert "2026-04-16" in resolved
    assert "week of 2026-04-20" in resolved
    assert "on 2026-04-21" in resolved


def test_resolve_relative_day_counts() -> None:
    resolver = AbsoluteTimeResolver(current_date=date(2026, 4, 17))
    text = "She mentioned this 3 days ago and we will revisit in 2 days."

    resolved = resolver.resolve_text(text)

    assert "2026-04-14" in resolved
    assert "2026-04-19" in resolved
