"""The house-state block can drop one entity without narrowing the include list.

Live motivation: the default include list carries ``binary_sensor.*``, so the
iPhone kiosk screensaver sensor rendered as "iPhone Kiosk Screensaver off" in
every prompt. Removing the entity meant narrowing ``binary_sensor.*`` (losing
every other binary sensor), so exclusion is a separate, post-match filter.
"""

from __future__ import annotations

from types import SimpleNamespace

import plugins.home_assistant as ha

_STATES = {
    "binary_sensor.iphone_kiosk_screensaver": {
        "state": "off",
        "attributes": {"friendly_name": "iPhone Kiosk Screensaver"},
    },
    "binary_sensor.front_door": {
        "state": "on",
        "attributes": {"friendly_name": "Front Door"},
    },
    "person.raine": {
        "state": "home",
        "attributes": {"friendly_name": "raineadmin"},
    },
    "sensor.kitchen_temperature": {
        "state": "21.5",
        "attributes": {
            "friendly_name": "Kitchen Temperature",
            "unit_of_measurement": "°C",
        },
    },
}


def _plugin() -> ha.HomeAssistantPlugin:
    plugin = ha.HomeAssistantPlugin.__new__(ha.HomeAssistantPlugin)
    plugin._client = SimpleNamespace(states=dict(_STATES))
    plugin._watched = set()
    return plugin


def _patch_config(monkeypatch, **values) -> None:
    monkeypatch.setattr(
        ha, "_cfg_str", lambda key, default="": str(values.get(key, default))
    )
    monkeypatch.setattr(
        ha, "_cfg_int", lambda key, default=0: int(values.get(key, default))
    )


def test_an_excluded_entity_leaves_the_house_state_block(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        HASS_AWARENESS_ENTITIES="binary_sensor.*,person.*",
        HASS_AWARENESS_EXCLUDE="binary_sensor.iphone_kiosk_screensaver",
    )

    text = _plugin()._render_house_state()

    assert "iPhone Kiosk Screensaver" not in text
    # Everything else the broad pattern covered is still there.
    assert "Front Door on" in text
    assert "raineadmin home" in text


def test_without_an_exclusion_the_entity_is_still_rendered(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        HASS_AWARENESS_ENTITIES="binary_sensor.*,person.*",
        HASS_AWARENESS_EXCLUDE="",
    )

    text = _plugin()._render_house_state()

    assert "iPhone Kiosk Screensaver off" in text


def test_exclusions_accept_wildcards(monkeypatch) -> None:
    _patch_config(
        monkeypatch,
        HASS_AWARENESS_ENTITIES="binary_sensor.*,person.*,sensor.*",
        HASS_AWARENESS_EXCLUDE="*kiosk*,sensor.kitchen_*",
    )

    text = _plugin()._render_house_state()

    assert "iPhone Kiosk Screensaver" not in text
    assert "Kitchen Temperature" not in text
    assert "Front Door on" in text
