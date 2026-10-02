"""Pin the house-timezone override.

The household's clock is a property of the house, not of this process: an
environment plugin that knows the house's own timezone (Home Assistant reads it
from its core config) publishes it here, and it outranks the ``TZ`` config var
while it is set. These tests pin the precedence, the refusal of an unknown zone,
the dual-time rendering, the derived time fields, and that a change notifies the
``TZ`` listeners (the scheduled-event recompute) exactly like a TZ edit does.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from core import time_zone_utils as tzu


@pytest.fixture(autouse=True)
def _clean_house_timezone():
    tzu.set_house_timezone("")
    yield
    tzu.set_house_timezone("")


def test_house_timezone_outranks_the_tz_config():
    tzu.set_house_timezone("Europe/Ljubljana")
    assert tzu.get_house_timezone() == "Europe/Ljubljana"
    assert str(tzu.get_local_timezone()) == "Europe/Ljubljana"


def test_clearing_gives_the_clock_back_to_the_tz_config():
    tzu.set_house_timezone("Europe/Ljubljana")
    tzu.set_house_timezone("")
    assert tzu.get_house_timezone() == ""
    assert str(tzu.get_local_timezone()) == str(tzu._TZ)


def test_unknown_house_timezone_is_refused_and_keeps_the_previous():
    tzu.set_house_timezone("Europe/Ljubljana")
    tzu.set_house_timezone("Mars/Olympus_Mons")
    assert tzu.get_house_timezone() == "Europe/Ljubljana"
    assert str(tzu.get_local_timezone()) == "Europe/Ljubljana"


def test_a_failed_publish_from_empty_leaves_the_config_in_charge():
    tzu.set_house_timezone("Not/AZone")
    assert tzu.get_house_timezone() == ""
    assert str(tzu.get_local_timezone()) == str(tzu._TZ)


def test_dual_time_reads_in_the_house_timezone():
    tzu.set_house_timezone("Europe/Ljubljana")
    dt = datetime(2026, 9, 22, 19, 59, tzinfo=timezone.utc)
    assert tzu.format_dual_time(dt) == "21:59 CEST (19:59 UTC)"


def test_time_fields_follow_the_house_timezone():
    tzu.set_house_timezone("Europe/Ljubljana")
    fields = asyncio.run(
        tzu.get_local_time_fields(datetime(2026, 9, 22, 22, 30, tzinfo=timezone.utc))
    )
    assert fields["local_time"] == "00:30"
    assert fields["local_date"] == "2026-09-23"
    assert fields["day_of_week"] == "Wednesday"


def test_changing_the_house_timezone_notifies_the_tz_listeners(monkeypatch):
    from core.config_manager import config_registry

    seen = []
    monkeypatch.setattr(
        config_registry, "notify_listeners", lambda key: seen.append(key) or 0
    )
    tzu.set_house_timezone("Europe/Ljubljana")
    assert seen == ["TZ"]
    # Re-publishing the same zone is not a change and must not re-notify.
    tzu.set_house_timezone("Europe/Ljubljana")
    assert seen == ["TZ"]


def test_notify_listeners_returns_zero_for_unknown_keys():
    from core.config_manager import config_registry

    assert config_registry.notify_listeners("NO_SUCH_CONFIG_KEY") == 0


# -- the plugin side: what HA's own config publishes -------------------------


def _bare_plugin(core_config=None, enabled=True):
    """A plugin instance with no boot side effects, just the config it needs."""
    from plugins.home_assistant.home_assistant import HomeAssistantPlugin

    plugin = object.__new__(HomeAssistantPlugin)
    plugin._core_config = dict(core_config or {})
    plugin.is_enabled = lambda: enabled
    return plugin


def test_plugin_publishes_the_timezone_from_ha_core_config():
    plugin = _bare_plugin({"time_zone": "Europe/Budapest"})
    plugin.publish_house_timezone()
    assert tzu.get_house_timezone() == "Europe/Budapest"


def test_plugin_clear_hands_the_clock_back_to_the_config():
    plugin = _bare_plugin({"time_zone": "Europe/Budapest"})
    plugin.publish_house_timezone()
    plugin.clear_house_timezone()
    assert tzu.get_house_timezone() == ""
    assert str(tzu.get_local_timezone()) == str(tzu._TZ)


def test_plugin_switch_off_clears_and_on_rereads_the_house():
    plugin = _bare_plugin({"time_zone": "Europe/Budapest"})
    plugin.publish_house_timezone()
    plugin.is_enabled = lambda: False
    plugin._on_timezone_switch(False)
    assert tzu.get_house_timezone() == ""
    # Turning it back on re-reads HA rather than trusting the cached copy, so a
    # timezone changed in HA is picked up by flipping the switch.
    refreshed = []
    plugin.is_enabled = lambda: True
    plugin._schedule_core_config_refresh = lambda force=False: refreshed.append(force)
    plugin._on_timezone_switch(True)
    assert refreshed == [True]


def test_stale_house_facts_are_refreshed_in_the_background():
    import asyncio
    import time

    plugin = _bare_plugin({"time_zone": "Europe/Budapest"})
    plugin._core_config_at = time.monotonic()
    plugin._core_config_task = None
    calls = []

    async def _fake_fetch():
        calls.append(1)

    plugin._fetch_core_config = _fake_fetch
    plugin._schedule_core_config_refresh()  # fresh copy: nothing to do
    assert calls == []

    plugin._core_config_at = time.monotonic() - 100000  # long stale
    asyncio.run(_run_once(plugin))
    assert calls == [1]


async def _run_once(plugin):
    plugin._schedule_core_config_refresh()
    task = plugin._core_config_task
    if task is not None:
        await task


def test_plugin_without_a_known_timezone_publishes_nothing():
    plugin = _bare_plugin({})
    plugin.publish_house_timezone()
    assert tzu.get_house_timezone() == ""
    assert str(tzu.get_local_timezone()) == str(tzu._TZ)
