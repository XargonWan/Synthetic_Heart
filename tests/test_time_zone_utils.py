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
