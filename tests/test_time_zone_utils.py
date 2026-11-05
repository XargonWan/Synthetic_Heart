from datetime import datetime
from zoneinfo import ZoneInfo

from core.time_zone_utils import get_time_of_day_label


def test_get_time_of_day_label_with_int_hours():
    assert get_time_of_day_label(0) == "night"
    assert get_time_of_day_label(3) == "night"
    assert get_time_of_day_label(4) == "early_morning"
    assert get_time_of_day_label(5) == "early_morning"
    assert get_time_of_day_label(6) == "morning"
    assert get_time_of_day_label(11) == "morning"
    assert get_time_of_day_label(12) == "afternoon"
    assert get_time_of_day_label(17) == "afternoon"
    assert get_time_of_day_label(18) == "evening"
    assert get_time_of_day_label(21) == "evening"
    assert get_time_of_day_label(22) == "late_evening"
    assert get_time_of_day_label(23) == "late_evening"


def test_get_time_of_day_label_with_datetime():
    dt = datetime(2026, 2, 10, 4, 0, tzinfo=ZoneInfo("UTC"))
    assert get_time_of_day_label(dt) == "early_morning"
    dt2 = datetime(2026, 2, 10, 15, 30, tzinfo=ZoneInfo("UTC"))
    assert get_time_of_day_label(dt2) == "afternoon"


def test_get_current_season():
    from core.time_zone_utils import get_current_season

    assert get_current_season(datetime(2026, 3, 15)) == "Early Spring"
    assert get_current_season(datetime(2026, 5, 20)) == "Late Spring"
    assert get_current_season(datetime(2026, 7, 4)) == "Mid Summer"
    assert get_current_season(datetime(2026, 12, 25)) == "Early Winter"


def test_format_day_month_has_no_leading_zero_on_any_platform():
    """``Sep 29``, never ``Sep 09`` — and never the glibc-only ``%-d``.

    ``strftime("%b %-d")`` raises ``ValueError: Invalid format string`` on
    Windows. Both callers format event lines inside a guard that swallows the
    error, so on Windows every upcoming-event line and every calendar cell
    silently vanished (live 2026-09-28: the event plugin's prompt block never
    appeared on this host). Asserting the single-digit case here fails loudly on
    Windows if anyone reintroduces the extension.
    """
    from core.time_zone_utils import format_day_month

    assert format_day_month(datetime(2026, 9, 9)) == "Sep 9"
    assert format_day_month(datetime(2026, 9, 29)) == "Sep 29"
