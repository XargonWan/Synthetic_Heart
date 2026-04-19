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
