"""Tests for the Recon-schema leak drop-filter in core.message_chain.

A state-retaining engine (e.g. the browser-driven selenium endpoint) can carry
Recon-pass priming into the immediately-following main pass, so the main pass
echoes Recon-schema keys (``tone_hint``, ``agent_intent`` …) back inside its
``actions`` array. Those keys are never real actions and, if left in place,
starve the turn of any deliverable action and drive the corrector into the
exhausting ``😵`` fallback loop. ``_drop_leaked_recon_actions`` removes them
before validation while preserving any real action in the same array.
"""

import core.message_chain as mc
from core.message_chain import _drop_leaked_recon_actions


def _patch_recon_keys(monkeypatch, keys):
    monkeypatch.setattr(
        "core.recon.get_registered_recon_keys",
        lambda: set(keys),
        raising=False,
    )


def test_drops_leaked_recon_actions_keeps_real_action(monkeypatch):
    _patch_recon_keys(monkeypatch, {"tone_hint", "agent_intent", "memory_search"})
    actions = [
        {"type": "tone_hint", "payload": {"tone": "warm"}},
        {"type": "agent_intent", "payload": {}},
        {"type": "vessel_minecraft_say", "payload": {"text": "hello"}},
        {"type": "memory_search", "payload": {}},
    ]

    kept = _drop_leaked_recon_actions(actions)

    assert kept == [{"type": "vessel_minecraft_say", "payload": {"text": "hello"}}]


def test_no_recon_keys_returns_untouched(monkeypatch):
    _patch_recon_keys(monkeypatch, set())
    actions = [{"type": "tone_hint"}, {"type": "vessel_minecraft_say"}]

    assert _drop_leaked_recon_actions(actions) == actions


def test_all_leaked_returns_empty(monkeypatch):
    _patch_recon_keys(monkeypatch, {"tone_hint", "web_search"})
    actions = [{"type": "tone_hint"}, {"type": "web_search"}]

    assert _drop_leaked_recon_actions(actions) == []


def test_helper_is_guarded_on_failure(monkeypatch):
    def _boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr("core.recon.get_registered_recon_keys", _boom, raising=False)
    actions = [{"type": "tone_hint"}, {"type": "vessel_minecraft_say"}]

    # Guarded: on any failure the actions are returned untouched.
    assert _drop_leaked_recon_actions(actions) == actions


def test_empty_and_non_list_inputs():
    assert _drop_leaked_recon_actions([]) == []
    assert _drop_leaked_recon_actions(None) is None


def test_action_key_alias_is_honored(monkeypatch):
    _patch_recon_keys(monkeypatch, {"tone_hint"})
    # Some LLMs use "action" instead of "type".
    actions = [
        {"action": "tone_hint"},
        {"action": "vessel_minecraft_say"},
    ]

    assert _drop_leaked_recon_actions(actions) == [{"action": "vessel_minecraft_say"}]


def test_helper_is_exposed_on_module():
    assert hasattr(mc, "_drop_leaked_recon_actions")
