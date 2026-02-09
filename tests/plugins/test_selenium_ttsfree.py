import asyncio
import os
import tempfile
import sys
import importlib.util

import pytest

# Ensure project root is on sys.path so tests can import plugins_dev when pytest's
# execution directory does not already include it.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from plugins.selenium_ttsfree import SeleniumTTSFreePlugin
except ModuleNotFoundError:
    from plugins_dev.selenium_ttsfree import SeleniumTTSFreePlugin


def test_validate_payload_success():
    payload = {
        "message": "Ciao, tutto ok?",
        "language": "italian",
        "voice": ["italian", "Isabella", 10, 20],
        "interface_path": "telegram_bot/123/456",
    }
    errs = SeleniumTTSFreePlugin.validate_payload("voice_message_ttsfree", payload)
    assert errs == []


def test_validate_payload_failures():
    # Too long
    long_msg = "a" * 501
    payload = {"message": long_msg, "language": "italian", "voice": ["italian", "Isabella"], "interface_path": "x"}
    errs = SeleniumTTSFreePlugin.validate_payload("voice_message_ttsfree", payload)
    assert any("exceeds 500" in e for e in errs)

    # Emoji not allowed
    payload2 = {"message": "Ciao 😃", "language": "italian", "voice": ["italian", "Isabella"], "interface_path": "x"}
    errs2 = SeleniumTTSFreePlugin.validate_payload("voice_message_ttsfree", payload2)
    assert any("unsupported characters" in e for e in errs2)

    # Missing voice is allowed (mapping is used instead)
    payload3 = {"message": "ok", "language": "italian", "interface_path": "x"}
    errs3 = SeleniumTTSFreePlugin.validate_payload("voice_message_ttsfree", payload3)
    assert errs3 == []


@pytest.mark.asyncio
async def test_execute_action_dispatch(monkeypatch, tmp_path):
    plugin = SeleniumTTSFreePlugin()

    # Mock generate speech to avoid launching Selenium
    temp_mp3 = tmp_path / "out.mp3"
    temp_mp3.write_bytes(b"MP3TEST")

    async def fake_generate(text, language, voice):
        return str(temp_mp3)

    monkeypatch.setattr(plugin, "_generate_speech", fake_generate)

    # Prepare fake interface registry entry
    from core.core_initializer import INTERFACE_REGISTRY

    sent = {}

    class FakeInterface:
        async def send_audio(self, payload):
            sent['payload'] = payload

    # stash previous value if any and set our fake
    prev = INTERFACE_REGISTRY.get('test_iface')
    INTERFACE_REGISTRY['test_iface'] = FakeInterface()

    action = {
        "type": "voice_message_ttsfree",
        "payload": {
            "message": "Ciao",
            "language": "italian",
            "voice": ["italian", "Isabella"],
            "interface_path": "test_iface/1/2",
        }
    }

    # Execute
    await plugin.execute_action(action, {}, None, None)

    # cleanup
    if prev is None:
        INTERFACE_REGISTRY.pop('test_iface', None)
    else:
        INTERFACE_REGISTRY['test_iface'] = prev

    # Assert dispatched
    assert 'payload' in sent
    assert sent['payload']['audio'] == str(temp_mp3)
    assert sent['payload']['interface_path'] == 'test_iface/1/2'


@pytest.mark.asyncio
async def test_execute_action_resolves_mapping(monkeypatch, tmp_path):
    # Ensure plugin will resolve voice mapping from Free_TTS_VOICES when voice key (string) is provided
    plugin = SeleniumTTSFreePlugin()

    temp_mp3 = tmp_path / "out.mp3"
    temp_mp3.write_bytes(b"MP3TEST")

    async def fake_generate(text, language, voice):
        # verify resolved voice is as expected
        assert voice[1] == "Isabella"
        return str(temp_mp3)

    monkeypatch.setattr(plugin, "_generate_speech", fake_generate)

    # Monkeypatch config_registry to return mapping
    from core import config_manager

    # The default mapping is provided by variables_engine.register_all so no setup required

    from core.core_initializer import INTERFACE_REGISTRY

    sent = {}

    class FakeInterface:
        async def send_audio(self, payload):
            sent['payload'] = payload

    prev = INTERFACE_REGISTRY.get('test_iface')
    INTERFACE_REGISTRY['test_iface'] = FakeInterface()

    action = {
        "type": "voice_message_ttsfree",
        "payload": {
            "message": "Ciao",
            "language": "italian",
            "voice": "italian",
            "interface_path": "test_iface/1/2",
        }
    }

    await plugin.execute_action(action, {}, None, None)

    if prev is None:
        INTERFACE_REGISTRY.pop('test_iface', None)
    else:
        INTERFACE_REGISTRY['test_iface'] = prev

    assert 'payload' in sent
    assert sent['payload']['audio'] == str(temp_mp3)
