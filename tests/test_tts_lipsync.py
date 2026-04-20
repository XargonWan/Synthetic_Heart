#!/usr/bin/env python3
"""Tests for TTS Lip Sync plugin behavior."""

import asyncio


def test_execute_skipped_when_disabled():
    """If the plugin has no endpoints configured (disabled), execute_action should skip.

    Because the plugin now defers to Vox when an engine is active we also stub
    the active engine to "disabled" to ensure we exercise the tts_disabled path.
    """
    from plugins.tts_lipsync import TTSLipSyncPlugin

    # Create instance without running __init__ to avoid heavy imports
    plugin = TTSLipSyncPlugin.__new__(TTSLipSyncPlugin)

    # Mark disabled
    plugin.enabled = False

    # Stub config_registry to return disabled for ACTIVE_VOX_ENGINE
    import core.config_manager as cm

    orig_get = cm.config_registry.get_value

    def fake_get(key, default=None, **kwargs):
        if key == "ACTIVE_VOX_ENGINE":
            return "disabled"
        return orig_get(key, default, **kwargs)

    cm.config_registry.get_value = fake_get

    action = {"type": "tts_speak", "payload": {"text": "hello"}}

    async def run():
        result = await plugin.execute_action(
            action, context={}, bot=None, original_message=None
        )
        assert isinstance(result, dict)
        assert result.get("status") == "skipped"
        assert result.get("reason") == "tts_disabled"

    try:
        asyncio.run(run())
    finally:
        cm.config_registry.get_value = orig_get


def test_refresh_config_respects_enabled_flag():
    """refresh_config should read TTS_ENABLED and endpoints correctly."""
    from plugins.tts_lipsync import TTSLipSyncPlugin

    plugin = TTSLipSyncPlugin.__new__(TTSLipSyncPlugin)

    # Fake config get_value to simulate webui settings
    def fake_get_value(key, default=None, **kwargs):
        if key == "TTS_ENABLED":
            return True
        if key == "TTS_ENDPOINTS":
            return "http://example/tts"
        if key == "TTS_TIMEOUT_SECONDS":
            return 10
        if key == "TTS_OUTPUT_DIR":
            return "./tmp_tts"
        if key == "TTS_FALLBACK_TO_TEXT":
            return True
        return default

    # Patch config_registry.get_value
    import core.config_manager as cm

    old_get = cm.config_registry.get_value
    cm.config_registry.get_value = fake_get_value

    try:
        plugin.refresh_config()
        assert plugin.endpoints == ["http://example/tts"]
        assert plugin.enabled is True
        assert plugin.timeout_s == 10
        assert plugin.fallback_to_text is True
    finally:
        cm.config_registry.get_value = old_get


def test_refresh_config_can_disable():
    """When TTS_ENABLED is False, plugin.enabled should be False even if endpoints exist."""
    from plugins.tts_lipsync import TTSLipSyncPlugin

    plugin = TTSLipSyncPlugin.__new__(TTSLipSyncPlugin)

    def fake_get_value(key, default=None, **kwargs):
        if key == "TTS_ENABLED":
            return False
        if key == "TTS_ENDPOINTS":
            return "http://example/tts"
        return default

    import core.config_manager as cm

    old_get = cm.config_registry.get_value
    cm.config_registry.get_value = fake_get_value

    try:
        plugin.refresh_config()
        assert plugin.endpoints == ["http://example/tts"]
        assert plugin.enabled is False
    finally:
        cm.config_registry.get_value = old_get
