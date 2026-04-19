import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Sequence

from core.prompt_engine import (
    build_json_prompt,
    build_live_prompt_request,
    build_live_system_instruction,
    load_json_instructions,
    load_unminified_chat_instruction,
)


def test_build_json_prompt_reply_without_text(monkeypatch):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id=1,
        text="hello",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime.now(timezone.utc),
        reply_to_message=SimpleNamespace(
            message_id=2,
            from_user=SimpleNamespace(full_name="bot", username="bot"),
        ),
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="discord_bot"))
    assert result["input"]["interface"] == "discord_bot"
    assert (
        result["input"]["payload"]["reply_message_id"]["text"] == "[Non-text content]"
    )
    # Phase 8: transport prompt no longer carries instructions_verbose.
    assert "instructions_verbose" not in result
    pr = result.get("__prompt_request")
    assert pr is not None
    assert "MASTER INSTRUCTION" in pr.system_instruction


def test_build_json_prompt_inherits_image_data_from_context_memory(monkeypatch):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id=1,
        text="hello",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime.now(timezone.utc),
    )

    image_data = {
        "type": "image",
        "source": {"interface": "webui", "user_id": 1},
        "image_data": {"path": "/tmp/test.jpg"},
    }
    context_memory = {"image_data": image_data}

    result = asyncio.run(
        build_json_prompt(message, context_memory, interface_name="discord_bot")
    )

    assert result["input"]["payload"]["image"] == image_data


def test_instructions_prohibit_embedded_emotion_tags():
    instructions = load_json_instructions()
    assert "Do NOT embed emotion tags" in instructions, (
        "Instructions should forbid embedding emotion tags in message text"
    )


def test_instructions_enforce_first_person_identity():
    instructions = load_json_instructions()
    assert "Stay inside the active persona in first person" in instructions
    assert "PRONOUN CONSISTENCY" in instructions
    assert (
        "Do not neutralize an established he/him or she/her person into singular they/them"
        in instructions
    )


def test_unminified_chat_instruction_enforces_identity_rules():
    instructions = load_unminified_chat_instruction("telegram_bot")
    assert "Stay in the active persona in first person" in instructions
    assert "Keep pronouns consistent" in instructions
    assert (
        "do not replace an established he/him or she/her person with singular they/them"
        in instructions
    )


def test_build_live_system_instruction_enforces_identity_rules(monkeypatch):
    async def dummy_gather(message, ctx):
        return {"persona": "You are 2B."}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    result = asyncio.run(build_live_system_instruction())

    assert "You are 2B." in result
    assert "Stay fully inside the active persona in first person" in result
    assert "Keep participant pronouns consistent" in result
    assert (
        "never replace an established he/him or she/her person with singular they/them"
        in result
    )


def test_build_live_prompt_request_returns_live_mode(monkeypatch):
    from core.prompt_request import PromptRequest

    async def dummy_gather(message, ctx):
        return {"persona": "You are 2B."}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    req = asyncio.run(build_live_prompt_request())
    assert isinstance(req, PromptRequest)
    assert req.mode == "live"
    assert "You are 2B." in req.system_instruction


def test_build_json_prompt_demotes_persona_preferences_to_context_summary(monkeypatch):
    async def dummy_gather(message, ctx):
        return {
            "persona": "PERSONA IDENTITY:\nName: 2B\nProfile: You are 2B.",
            "persona_preferences": "Likes: tea\nDislikes: liars",
        }

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id=1,
        text="hey",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime.now(timezone.utc),
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="telegram_bot"))

    assert "Likes: tea" not in result["instructions"]
    assert "Dislikes: liars" not in result["instructions"]

    pr = result["__prompt_request"]
    assert "Likes: tea" not in pr.system_instruction
    assert "Dislikes: liars" not in pr.system_instruction
    assert "[Persona background]" in pr.context_summary
    assert "Likes: tea" in pr.context_summary
    assert "Dislikes: liars" in pr.context_summary


def test_build_live_prompt_request_keeps_persona_preferences(monkeypatch):
    async def dummy_gather(message, ctx):
        return {
            "persona": "You are 2B.",
            "persona_preferences": "Likes: tea\nDislikes: liars",
        }

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    req = asyncio.run(build_live_prompt_request())

    assert "You are 2B." in req.system_instruction
    assert "Background preferences and interests:" in req.system_instruction
    assert "Likes: tea" in req.system_instruction
    assert "Dislikes: liars" in req.system_instruction


def test_build_live_system_instruction_matches_live_renderer(monkeypatch):
    from core.prompt_renderers import LiveRenderer

    async def dummy_gather(message, ctx):
        return {"persona": "You are 2B."}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    req = asyncio.run(build_live_prompt_request())
    rendered = LiveRenderer(req).render_as_text()
    built = asyncio.run(build_live_system_instruction())

    assert built == rendered


