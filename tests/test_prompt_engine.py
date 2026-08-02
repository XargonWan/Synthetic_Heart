import base64
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Sequence

from core.prompt_engine import (
    _build_context_summary,
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


def test_instructions_prohibit_referencing_input_metadata_prefix():
    instructions = load_json_instructions()
    assert "INPUT METADATA" in instructions
    assert "the user did not write it" in instructions


def test_instructions_require_chat_reply_action():
    instructions = load_json_instructions()
    assert "CHAT REPLY REQUIRED" in instructions
    assert "GRILLO INTERNAL MODE is NOT active" in instructions
    assert "hard failure" in instructions


def test_instructions_enforce_first_person_identity():
    instructions = load_json_instructions()
    assert "Stay inside the active persona in first person" in instructions
    assert "treat that as stale style noise" in instructions
    assert "PRONOUN CONSISTENCY" in instructions
    assert "MEMORY HONESTY" in instructions
    assert "prefer honesty over confidence" in instructions
    assert "never turn uncertainty into fiction" in instructions
    assert (
        "Do not neutralize an established he/him or she/her person into singular they/them"
        in instructions
    )


def test_unminified_chat_instruction_enforces_identity_rules():
    instructions = load_unminified_chat_instruction("telegram_bot")
    assert "Stay in the active persona in first person" in instructions
    assert "Treat that as stale style noise" in instructions
    assert "Keep pronouns consistent" in instructions
    assert "prefer explicit honesty over confident reconstruction" in instructions
    assert "potentially incomplete or reconstructed" in instructions
    assert (
        "do not replace an established he/him or she/her person with singular they/them"
        in instructions
    )


def test_build_context_summary_adds_memory_honesty_notice_when_memories_present() -> (
    None
):
    summary = _build_context_summary(
        {
            "memories": [
                "[SOUL recalled memory | 2026-04-20 | same chat] Alice loves jasmine tea."
            ]
        }
    )

    assert "[Memory honesty notice]" in summary
    assert "recalled internal records" in summary
    assert "acknowledge uncertainty instead of inventing a recollection" in summary
    assert (
        "Recalled memory from 2026-04-20 (same chat): Alice loves jasmine tea."
        in summary
    )
    assert "SOUL recalled memory" not in summary


def test_build_context_summary_humanizes_diary_entries() -> None:
    summary = _build_context_summary(
        {
            "history_recent": [
                "[diary 07/05/26:0335] summary: First memory. --- First memory. --- Second memory. | thought: Private note. --- Private note."
            ],
            "thoughts": ["[thought 07/05/26:0335] Private note. --- Private note."],
        }
    )

    assert "Diary entry from 07/05/26:0335: First memory. | Second memory." in summary
    assert "Thought from 07/05/26:0335: Private note." in summary
    assert "| thought:" not in summary


def test_build_live_system_instruction_enforces_identity_rules(monkeypatch):
    async def dummy_gather(message, ctx):
        return {"persona": "You are SynthA."}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    result = asyncio.run(build_live_system_instruction())

    assert "You are SynthA." in result
    assert "Stay fully inside the active persona in first person" in result
    assert "Keep participant pronouns consistent" in result
    assert (
        "never replace an established he/him or she/her person with singular they/them"
        in result
    )


def test_build_live_prompt_request_returns_live_mode(monkeypatch):
    from core.prompt_request import PromptRequest

    async def dummy_gather(message, ctx):
        return {"persona": "You are SynthA."}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    req = asyncio.run(build_live_prompt_request())
    assert isinstance(req, PromptRequest)
    assert req.mode == "live"
    assert "You are SynthA." in req.system_instruction


def test_build_json_prompt_demotes_persona_preferences_to_context_summary(monkeypatch):
    async def dummy_gather(message, ctx):
        return {
            "persona": "PERSONA IDENTITY:\nName: SynthA\nProfile: You are SynthA.",
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


def test_build_context_summary_keeps_exact_runtime_facts_implicit_by_default() -> None:
    summary = _build_context_summary(
        {
            "date": "2026-04-20",
            "time": "21:27",
            "time_of_day": "late evening",
            "location": "Sečovlje,Slovenia",
            "season": "Mid Spring",
            "day_of_week": "Monday",
        }
    )

    assert "[SYSTEM: REALITY ANCHOR]" in summary
    assert "- Current Date: Monday, April 20, 2026" in summary
    assert "- Current Time: 9:27 PM" in summary
    assert "- Season: Mid Spring" in summary
    assert "- Current Location: Sečovlje,Slovenia" in summary
    assert "Temporal Delta: It is now 2026" in summary


def test_build_context_summary_can_surface_exact_runtime_facts_when_requested() -> None:
    summary = _build_context_summary(
        {
            "date": "2026-04-20",
            "time": "21:27",
            "location": "Sečovlje,Slovenia",
            "season": "Mid Spring",
            "day_of_week": "Monday",
        },
        include_explicit_runtime_facts=True,
    )

    assert "[SYSTEM: REALITY ANCHOR]" in summary
    assert "- Current Date: Monday, April 20, 2026" in summary
    assert "- Current Time: 9:27 PM" in summary
    assert "- Season: Mid Spring" in summary
    assert "- Current Location: Sečovlje,Slovenia" in summary


def test_build_json_prompt_gates_exact_runtime_facts_by_current_turn(monkeypatch):
    async def dummy_gather(message, ctx):
        return {"location": "Sečovlje,Slovenia"}

    async def dummy_local_time_fields(message_date, interface_path=None):
        return {
            "local_date": "2026-04-20",
            "local_time": "21:27",
            "time_of_day": "late evening",
            "season": "Mid Spring",
            "day_of_week": "Monday",
        }

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)
    monkeypatch.setattr(
        "core.time_zone_utils.get_local_time_fields", dummy_local_time_fields
    )

    message = SimpleNamespace(
        chat_id=1,
        text="How are you feeling?",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime.now(timezone.utc),
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="telegram_bot"))
    summary = result["__prompt_request"].context_summary
    assert "[SYSTEM: REALITY ANCHOR]" in summary
    assert "- Current Date: Monday, April 20, 2026" in summary
    assert "- Current Time: 9:27 PM" in summary
    assert "- Current Location: Sečovlje,Slovenia" in summary

    message.text = "What time is it there?"
    explicit = asyncio.run(
        build_json_prompt(message, {}, interface_name="telegram_bot")
    )
    explicit_summary = explicit["__prompt_request"].context_summary
    assert "[SYSTEM: REALITY ANCHOR]" in explicit_summary
    assert "- Current Date: Monday, April 20, 2026" in explicit_summary
    assert "- Current Time: 9:27 PM" in explicit_summary
    assert "- Current Location: Sečovlje,Slovenia" in explicit_summary


