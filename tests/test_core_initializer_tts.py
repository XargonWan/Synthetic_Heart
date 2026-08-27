#!/usr/bin/env python3
"""Tests for CoreInitializer behavior regarding plugin-enabled flag."""

import asyncio


def test_disabled_plugin_actions_not_registered():
    from core.core_initializer import core_initializer, PLUGIN_REGISTRY

    # Backup original registry
    original_registry = dict(PLUGIN_REGISTRY)

    class FakePlugin:
        display_name = "Fake TTS"

        def __init__(self):
            self.enabled = False

        def get_supported_actions(self):
            return {"fake_tts_speak": {"description": "fake tts"}}

    try:
        PLUGIN_REGISTRY["fake_tts"] = FakePlugin()

        # Build actions block
        asyncio.run(core_initializer._build_actions_block())

        available = core_initializer.actions_block.get("available_actions", {})
        assert "fake_tts_speak" not in available

    finally:
        # Restore registry
        PLUGIN_REGISTRY.clear()
        PLUGIN_REGISTRY.update(original_registry)
        # Rebuild actions block to previous state
        try:
            asyncio.run(core_initializer._build_actions_block())
        except Exception:
            pass


def test_is_enabled_false_plugin_actions_not_registered() -> None:
    from core.core_initializer import PLUGIN_REGISTRY, core_initializer

    original_registry = dict(PLUGIN_REGISTRY)

    class FakePlugin:
        display_name = "Fake Vision"

        def is_enabled(self) -> bool:
            return False

        def get_supported_actions(self):
            return {"fake_vision": {"description": "fake vision"}}

    try:
        PLUGIN_REGISTRY["fake_vision"] = FakePlugin()

        asyncio.run(core_initializer._build_actions_block())

        available = core_initializer.actions_block.get("available_actions", {})
        assert "fake_vision" not in available

    finally:
        PLUGIN_REGISTRY.clear()
        PLUGIN_REGISTRY.update(original_registry)
        try:
            asyncio.run(core_initializer._build_actions_block())
        except Exception:
            pass


def test_is_enabled_true_plugin_actions_registered() -> None:
    from core.core_initializer import PLUGIN_REGISTRY, core_initializer

    original_registry = dict(PLUGIN_REGISTRY)

    class FakePlugin:
        display_name = "Fake Vision"

        def is_enabled(self) -> bool:
            return True

        def get_supported_actions(self):
            return {"fake_vision": {"description": "fake vision"}}

    try:
        PLUGIN_REGISTRY["fake_vision"] = FakePlugin()

        asyncio.run(core_initializer._build_actions_block())

        available = core_initializer.actions_block.get("available_actions", {})
        assert "fake_vision" in available

    finally:
        PLUGIN_REGISTRY.clear()
        PLUGIN_REGISTRY.update(original_registry)
        try:
            asyncio.run(core_initializer._build_actions_block())
        except Exception:
            pass


def test_real_plugin_actions_follow_runtime_enablement(monkeypatch) -> None:
    from core.config_manager import config_registry
    from core.core_initializer import PLUGIN_REGISTRY, core_initializer
    from plugins.auris_plugin import AurisPlugin
    from plugins.iris_plugin import IrisPlugin
    from plugins.memory_search import MemorySearchPlugin
    from plugins.soul_plugin import SoulPlugin
    from plugins.vox_plugin import VoxPlugin

    original_registry = dict(PLUGIN_REGISTRY)
    overrides = {
        "ACTIVE_VOX_ENGINE": "disabled",
        "ACTIVE_AURIS_ENGINE": "disabled",
        "ACTIVE_IRIS_ENGINE": "disabled",
    }

    def fake_get_value(key, default=None, **kwargs):
        return overrides.get(key, default)

    real_plugins = {
        "vox_plugin": VoxPlugin.__new__(VoxPlugin),
        "auris_plugin": AurisPlugin.__new__(AurisPlugin),
        "iris_plugin": IrisPlugin.__new__(IrisPlugin),
        "soul_plugin": SoulPlugin.__new__(SoulPlugin),
        "memory_search": MemorySearchPlugin.__new__(MemorySearchPlugin),
    }

    with monkeypatch.context() as patch_ctx:
        patch_ctx.setattr(config_registry, "get_value", fake_get_value)
        PLUGIN_REGISTRY.clear()
        PLUGIN_REGISTRY.update(real_plugins)

        asyncio.run(core_initializer._build_actions_block())
        available = core_initializer.actions_block.get("available_actions", {})

        assert "tts_speak" not in available
        assert "stt_transcribe" not in available
        assert "vision_describe" not in available
        # soul_plugin and memory_search are gated only by the global plugin
        # toggle (PLUGIN_ENABLED__<name>), not by an internal config flag, so
        # their actions are always exposed once the plugin is loaded.
        assert "memory_search" in available

        overrides.update(
            {
                "ACTIVE_VOX_ENGINE": "kitten",
                "ACTIVE_AURIS_ENGINE": "vosk",
                "ACTIVE_IRIS_ENGINE": "selenium-llm-engine",
            }
        )

        asyncio.run(core_initializer._build_actions_block())
        available = core_initializer.actions_block.get("available_actions", {})

        assert "tts_speak" in available
        assert "stt_transcribe" in available
        assert "vision_describe" not in available
        assert "memory_search" in available
        assert "soul_force_compile" not in available

    PLUGIN_REGISTRY.clear()
    PLUGIN_REGISTRY.update(original_registry)
    try:
        asyncio.run(core_initializer._build_actions_block())
    except Exception:
        pass
