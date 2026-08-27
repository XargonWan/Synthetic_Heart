"""Tests for the leaked Recon-schema action drop in ``run_actions``.

A state-retaining browser engine (e.g. ``selenium-llm-engine``) can echo the
separate Recon call's JSON keys back into the main-pass ``actions`` array. Those
keys (``tone_hint``, ``agent_intent``, ...) are preflight metadata, not
executable actions, and would otherwise fail validation with "Unsupported type"
and starve the turn (the "😵" fallback bug). ``run_actions`` structurally drops
them before validation, using the reflectively-collected recon-key set.
"""

from types import SimpleNamespace

import pytest

import core.action_parser as action_parser


@pytest.mark.asyncio
async def test_leaked_recon_actions_dropped_before_validation(monkeypatch):
    monkeypatch.setattr(
        action_parser,
        "get_registered_recon_keys",
        lambda: {"tone_hint", "agent_intent", "language_hint"},
        raising=False,
    )
    # Also patch the import target used inside run_actions.
    import core.recon as recon

    monkeypatch.setattr(
        recon,
        "get_registered_recon_keys",
        lambda: {"tone_hint", "agent_intent", "language_hint"},
    )

    validated: list[dict] = []

    def fake_validate(a, c, o):
        validated.append(a)
        return (True, [])

    monkeypatch.setattr(action_parser, "validate_action", fake_validate)

    async def fake_run_action(a, c, b, o):
        return {}

    monkeypatch.setattr(action_parser, "run_action", fake_run_action)

    actions = [
        {"type": "tone_hint", "payload": {}},
        {"type": "agent_intent", "payload": {}},
        {
            "type": "message_telegram_bot",
            "payload": {"text": "hi", "interface_path": "telegram_bot/1"},
        },
    ]
    original = SimpleNamespace(from_cortex=True)

    await action_parser.run_actions(actions, {}, bot=None, original_message=original)

    # Only the real deliverable action reached validation.
    validated_types = [a.get("type") for a in validated]
    assert validated_types == ["message_telegram_bot"]


@pytest.mark.asyncio
async def test_real_actions_untouched_when_no_recon_keys(monkeypatch):
    import core.recon as recon

    monkeypatch.setattr(recon, "get_registered_recon_keys", lambda: set())

    validated: list[dict] = []

    def fake_validate(a, c, o):
        validated.append(a)
        return (True, [])

    monkeypatch.setattr(action_parser, "validate_action", fake_validate)

    async def fake_run_action(a, c, b, o):
        return {}

    monkeypatch.setattr(action_parser, "run_action", fake_run_action)

    actions = [
        {
            "type": "vessel_minecraft_say",
            "payload": {"text": "hello world"},
        }
    ]
    original = SimpleNamespace(from_cortex=True)

    await action_parser.run_actions(actions, {}, bot=None, original_message=original)

    assert [a.get("type") for a in validated] == ["vessel_minecraft_say"]