def test_build_live_prompt_request_keeps_runtime_facts_ambient_by_default(monkeypatch):
    async def dummy_gather(message, ctx):
        return {
            "persona": "You are SynthA.",
            "date": "2026-04-20",
            "time": "21:27 CEST",
            "time_of_day": "late evening",
            "location": "Sečovlje,Slovenia",
        }

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    req = asyncio.run(
        build_live_prompt_request(message=SimpleNamespace(text="How are you feeling?"))
    )

    assert "Ambient runtime context:" in req.system_instruction
    assert "Current part of day: late evening." in req.system_instruction
    assert "Time: 21:27 CEST" not in req.system_instruction
    assert "Location: Sečovlje,Slovenia" not in req.system_instruction


def test_build_live_prompt_request_surfaces_exact_runtime_facts_when_requested(
    monkeypatch,
):
    async def dummy_gather(message, ctx):
        return {
            "persona": "You are SynthA.",
            "date": "2026-04-20",
            "time": "21:27 CEST",
            "location": "Sečovlje,Slovenia",
        }

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    req = asyncio.run(
        build_live_prompt_request(
            message=SimpleNamespace(text="What time is it there?")
        )
    )

    assert "Date: 2026-04-20" in req.system_instruction
    assert "Time: 21:27 CEST" in req.system_instruction
    assert "Location: Sečovlje,Slovenia" in req.system_instruction