def test_build_json_prompt_filters_actions_by_allowlist(monkeypatch):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    from core.core_initializer import core_initializer

    original_actions_block = core_initializer.actions_block
    core_initializer.actions_block = {
        "available_actions": {
            "create_personal_diary_entry": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Create diary entry.",
                "source": "ai_diary",
            },
            "message_telegram_bot": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Send Telegram message.",
                "source": "telegram_bot",
            },
        }
    }

    try:
        message = SimpleNamespace(
            chat_id=-1,
            text="internal beat",
            message_id=1,
            from_user=SimpleNamespace(full_name="grillo", username="grillo"),
            date=datetime.now(timezone.utc),
            interface_path="grillo/-1",
        )

        context_memory = {
            "grillo_beat": True,
            "beat_type": "self_reflection",
            "allowed_action_types": ["create_personal_diary_entry"],
        }

        result = asyncio.run(
            build_json_prompt(message, context_memory, interface_name="grillo")
        )

        assert list(result["actions"].keys()) == ["create_personal_diary_entry"]
    finally:
        core_initializer.actions_block = original_actions_block


def test_prompt_request_attached_to_result(monkeypatch):
    """build_json_prompt must attach a PromptRequest under '__prompt_request'."""
    from core.prompt_request import PromptRequest

    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id=1,
        text="hey",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime.now(timezone.utc),
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="telegram_bot"))

    assert "__prompt_request" in result, (
        "build_json_prompt must attach a PromptRequest under '__prompt_request'"
    )
    pr = result["__prompt_request"]
    assert isinstance(pr, PromptRequest), f"Expected PromptRequest, got {type(pr)}"
    assert pr.system_instruction, "system_instruction must be non-empty"
    assert pr.current_text, "current_text must be non-empty"
    assert pr.mode in ("chat", "grillo", "delivery", "live"), (
        f"Unexpected mode: {pr.mode!r}"
    )


def test_prompt_request_mode_is_grillo_for_grillo_beat(monkeypatch):
    """Grillo beats should produce a PromptRequest with mode='grillo'."""
    from core.prompt_request import PromptRequest

    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id=-1,
        text="internal beat",
        message_id=1,
        from_user=SimpleNamespace(full_name="grillo", username="grillo"),
        date=datetime.now(timezone.utc),
        interface_path="grillo/-1",
    )

    result = asyncio.run(
        build_json_prompt(
            message,
            {"grillo_beat": True, "beat_type": "self_reflection"},
            interface_name="grillo",
        )
    )

    assert "__prompt_request" in result
    pr = result["__prompt_request"]
    assert isinstance(pr, PromptRequest)
    assert pr.mode == "grillo", f"Expected mode='grillo', got {pr.mode!r}"
    assert pr.runtime_ctx.is_grillo_beat is True
    assert "GRILLO INTERNAL MODE" in result.get("instructions", "")
    assert "DO NOT emit any message_* action" in result.get("instructions", "")


# ── _history_to_turns tests ──────────────────────────────────────────────


class TestHistoryToTurns:
    """Tests for _history_to_turns: converting formatted history lines to Turn objects."""

    def _call(
        self, lines: Sequence[object], synth_names: set[str] | None = None
    ) -> list[Any]:
        from core.prompt_engine import _history_to_turns

        return _history_to_turns(list(lines), synth_names or {"synth"})

    def test_self_sender_becomes_assistant(self) -> None:
        """'self' is the canonical sender_name for the AI in history format."""
        lines = ['[13/04/26:0858] self: "Hello from the AI"']
        turns = self._call(lines, {"2b"})
        assert len(turns) == 1
        assert turns[0].role == "assistant"
        assert "Hello from the AI" in turns[0].content

    def test_synth_name_becomes_assistant(self) -> None:
        lines = ['[13/04/26:0900] 2B: "I am 2B"']
        turns = self._call(lines, {"2b"})
        assert len(turns) == 1
        assert turns[0].role == "assistant"

    def test_user_sender_becomes_user(self) -> None:
        lines = ['[13/04/26:0924] Scar: "Hey there"']
        turns = self._call(lines, {"2b"})
        assert len(turns) == 1
        assert turns[0].role == "user"
        assert "Hey there" in turns[0].content

    def test_mixed_conversation_roles(self) -> None:
        lines = [
            '[13/04/26:0924] Scar: "How are you?"',
            '[13/04/26:0925] self: "I am great!"',
            '[13/04/26:0926] Scar: "Good to hear"',
        ]
        turns = self._call(lines, {"2b"})
        assert [t.role for t in turns] == ["user", "assistant", "user"]

    def test_from_prefix_parsed_correctly(self) -> None:
        """Lines with [from ...] prefix should still parse sender and content."""
        lines = [
            '[from discord_bot/123/456] [12/04/26:2035] Remuraine: "Hello"',
            '[from discord_live_123] [12/04/26:2035] self: "Hi back"',
        ]
        turns = self._call(lines, {"synth"})
        assert len(turns) == 2
        assert turns[0].role == "user"
        assert turns[1].role == "assistant"

    def test_malformed_lines_skipped(self) -> None:
        lines = [
            "not a valid line",
            42,
            '[13/04/26:0924] Scar: "valid line"',
        ]
        turns = self._call(lines, {"synth"})
        assert len(turns) == 1
        assert turns[0].role == "user"

    def test_alias_detected_as_assistant(self) -> None:
        lines = ['[13/04/26:0900] Toobs: "My alias"']
        turns = self._call(lines, {"2b", "toobs"})
        assert len(turns) == 1
        assert turns[0].role == "assistant"
