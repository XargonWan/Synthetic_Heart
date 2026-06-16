"""Tests for default Vox configuration values."""

from __future__ import annotations

from core.config_manager import config_registry


def test_active_vox_engine_default_is_kitten() -> None:
    import plugins.vox_plugin  # noqa: F401

    definition = config_registry._definitions["ACTIVE_VOX_ENGINE"]
    config_registry._load_definition_sync(definition)

    assert definition.default == "kitten"
    assert definition.value == "kitten"