def test_build_live_prompt_request_keeps_persona_preferences(monkeypatch):
    async def dummy_gather(message, ctx):
        return {
            "persona": "You are SynthA.",
            "persona_preferences": "Likes: tea\nDislikes: liars",
        }

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    req = asyncio.run(build_live_prompt_request())

    assert "You are SynthA." in req.system_instruction
    assert "Background preferences and interests:" in req.system_instruction
    assert "Likes: tea" in req.system_instruction
    assert "Dislikes: liars" in req.system_instruction


def test_build_live_system_instruction_matches_live_renderer(monkeypatch):
    from core.prompt_renderers import LiveRenderer

    async def dummy_gather(message, ctx):
        return {"persona": "You are SynthA."}

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


def test_build_json_prompt_derives_default_interface_action_scope(monkeypatch):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    from core.core_initializer import core_initializer

    monkeypatch.setattr(
        "core.core_initializer.INTERFACE_REGISTRY",
        {"telegram_bot": object(), "discord_bot": object()},
        raising=False,
    )

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
                "source": "message_plugin, telegram_bot",
            },
            "message_discord_bot": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Send Discord message.",
                "source": "message_plugin, discord_bot",
            },
            "update_diary_entry": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Replace diary entry content with a synthesised version (internal — triggered by the daily consolidation beat only).",
                "source": "ai_diary",
            },
            "promote_upload": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Promote a temporary animation upload into a target skin (admin only).",
                "source": "mate_engine",
            },
        }
    }

    try:
        message = SimpleNamespace(
            chat_id=1,
            text="hello",
            message_id=1,
            from_user=SimpleNamespace(full_name="user", username="user"),
            date=datetime.now(timezone.utc),
            interface_path="telegram_bot/1",
        )

        result = asyncio.run(
            build_json_prompt(message, {}, interface_name="telegram_bot")
        )

        assert sorted(result["actions"].keys()) == [
            "create_personal_diary_entry",
            "message_telegram_bot",
        ]

        pr = result["__prompt_request"]
        tool_names = {
            getattr(tool, "name", None)
            for tool in pr.tool_declarations
            if getattr(tool, "name", None)
        }
        assert tool_names == {"create_personal_diary_entry", "message_telegram_bot"}
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


def test_prompt_request_attaches_for_grillo_observer_with_string_message_id(
    monkeypatch,
):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id="123456",
        text="[G.R.I.L.L.O. CHAT OBSERVER] check in",
        message_id="grillo_observer_0",
        from_user=SimpleNamespace(full_name="grillo", username="grillo"),
        date=datetime.now(timezone.utc),
        interface_path="telegram_bot/123456",
    )

    result = asyncio.run(
        build_json_prompt(
            message,
            {"grillo_beat": True, "beat_type": "observer"},
            interface_name="telegram_bot",
        )
    )

    assert "__prompt_request" in result
    pr = result["__prompt_request"]
    assert pr.current_text == "[G.R.I.L.L.O. CHAT OBSERVER] check in"
    assert pr.runtime_ctx.interface_path == "telegram_bot/123456"
    assert pr.runtime_ctx.message_id is None


def test_build_pr_attachments_extracts_pdf_text(monkeypatch):
    from core.prompt_engine import _build_pr_attachments

    monkeypatch.setattr(
        "core.prompt_engine._extract_attachment_text_preview",
        lambda mime_type, filename, data: ("[Page 1]\nMotor spec page", False),
    )

    attachments = _build_pr_attachments(
        None,
        [
            {
                "mime_type": "application/pdf",
                "filename": "manual.pdf",
                "data": base64.b64encode(b"%PDF-1.4 fake").decode("ascii"),
            }
        ],
    )

    assert len(attachments) == 1
    assert (
        attachments[0].media_metadata["extracted_text"] == "[Page 1]\nMotor spec page"
    )


