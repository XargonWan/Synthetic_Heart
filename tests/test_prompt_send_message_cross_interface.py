"""``send_message`` reaches any interface, so a real-interface turn is offered it."""

from __future__ import annotations

from typing import Any

import pytest

from core.prompt_engine import _derive_default_prompt_action_types


@pytest.fixture()
def interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    from core import core_initializer as ci

    monkeypatch.setattr(
        ci,
        "INTERFACE_REGISTRY",
        {name: object() for name in ("synth_webui", "telegram", "discord_bot")},
    )


def _actions() -> dict[str, Any]:
    return {
        # Merged by the core from the interfaces that declare it; the WebUI
        # answers with its own message_synth_webui and is not among them.
        "send_message": {"source": "discord_bot, telegram", "scope": "core"},
        "message_synth_webui": {"source": "message_plugin", "scope": "core"},
        "discord_only_thing": {"source": "discord_bot", "scope": "core"},
    }


def test_webui_turn_is_offered_send_message(interfaces: None) -> None:
    allowed = _derive_default_prompt_action_types(
        _actions(), "synth_webui", turn_scopes={"core"}
    )
    assert "send_message" in allowed
    assert "message_synth_webui" in allowed
    # The interface rule still hides what belongs to another interface.
    assert "discord_only_thing" not in allowed


def test_beat_turn_keeps_the_strict_interface_rule(interfaces: None) -> None:
    """A Grillo beat runs under a synthetic interface with explicit targets."""
    allowed = _derive_default_prompt_action_types(
        _actions(),
        "grillo",
        turn_scopes={"core"},
        outbound_target_interfaces={"synth_webui"},
    )
    assert "send_message" not in allowed


def test_unregistered_synthetic_interface_keeps_the_strict_rule(
    interfaces: None,
) -> None:
    allowed = _derive_default_prompt_action_types(
        _actions(), "grillo", turn_scopes={"core"}
    )
    assert "send_message" not in allowed
