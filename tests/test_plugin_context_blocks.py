"""Pin the plugin-supplied prompt blocks and the silent-drop detector.

A plugin's ``get_static_injection()`` output lands in ``context_section`` and is
only visible to the model if a renderer consumes it, so this file pins: the
blocks render on the chat and beat routes, a block supersedes the built-in
provider whose key it replaces, and an injected key nobody renders is named
instead of dropped silently.
"""

from pathlib import Path

from core.prompt_engine import (
    _PLUGIN_CONTEXT_BLOCKS,
    _apply_plugin_block_supersedes,
    _build_context_summary,
    _unrendered_injection_keys,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROMPT_ENGINE_SOURCE = (_REPO_ROOT / "core" / "prompt_engine.py").read_text(
    encoding="utf-8"
)


def test_home_block_renders_on_chat_and_beat_routes():
    house = "living room 21.4 C, 3 lights on, nobody home"
    for is_grillo_internal in (False, True):
        summary = _build_context_summary(
            {"home": house, "date": "2026-09-21"},
            is_grillo_internal=is_grillo_internal,
        )
        assert "[Home]" in summary, f"missing heading (grillo={is_grillo_internal})"
        assert house in summary, f"missing house text (grillo={is_grillo_internal})"


def test_weather_and_location_blocks_render():
    summary = _build_context_summary(
        {"home_weather": "cloudy, 25.5°C", "home_location": "Home at 45.4723, 13.6732"}
    )
    assert "[Weather]\ncloudy, 25.5°C" in summary
    assert "[House]\nHome at 45.4723, 13.6732" in summary


def test_blank_or_missing_blocks_add_nothing():
    assert "[Home]" not in _build_context_summary({"home": "   "})
    assert "[Home]" not in _build_context_summary({"home": None})
    assert "[Weather]" not in _build_context_summary({"home_weather": ""})
    assert "[Home]" not in _build_context_summary({})


def test_plugin_block_supersedes_the_legacy_provider_key():
    section = {
        "home_weather": "cloudy, 25.5°C",
        "home_location": "Home at 45.4723, 13.6732",
        "weather": "wttr.in text for the wrong town",
        "location": "Ljubljana",
    }
    dropped = _apply_plugin_block_supersedes(section, section.keys())
    assert sorted(dropped) == ["location", "weather"]
    assert "weather" not in section and "location" not in section
    assert section["home_weather"] and section["home_location"]


def test_without_the_plugin_blocks_the_legacy_keys_are_untouched():
    section = {"weather": "wttr.in text", "location": "Ljubljana", "home": "kitchen on"}
    assert _apply_plugin_block_supersedes(section, section.keys()) == []
    assert section == {
        "weather": "wttr.in text",
        "location": "Ljubljana",
        "home": "kitchen on",
    }
    # A blank block must not supersede anything either.
    blank = {"home_weather": "   ", "weather": "wttr.in text"}
    assert _apply_plugin_block_supersedes(blank, blank.keys()) == []
    assert blank["weather"] == "wttr.in text"


def test_every_declared_plugin_block_is_considered_rendered():
    # The detector must never flag a key the renderer knows about.
    keys = [key for key, _heading, _legacy in _PLUGIN_CONTEXT_BLOCKS]
    assert _unrendered_injection_keys(keys) == []


def test_drop_detector_names_keys_with_no_renderer():
    unrendered = _unrendered_injection_keys(
        {"home", "persona", "weather", "upcoming_events", "facial_expression_guidance"}
    )
    assert unrendered == ["facial_expression_guidance", "upcoming_events"]
    assert _unrendered_injection_keys({"home", "memories", "location"}) == []
    assert _unrendered_injection_keys(None) == []


def test_both_renderers_use_the_same_block_table():
    # Chat/beat summary plus the live route: a key rendered by only one of them
    # is the exact shape of the bug this file exists to prevent.
    assert (
        _PROMPT_ENGINE_SOURCE.count(
            "for _plugin_key, _plugin_heading, _plugin_legacy in _PLUGIN_CONTEXT_BLOCKS"
        )
        >= 2
    )