def test_build_pr_attachments_extracts_pdf_page_images_when_text_missing(monkeypatch):
    from core.prompt_engine import _build_pr_attachments

    monkeypatch.setattr(
        "core.prompt_engine._extract_attachment_text_preview",
        lambda mime_type, filename, data: (None, False),
    )
    monkeypatch.setattr(
        "core.prompt_engine._extract_pdf_page_images",
        lambda filename, data: (
            [
                {
                    "mime_type": "image/png",
                    "data": "BBBB",
                    "filename": "manual_page_1.png",
                }
            ],
            False,
        ),
    )

    attachments = _build_pr_attachments(
        None,
        [
            {
                "mime_type": "application/pdf",
                "filename": "manual.pdf",
                "data": base64.b64encode(b"%PDF-1.4 fake").decode("ascii"),
            }
        ],
    )

    assert len(attachments) == 1
    assert (
        attachments[0].media_metadata["page_images"][0]["filename"]
        == "manual_page_1.png"
    )


def _build_acroform_pdf() -> bytes:
    """Build a minimal one-page PDF with a filled AcroForm text field."""
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    page = writer.pages[0]

    field = DictionaryObject()
    field.update(
        {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("CharacterName"),
            NameObject("/V"): TextStringObject("Rekku the Bard"),
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Rect"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(100), NumberObject(20)]
            ),
        }
    )
    ref = writer._add_object(field)
    page[NameObject("/Annots")] = ArrayObject([ref])

    acroform = DictionaryObject()
    acroform.update(
        {
            NameObject("/Fields"): ArrayObject([ref]),
            NameObject("/NeedAppearances"): BooleanObject(True),
        }
    )
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(acroform)

    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_extract_attachment_text_preview_reads_acroform_fields():
    from core.prompt_engine import _extract_attachment_text_preview

    pdf_bytes = _build_acroform_pdf()

    text, _truncated = _extract_attachment_text_preview(
        mime_type="application/pdf",
        filename="sheet.pdf",
        data=pdf_bytes,
    )

    assert text is not None
    assert "=== Form fields ===" in text
    assert "CharacterName: Rekku the Bard" in text


