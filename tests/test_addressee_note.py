"""Tests for the structural addressee-note identity guard.

When a human opens their message by addressing Synth with its own name/alias
("Rekku, ..."), weak models mirror the vocative back and address the USER as
the synth (live incident 2026-08-26). The guard renders a short grounding note
in the current-turn prefix re-anchoring who is who. Structural only: matches
Synth's configured name set, never user-text semantics.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.prompt_engine import _assemble_prompt_request


def _build(text: str, synth_name="Rekku", grillo=False):
    import core.config_manager as cm

    original_get_value = cm.config_registry.get_value

    def fake_get_value(key, default=None, **kwargs):
        if key == "SYNTH_NAME":
            return synth_name
        if key == "SYNTH_ALIASES":
            return ""
        return original_get_value(key, default, **kwargs)

    cm.config_registry.get_value = fake_get_value
    try:
        ctx = _assemble_prompt_request(
            prompt_dict={"instructions": "test"},
            context_section={},
            text=text,
            interface_name="telegram_bot",
            interface_path="telegram_bot/123",
            message=SimpleNamespace(
                from_user=SimpleNamespace(
                    username="Xargon", first_name="Jay", last_name="Cheshire"
                )
            ),
            is_grillo_internal=grillo,
            beat_type="",
            is_voice_input=False,
            resolved_language=None,
            resolved_message_tone=None,
            image_data=None,
            attachments=None,
            allowed_action_types=None,
        )
    finally:
        cm.config_registry.get_value = original_get_value
    return ctx


@pytest.mark.parametrize("opening", ["Rekku,", "rekku:", "Rekku?", "*Rekku,*"])
def test_note_added_when_addressed_by_synth_name(opening) -> None:
    req = _build(f"{opening} che ne pensi?")
    assert "'Rekku'" in req.runtime_ctx.addressee_note
    assert "Jay" in req.runtime_ctx.addressee_note


def test_no_note_without_vocative() -> None:
    req = _build("Ciao, che ne pensi?")
    assert req.runtime_ctx.addressee_note == ""


def test_no_note_for_grillo_beats() -> None:
    req = _build("Rekku, rifletti", grillo=True)
    assert req.runtime_ctx.addressee_note == ""


def test_note_renders_into_current_turn_prefix() -> None:
    from core.prompt_renderers import OpenAIRenderer

    req = _build("Rekku, che ne pensi?")
    messages = OpenAIRenderer(req).render()
    user_turns = [m for m in messages if m["role"] == "user"]
    assert user_turns, "expected at least one user turn"
    assert any("YOUR own name" in str(m["content"]) for m in user_turns)
