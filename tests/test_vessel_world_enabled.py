"""A world is offered to ``vessel_connect`` according to the key its card toggles."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from plugins.rift_vessel.vessel_plugin import VesselPlugin


def _mock_config(monkeypatch: pytest.MonkeyPatch, values: Dict[str, Any]) -> None:
    monkeypatch.setattr(
        "core.config_manager.config_registry.get_value",
        lambda key, default=None, **kwargs: values.get(key, default),
    )


def test_world_enabled_follows_the_card_toggle_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft": True})
    assert VesselPlugin._world_enabled("minecraft") is True
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft": False})
    assert VesselPlugin._world_enabled("minecraft") is False


def test_stale_alias_key_does_not_hide_an_enabled_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression: ``PLUGIN_ENABLED__minecraft_vessel`` was left at ``false`` in the
    # database while the card (``minecraft``) showed enabled, so ``vessel_connect``
    # offered only ``retroxr``. The alias key is not the toggle and is ignored.
    _mock_config(
        monkeypatch,
        {"PLUGIN_ENABLED__minecraft": True, "PLUGIN_ENABLED__minecraft_vessel": False},
    )
    assert VesselPlugin._world_enabled("minecraft") is True


def test_world_defaults_to_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_config(monkeypatch, {})
    assert VesselPlugin._world_enabled("retroxr") is True