def test_rasterize_pdf_pages_renders_vector_only_pdf():
    from core.prompt_engine import _rasterize_pdf_pages

    # A blank PDF page has no extractable text and no embedded raster image;
    # rasterization is the only way to make it visible to a vision model.
    pdf_bytes = _build_acroform_pdf()

    images, _truncated = _rasterize_pdf_pages(pdf_bytes, "sheet", "sheet.pdf")

    assert len(images) == 1
    assert images[0]["mime_type"] == "image/png"
    assert images[0]["filename"] == "sheet_page_1.png"
    # Rendered PNG payload must be non-empty base64.
    assert len(images[0]["data"]) > 0


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
        turns = self._call(lines, {"syntha"})
        assert len(turns) == 1
        assert turns[0].role == "assistant"
        assert "Hello from the AI" in turns[0].content

    def test_synth_name_becomes_assistant(self) -> None:
        lines = ['[13/04/26:0900] SynthA: "I am SynthA"']
        turns = self._call(lines, {"syntha"})
        assert len(turns) == 1
        assert turns[0].role == "assistant"

    def test_user_sender_becomes_user(self) -> None:
        lines = ['[13/04/26:0924] Alice: "Hey there"']
        turns = self._call(lines, {"syntha"})
        assert len(turns) == 1
        assert turns[0].role == "user"
        assert "Hey there" in turns[0].content

    def test_mixed_conversation_roles(self) -> None:
        lines = [
            '[13/04/26:0924] Alice: "How are you?"',
            '[13/04/26:0925] self: "I am great!"',
            '[13/04/26:0926] Alice: "Good to hear"',
        ]
        turns = self._call(lines, {"syntha"})
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
            '[13/04/26:0924] Alice: "valid line"',
        ]
        turns = self._call(lines, {"synth"})
        assert len(turns) == 1
        assert turns[0].role == "user"

    def test_alias_detected_as_assistant(self) -> None:
        lines = ['[13/04/26:0900] Toobs: "My alias"']
        turns = self._call(lines, {"syntha", "toobs"})
        assert len(turns) == 1
        assert turns[0].role == "assistant"

    def test_drops_leading_assistant_run_when_user_appears_later(self) -> None:
        lines = [
            '[13/04/26:0900] self: "Older outreach"',
            '[13/04/26:0901] self: "Another outreach"',
            '[13/04/26:0902] Alice: "Replying now"',
            '[13/04/26:0903] self: "Thanks"',
        ]

        turns = self._call(lines, {"syntha"})

        assert [turn.role for turn in turns] == ["user", "assistant"]
        assert turns[0].content == "Replying now"
        assert turns[1].content == "Thanks"

    def test_coalesces_consecutive_same_role_turns(self) -> None:
        lines = [
            '[13/04/26:0924] Alice: "First part"',
            '[13/04/26:0925] Alice: "Second part"',
            '[13/04/26:0926] self: "First answer"',
            '[13/04/26:0927] self: "Second answer"',
        ]

        turns = self._call(lines, {"syntha"})

        assert [turn.role for turn in turns] == ["user", "assistant"]
        assert turns[0].content == "First part\n\nSecond part"
        assert turns[1].content == "First answer\n\nSecond answer"

    def test_peer_synth_sender_tagged_not_collapsed_into_anonymous_user(
        self, monkeypatch
    ) -> None:
        """A peer SyntH's message must stay attributable, not silently look
        like it came from the human -- see AGENTS.md 'Telegram bots can't
        see each other's messages...' entry."""
        monkeypatch.setattr(
            "core.peer_policy.get_peer_names", lambda: {8000000001: "SynthB"}
        )
        lines = ['[13/04/26:0924] SynthB: "I bounce on my toes, so excited!"']

        turns = self._call(lines, {"syntha"})

        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].content == "[SynthB]: I bounce on my toes, so excited!"

    def test_human_sender_not_tagged_even_with_peer_mode_configured(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "core.peer_policy.get_peer_names", lambda: {8000000001: "SynthB"}
        )
        lines = ['[13/04/26:0924] Alice: "Hey there"']

        turns = self._call(lines, {"syntha"})

        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].content == "Hey there"

    def test_peer_lookup_failure_falls_back_to_plain_user(self, monkeypatch) -> None:
        def _raise():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr("core.peer_policy.get_peer_names", _raise)
        lines = ['[13/04/26:0924] SynthB: "Still works without peer config"']

        turns = self._call(lines, {"syntha"})

        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].content == "Still works without peer config"

    def test_peer_and_human_turns_not_coalesced_together(self, monkeypatch) -> None:
        """Reproduces the Langfuse trace bug (ff4e648f-fb13-47f9-b7ca-85828d987832,
        blocks 1 and 3): a human line sandwiched between two peer lines must not
        get squashed into one "user" blob -- each stays a standalone turn so the
        peer attribution and the human's real words are never blended."""
        monkeypatch.setattr("core.peer_policy.get_peer_names", lambda: {99: "SynthA"})
        lines = [
            '[02/07/26:2045] SynthA: "peer line one"',
            '[02/07/26:2046] Alice: "genuine human line"',
            '[02/07/26:2047] SynthA: "peer line two"',
        ]

        turns = self._call(lines, {"synth"})

        assert [turn.role for turn in turns] == ["user", "user", "user"]
        assert turns[0].content == "[SynthA]: peer line one"
        assert turns[1].content == "genuine human line"
        assert turns[2].content == "[SynthA]: peer line two"

    def test_consecutive_peer_turns_still_coalesce(self, monkeypatch) -> None:
        monkeypatch.setattr("core.peer_policy.get_peer_names", lambda: {99: "SynthA"})
        lines = [
            '[02/07/26:2045] SynthA: "peer line one"',
            '[02/07/26:2046] SynthA: "peer line two"',
        ]

        turns = self._call(lines, {"synth"})

        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].content == "[SynthA]: peer line one\n\n[SynthA]: peer line two"

    def test_consecutive_human_turns_still_coalesce_with_peer_configured(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr("core.peer_policy.get_peer_names", lambda: {99: "SynthA"})
        lines = [
            '[02/07/26:2045] Alice: "First part"',
            '[02/07/26:2046] Alice: "Second part"',
        ]

        turns = self._call(lines, {"synth"})

        assert len(turns) == 1
        assert turns[0].role == "user"
        assert turns[0].content == "First part\n\nSecond part"
