"""Pin what a prompt says about the clock and the household's whereabouts.

Two things were live on 2026-09-22. The prompt's time field carried the dual
local+UTC rendering, so every prompt handed the model two clocks and a zone name
("22:52 CEST (20:52 UTC)", and "21:05 UTC (21:05 UTC)" until an environment
plugin published the house timezone). And the ``[House]`` block carried the
house's exact coordinates while the location helper could invent ``UTC`` as a
place name from a zone that names no place at all.

The rule these tests hold: a prompt states the local clock once, and a rough
place the household is content to share, never a second clock and never the
pinpoint.
"""

from __future__ import annotations

import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from core import time_zone_utils as tzu

# -- the clock the prompt carries -------------------------------------------


def test_time_plugin_injects_a_bare_local_clock() -> None:
    """``HH:MM`` local: no UTC twin, no zone name, nothing to quote back."""
    from plugins.time_plugin.time_plugin import TimePlugin

    plugin = object.__new__(TimePlugin)
    before = datetime.now(tzu.get_local_timezone()).strftime("%H:%M")
    injected = plugin.get_static_injection()
    after = datetime.now(tzu.get_local_timezone()).strftime("%H:%M")

    assert re.fullmatch(r"\d{2}:\d{2}", injected["time"]), injected["time"]
    assert injected["time"] in {before, after}, "must be the household's own clock"
    assert "UTC" not in injected["time"]
    assert "(" not in injected["time"], "no parenthesised second clock"


def test_the_anchor_renders_the_bare_clock_readably() -> None:
    """Bare ``HH:MM`` is also the form the Reality Anchor can render."""
    from core.prompt_engine import _pretty_anchor_time

    assert _pretty_anchor_time("22:52") == "10:52 PM"
    # The dual form was not a clock the anchor could parse, so it printed raw.
    assert _pretty_anchor_time("22:52 CEST (20:52 UTC)") == "22:52 CEST (20:52 UTC)"


# -- a zone is not a place --------------------------------------------------


def test_a_zone_that_names_no_place_is_not_a_location(monkeypatch: Any) -> None:
    for zone in ("UTC", "utc", "GMT", "Etc/UTC", "Etc/GMT+2", ""):
        monkeypatch.setattr(tzu, "_TZ", zone)
        monkeypatch.setattr(tzu, "_PROMPT_LOCATION", "")
        assert tzu.get_local_location() == "", f"'{zone}' must not read as a place"


def test_a_zone_that_names_a_city_still_reads_as_a_place(monkeypatch: Any) -> None:
    monkeypatch.setattr(tzu, "_TZ", "Europe/Ljubljana")
    monkeypatch.setattr(tzu, "_PROMPT_LOCATION", "")
    assert tzu.get_local_location() == "Ljubljana"


def test_the_configured_location_wins_over_the_zone(monkeypatch: Any) -> None:
    monkeypatch.setattr(tzu, "_TZ", "UTC")
    monkeypatch.setattr(tzu, "_PROMPT_LOCATION", "Dragonja valley,Slovenia")
    assert tzu.get_local_location() == "Dragonja valley,Slovenia"


# -- the [House] block: rough place instead of the pinpoint -----------------

HOUSE = {
    "location_name": "home",
    "latitude": 45.4723,
    "longitude": 13.6732,
    "country": "SI",
    "time_zone": "Europe/Ljubljana",
    "elevation": 20,
}


def _bare_plugin(core_config: dict[str, Any] | None = None) -> Any:
    """A plugin instance with no boot side effects, just what the block needs."""
    from plugins.home_assistant.home_assistant import HomeAssistantPlugin

    plugin = object.__new__(HomeAssistantPlugin)
    plugin._core_config = dict(core_config or {})
    plugin._client = SimpleNamespace(states={})
    plugin.is_enabled = lambda: True
    return plugin


def _with_label(monkeypatch: Any, label: str) -> None:
    from core.config_manager import config_registry

    real = config_registry.get_value
    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **kw: (
            label if key == "HASS_LOCATION_LABEL" else real(key, default, **kw)
        ),
    )


def test_the_house_block_names_the_rough_location(monkeypatch: Any) -> None:
    _with_label(monkeypatch, "the Dragonja valley")
    line = _bare_plugin(HOUSE)._render_location()

    assert "the Dragonja valley" in line
    assert "45.4723" not in line and "13.6732" not in line, "no pinpoint"
    assert "SI" in line, "the country still places the household"
    assert "timezone Europe/Ljubljana" in line, "the clock is still stated"


def test_without_a_label_the_coordinates_are_kept(monkeypatch: Any) -> None:
    _with_label(monkeypatch, "")
    assert "45.4723, 13.6732" in _bare_plugin(HOUSE)._render_location()


def test_a_house_with_no_coordinates_still_renders_its_place(monkeypatch: Any) -> None:
    _with_label(monkeypatch, "")
    line = _bare_plugin({"location_name": "home", "country": "SI"})._render_location()
    assert line.startswith("home")
    assert " at " not in line
