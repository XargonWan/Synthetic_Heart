"""Per-turn action-scope gate: what the model is actually offered.

Two defects are pinned here.

1. **Scope declarations.** Only ``vessel_`` and ``agent_`` prefixes were mapped
   and almost nothing declared a scope, so whole integration suites fell through
   to the always-visible ``core`` default. An action that declares
   ``external_effects`` is now agent-scoped structurally (its own schema), which
   is what keeps agpeer/Home Assistant out of the Fast-Lane chat catalog.

2. **The bypass.** ``build_json_prompt`` scope-filtered the prompt dict's
   ``actions`` key, but the text catalog the model reads
   (``=== AVAILABLE ACTIONS ===``) was rendered by the bridge from
   ``tool_declarations``, built from the *raw* registry. The two disagreed on a
   live turn: 64 actions, 18 agpeer + 14 hass + ``vessel_connect``.
"""

from __future__ import annotations

import pytest

from core.prompt_engine import (
    _action_scopes_by_name,
    _derive_default_prompt_action_types,
    _scoped_actions_for_prompt,
)


def _catalogue() -> dict:
    """A slice of the real registry: the suites plus the chat essentials."""
    return {
        "send_message": {"required_fields": ["text"]},
        "create_personal_diary_entry": {"required_fields": ["content"]},
        "update_emotion_state": {"required_fields": ["emotions"]},
        "spawn_drone": {
            "required_fields": ["goal"],
            "scope": "core",
            "external_effects": ["drone"],
        },
        "vessel_connect": {"required_fields": ["game"]},
        "agpeer_status": {"external_effects": ["network"]},
        "agpeer_search": {
            "required_fields": ["query"],
            "external_effects": ["network"],
        },
        "hass_status": {"external_effects": ["network"]},
        "hass_call_service": {
            "required_fields": ["domain", "service"],
            "external_effects": ["network"],
        },
    }


# --- 1. scope resolution ---------------------------------------------------


def test_external_effects_implies_agent_scope():
    assert _action_scopes_by_name(
        "agpeer_search", {"external_effects": ["network"]}
    ) == {"agent"}
    assert _action_scopes_by_name("hass_status", {"external_effects": "network"}) == {
        "agent"
    }


def test_explicit_scope_beats_the_external_effects_rule():
    """``spawn_drone`` declares core on purpose (the Drone entry point)."""
    assert _action_scopes_by_name(
        "spawn_drone", {"scope": "core", "external_effects": ["drone"]}
    ) == {"core"}


def test_plain_chat_actions_stay_core():
    """A chat reply declares no external effects, so it is never scoped away."""
    assert _action_scopes_by_name("send_message", {"required_fields": ["text"]}) == {
        "core"
    }
    assert _action_scopes_by_name("create_personal_diary_entry", {}) == {"core"}


def test_namespacing_prefix_fallback_still_applies():
    assert _action_scopes_by_name("vessel_minecraft_say", {}) == {"vessel"}
    assert _action_scopes_by_name("agent_read_file", {}) == {"agent"}


# --- 2. what a chat turn is offered ----------------------------------------


def test_chat_turn_catalog_drops_the_integration_suites():
    kept = _derive_default_prompt_action_types(
        _catalogue(), "telegram_bot", turn_scopes={"core"}
    )
    assert "send_message" in kept
    assert "create_personal_diary_entry" in kept
    assert "spawn_drone" in kept  # explicit core scope wins
    for name in ("agpeer_status", "agpeer_search", "hass_status", "hass_call_service"):
        assert name not in kept, f"{name} must not be advertised on a chat turn"


def test_vessel_actions_stay_off_a_chat_turn():
    kept = _derive_default_prompt_action_types(
        _catalogue(), "telegram_bot", turn_scopes={"core"}
    )
    assert "vessel_connect" not in kept


def test_vessel_turn_keeps_vessel_actions():
    kept = _derive_default_prompt_action_types(
        _catalogue(),
        "vessel/minecraft",
        turn_scopes={"core", "vessel", "recon", "wiki"},
    )
    assert "vessel_connect" in kept


# --- 3. the bypass: declared tools must equal the offered catalog ----------


def test_declarations_use_the_prompt_dict_scoped_names():
    """The scoped dict is authoritative when present."""
    prompt_dict = {"actions": {"send_message": {}, "hass_status": {}}}
    scoped = _scoped_actions_for_prompt(
        _catalogue(),
        prompt_dict,
        interface_name="telegram_bot",
        interface_path="telegram_bot/-1",
        message=None,
        allowed_action_types=None,
    )
    assert set(scoped) == {"send_message", "hass_status"}


def test_declarations_fall_back_to_the_scope_gate():
    """With no scoped dict (injection failed upstream) the gate is re-applied."""
    scoped = _scoped_actions_for_prompt(
        _catalogue(),
        {},
        interface_name="telegram_bot",
        interface_path="telegram_bot/-1",
        message=None,
        allowed_action_types=None,
    )
    assert "send_message" in scoped
    assert "agpeer_search" not in scoped
    assert "hass_status" not in scoped


def test_explicit_allowlist_still_wins():
    scoped = _scoped_actions_for_prompt(
        _catalogue(),
        {"actions": {"send_message": {}, "hass_status": {}}},
        interface_name="telegram_bot",
        interface_path="telegram_bot/-1",
        message=None,
        allowed_action_types={"hass_status"},
    )
    assert set(scoped) == {"hass_status"}


def test_scope_filter_fails_open():
    """A broken prompt_dict must widen the catalog, never strip a capability."""

    class Explodes(dict):
        def get(self, *a, **k):  # pragma: no cover - exercised below
            raise RuntimeError("boom")

    raw = _catalogue()
    scoped = _scoped_actions_for_prompt(
        raw,
        Explodes(),
        interface_name="telegram_bot",
        interface_path="telegram_bot/-1",
        message=None,
        allowed_action_types=None,
    )
    assert set(scoped) == set(raw)


def test_assembled_request_declarations_match_the_scoped_catalog(monkeypatch):
    """End-to-end: the PromptRequest the bridge injects from is scoped."""
    import core.prompt_engine as pe
    from core.core_initializer import core_initializer

    monkeypatch.setattr(
        core_initializer, "actions_block", {"available_actions": _catalogue()}
    )
    # What ``build_json_prompt`` would have kept for a chat turn: the chat
    # essentials + spawn_drone (explicit core scope). vessel_connect is absent
    # because the scope gate already dropped it from the dict.
    chat_actions = [
        "send_message",
        "create_personal_diary_entry",
        "update_emotion_state",
        "spawn_drone",
    ]
    prompt_dict = {
        "instructions": "RULES",
        "context": {},
        "input": {"type": "message", "payload": {"text": "hello"}},
        "actions": {name: {} for name in chat_actions},
    }

    request = pe._assemble_prompt_request(
        prompt_dict=prompt_dict,
        context_section={},
        text="hello",
        interface_name="telegram_bot",
        interface_path="telegram_bot/-1",
        message=None,
        is_grillo_internal=False,
        beat_type="",
        is_voice_input=False,
        resolved_language=None,
        resolved_message_tone=None,
        image_data=None,
        attachments=None,
        allowed_action_types=None,
    )

    names = {m.name for m in (getattr(request, "tool_declarations", None) or [])}
    assert "send_message" in names
    assert not {n for n in names if n.startswith(("agpeer_", "hass_"))}, (
        f"integration suites leaked into the declarations: {sorted(names)}"
    )
    assert "vessel_connect" not in names


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
