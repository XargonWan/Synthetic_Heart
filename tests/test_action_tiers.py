from __future__ import annotations

from typing import Any

import pytest

from core import action_tiers
from core.prompt_engine import minify_actions_block
from plugins.recon.recon_action_needs import ReconActionNeedsPlugin


# The owning plugins register these themselves (see the last test); the tests that
# build a catalogue by hand need them registered whichever plugins were imported.
action_tiers.register_always_full_actions(
    "create_personal_diary_entry", "update_emotion_state"
)
action_tiers.register_state_actions(
    "self_growth_pending_proposal", "apply_growth_proposal"
)


def _actions() -> dict[str, dict[str, Any]]:
    return {
        "message_telegram_bot": {
            "description": "Send a message via Telegram",
            "required_fields": ["text", "target"],
            "optional_fields": ["parse_mode"],
        },
        "create_personal_diary_entry": {
            "description": "Write a diary entry. It is mandatory after turns.",
            "required_fields": ["interaction_summary"],
            "optional_fields": ["personal_thought", "emotions"],
        },
        "goal_set": {
            "description": "Create a goal for the persona. " + "Long detail. " * 60,
            "required_fields": ["title"],
            "optional_fields": ["notes", "due"],
        },
        "bio_update": {
            "description": "Update someone's profile",
            "required_fields": ["target", "fields"],
        },
        "apply_growth_proposal": {
            "description": "Apply the pending growth proposal",
            "required_fields": [],
        },
    }


def test_core_actions_are_the_fixed_set_and_every_message_action() -> None:
    assert action_tiers.is_core_action("send_message")
    assert action_tiers.is_core_action("message_telegram_bot")
    assert action_tiers.is_core_action("create_personal_diary_entry")
    assert not action_tiers.is_core_action("goal_set")


def test_short_brief_keeps_the_first_sentence_and_caps_the_length() -> None:
    assert action_tiers.short_brief("Create a goal. Then do much more.") == (
        "Create a goal."
    )
    assert len(action_tiers.short_brief("x" * 500)) <= 110


def test_selection_is_core_plus_recon_plus_state_driven() -> None:
    names = list(_actions())

    nothing_extra = action_tiers.select_full_schema_actions(names, set())
    assert nothing_extra == {"message_telegram_bot", "create_personal_diary_entry"}

    with_goal = action_tiers.select_full_schema_actions(names, {"goal_set"})
    assert "goal_set" in with_goal and "bio_update" not in with_goal

    # Unknown names from the Recon model are ignored, never invented.
    assert "ghost" not in action_tiers.select_full_schema_actions(names, {"ghost"})

    # A pending proposal puts its action in full without Recon asking.
    pending = action_tiers.select_full_schema_actions(
        names, set(), context_keys={"self_growth_pending_proposal"}
    )
    assert "apply_growth_proposal" in pending


def test_tiered_catalogue_is_much_smaller_and_keeps_every_action() -> None:
    actions = _actions()
    full = minify_actions_block(actions)
    tiered = minify_actions_block(
        actions, full_schema_for={"message_telegram_bot", "create_personal_diary_entry"}
    )

    assert set(tiered) == set(full), "no action may disappear from the catalogue"
    assert "schema" in tiered["create_personal_diary_entry"]
    assert "schema" not in tiered["goal_set"]
    assert tiered["goal_set"]["payload_keys"] == ["title", "notes", "due"]
    assert tiered["goal_set"]["required_payload_keys"] == ["title"]
    assert len(str(tiered)) < len(str(full)) * 0.75


def test_without_a_verdict_the_catalogue_is_sent_whole() -> None:
    actions = _actions()
    assert minify_actions_block(actions, full_schema_for=None) == minify_actions_block(
        actions
    )


def test_optional_briefs_exclude_core_and_system_only_actions() -> None:
    briefs = action_tiers.optional_action_briefs(_actions(), skip={"bio_update"})
    assert set(briefs) == {"goal_set", "apply_growth_proposal"}
    assert len(briefs["goal_set"]) <= 110


@pytest.mark.asyncio
async def test_recon_plugin_returns_a_verdict_only_for_a_usable_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.recon.recon_action_needs as module

    monkeypatch.setattr(module, "_available_actions", _actions)
    plugin = ReconActionNeedsPlugin()

    ok = await plugin.parse_recon_response({"actions": ["goal_set", "ghost"]})
    assert ok[0]["type"] == "action_hint" and ok[0]["names"] == ["goal_set"]

    # "none needed" is a verdict too (an empty list), distinct from "no answer".
    none_needed = await plugin.parse_recon_response({"actions": []})
    assert none_needed and none_needed[0]["names"] == []

    for unusable in (None, 42, {"other": 1}):
        assert await plugin.parse_recon_response(unusable) == []


def test_recon_instruction_lists_only_optional_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.recon.recon_action_needs as module

    monkeypatch.setattr(module, "_available_actions", _actions)
    instruction = ReconActionNeedsPlugin().get_recon_instruction()

    assert "goal_set" in instruction and "bio_update" in instruction
    assert "message_telegram_bot" not in instruction
    assert "create_personal_diary_entry" not in instruction


def test_recon_plugin_sits_out_when_tiering_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.recon.recon_action_needs as module

    monkeypatch.setattr(module, "_available_actions", _actions)
    monkeypatch.setattr(action_tiers, "tiering_enabled", lambda: False)
    assert ReconActionNeedsPlugin().is_recon_eligible() is False
    monkeypatch.setattr(action_tiers, "tiering_enabled", lambda: True)
    assert ReconActionNeedsPlugin().is_recon_eligible() is True


def test_each_plugin_registers_the_actions_it_owns() -> None:
    """The core names no plugin's action: the owning plugin's module registers it."""
    import plugins.ai_diary  # noqa: F401
    import plugins.emotion_manager  # noqa: F401
    import plugins.grillo.grillo_growth  # noqa: F401

    assert action_tiers.is_core_action("create_personal_diary_entry")
    assert action_tiers.is_core_action("update_emotion_state")
    # Core's own: the unified send, the animation and the per-interface messages.
    assert action_tiers.is_core_action("send_message")
    assert action_tiers.is_core_action("use_animation")
    assert action_tiers.is_core_action("message_anything")
    selected = action_tiers.select_full_schema_actions(
        ["apply_growth_proposal"], set(), {"self_growth_pending_proposal"}
    )
    assert selected == {"apply_growth_proposal"}


def test_the_plugin_reads_the_actions_the_prompt_engine_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``actions_block`` is on the ``core_initializer`` instance, not the module.

    Reading it off the module gave an empty catalogue, so the plugin declined every
    turn and the prompt silently stayed whole.
    """
    from core.core_initializer import core_initializer
    from plugins.recon.recon_action_needs import _available_actions

    monkeypatch.setattr(
        core_initializer, "actions_block", {"available_actions": _actions()}
    )
    assert set(_available_actions()) == set(_actions())
    assert ReconActionNeedsPlugin().is_recon_eligible() is True
