"""Tests for ``core.interface_capabilities`` and its dispatch-gate wiring.

Covers:

* structural capability derivation from method presence and action names;
* the explicit ``get_capabilities()`` hook;
* fail-open behaviour of ``has_capability`` on a broken interface;
* the config toggle for the gate;
* the dispatch gate in ``core.action_parser`` returning an explicit,
  structured ``{"ok": False}`` failure instead of falling through silently.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import core.action_parser as ap
from core.interface_capabilities import (
    capability_gate_enabled,
    has_capability,
    interface_capabilities,
)


class _MessageInterface:
    def send_message(self, payload: dict, original_message=None) -> bool:
        return True

    def send_audio(self, payload: dict, original_message=None) -> bool:
        return True

    def get_supported_actions(self) -> dict:
        return {"message_cap_bot": {"required_fields": ["text"]}}


class _NoMessageInterface:
    async def send_file(self, payload: dict, **kwargs) -> bool:
        return True


class _BrokenInterface:
    def get_supported_actions(self) -> dict:
        raise RuntimeError("introspection boom")


class _HookInterface:
    def get_capabilities(self) -> list:
        return ["send_message", "custom_cap"]

    def send_audio(self, payload: dict) -> bool:
        return True


class _TtsAudioInterface:
    def send_tts_audio(self, payload: dict) -> bool:
        return True


def test_interface_capabilities_derives_from_method_presence() -> None:
    caps = interface_capabilities(_MessageInterface())
    assert "send_message" in caps
    assert "send_audio" in caps
    assert "send_file" not in caps
    assert "send_voice" not in caps


def test_interface_capabilities_includes_supported_action_names() -> None:
    caps = interface_capabilities(_MessageInterface())
    assert "message_cap_bot" in caps


def test_interface_capabilities_prefers_get_capabilities_hook() -> None:
    caps = interface_capabilities(_HookInterface())
    assert caps == frozenset({"send_message", "custom_cap"})
    # The hook wins: send_audio is present on the object but not advertised.
    assert "send_audio" not in caps


def test_interface_capabilities_send_tts_audio_implies_send_audio() -> None:
    caps = interface_capabilities(_TtsAudioInterface())
    assert "send_audio" in caps


def test_interface_capabilities_none_returns_empty() -> None:
    assert interface_capabilities(None) == frozenset()


def test_has_capability_negative() -> None:
    assert has_capability(_NoMessageInterface(), "send_message") is False
    assert has_capability(_NoMessageInterface(), "send_file") is True


def test_has_capability_fail_open_on_broken_object() -> None:
    assert has_capability(_BrokenInterface(), "send_message") is True


def test_capability_gate_enabled_parses_boolish_values(monkeypatch) -> None:
    import core.interface_capabilities as ic

    monkeypatch.setattr(ic.config_registry, "get_value", lambda *a, **k: "0")
    assert capability_gate_enabled() is False

    monkeypatch.setattr(ic.config_registry, "get_value", lambda *a, **k: "false")
    assert capability_gate_enabled() is False

    monkeypatch.setattr(ic.config_registry, "get_value", lambda *a, **k: "1")
    assert capability_gate_enabled() is True


def test_capability_gate_enabled_fail_open_on_config_error(monkeypatch) -> None:
    import core.interface_capabilities as ic

    def _boom(*a, **k) -> None:
        raise RuntimeError("config read boom")

    monkeypatch.setattr(ic.config_registry, "get_value", _boom)
    assert capability_gate_enabled() is True


def _register_interface(monkeypatch: pytest.MonkeyPatch, name: str, iface: Any) -> None:
    import core.core_initializer as ci

    monkeypatch.setitem(ci.INTERFACE_REGISTRY, name, iface)


@pytest.mark.asyncio
async def test_dispatch_missing_send_message_returns_structured_failure(
    monkeypatch,
) -> None:
    _register_interface(monkeypatch, "cap_test_bot", _NoMessageInterface())
    monkeypatch.setattr(ap, "_plugins_for", lambda action_type: [])
    monkeypatch.setattr(ap, "_interface_capability_gate_enabled", lambda: True)

    action = {
        "type": "message_cap_test_bot",
        "interface": "cap_test_bot",
        "payload": {"text": "hello"},
    }

    result = await ap._handle_plugin_action(
        action, context={}, bot=None, original_message=None
    )

    assert result is not None
    assert result.get("ok") is False
    assert "lacks send_message capability" in result.get("error", "")


@pytest.mark.asyncio
async def test_dispatch_missing_audio_capability_returns_structured_failure(
    monkeypatch,
) -> None:
    _register_interface(monkeypatch, "cap_test_bot", _NoMessageInterface())
    monkeypatch.setattr(ap, "_plugins_for", lambda action_type: [])
    monkeypatch.setattr(ap, "_interface_capability_gate_enabled", lambda: True)

    action = {
        "type": "audio_cap_test_bot",
        "interface": "cap_test_bot",
        "payload": {"audio_path": "/tmp/x.wav"},
    }

    result = await ap._handle_plugin_action(
        action, context={}, bot=None, original_message=None
    )

    assert result is not None
    assert result.get("ok") is False
    assert "lacks audio capability" in result.get("error", "")


@pytest.mark.asyncio
async def test_dispatch_send_message_capable_interface_dispatches(monkeypatch) -> None:
    _register_interface(monkeypatch, "cap_test_bot", _MessageInterface())
    monkeypatch.setattr(ap, "_plugins_for", lambda action_type: [])
    monkeypatch.setattr(ap, "_interface_capability_gate_enabled", lambda: True)

    action = {
        "type": "message_cap_test_bot",
        "interface": "cap_test_bot",
        "payload": {"text": "hello"},
    }

    result = await ap._handle_plugin_action(
        action, context={}, bot=None, original_message=None
    )

    assert result is not None
    assert result.get("ok") is True


@pytest.mark.asyncio
async def test_dispatch_gate_disabled_preserves_legacy_silent_return(
    monkeypatch,
) -> None:
    # With the gate disabled, a capability-less interface falls through silently
    # exactly as before (None result, no structured failure).
    _register_interface(monkeypatch, "cap_test_bot", _NoMessageInterface())
    monkeypatch.setattr(ap, "_plugins_for", lambda action_type: [])
    monkeypatch.setattr(ap, "_interface_capability_gate_enabled", lambda: False)

    action = {
        "type": "message_cap_test_bot",
        "interface": "cap_test_bot",
        "payload": {"text": "hello"},
    }

    result = await ap._handle_plugin_action(
        action, context={}, bot=None, original_message=SimpleNamespace()
    )

    assert result is None
