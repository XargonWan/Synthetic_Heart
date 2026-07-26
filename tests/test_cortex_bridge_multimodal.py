"""Tests for multimodal extraction in the cortex_bridge → gemini_adapter path."""

from __future__ import annotations


from core.external_endpoints.bridges.cortex_bridge import (
    _extract_attachments_and_redact,
)


# ---------------------------------------------------------------------------
# _extract_attachments_and_redact
# ---------------------------------------------------------------------------


class TestExtractAttachmentsAndRedact:
    """Unit tests for the bridge-level multimodal extraction helper."""

    def test_no_attachments_returns_empty(self) -> None:
        prompt = {"input": {"text": "hello"}, "context": {"mood": "curious"}}
        redacted, parts = _extract_attachments_and_redact(prompt)
        assert parts == []
        # Redacted copy should be structurally identical to the original
        assert redacted == prompt

    def test_extracts_image_attachment(self) -> None:
        b64 = "A" * 1000  # fake base64 longer than 256-char threshold
        prompt = {
            "input": {
                "text": "what is this?",
                "attachments": [
                    {"mime_type": "image/jpeg", "data": b64, "filename": "photo.jpg"}
                ],
            },
        }
        redacted, parts = _extract_attachments_and_redact(prompt)

        # Should extract exactly one part
        assert len(parts) == 1
        assert parts[0]["mime_type"] == "image/jpeg"
        assert parts[0]["data"] == b64

        # Redacted copy should have placeholder instead of raw base64
        att = redacted["input"]["attachments"][0]
        assert att["data"].startswith("<redacted:")
        assert att["mime_type"] == "image/jpeg"

    def test_extracts_multiple_attachments(self) -> None:
        b64_img = "I" * 500
        b64_audio = "A" * 800
        prompt = {
            "input": {
                "attachments": [
                    {"mime_type": "image/png", "data": b64_img},
                    {"mime_type": "audio/wav", "data": b64_audio},
                ],
            },
        }
        _, parts = _extract_attachments_and_redact(prompt)
        assert len(parts) == 2
        mimes = {p["mime_type"] for p in parts}
        assert mimes == {"image/png", "audio/wav"}

    def test_handles_nested_attachments(self) -> None:
        b64 = "X" * 400
        prompt = {
            "input": {
                "payload": {"attachments": [{"mime_type": "image/webp", "data": b64}]}
            }
        }
        _, parts = _extract_attachments_and_redact(prompt)
        assert len(parts) == 1
        assert parts[0]["mime_type"] == "image/webp"

    def test_skips_invalid_short_data_fields(self) -> None:
        """Short non-base64 strings are not treated as binary attachments."""
        prompt = {
            "input": {
                "attachments": [{"mime_type": "image/jpeg", "data": "short"}],
            },
        }
        _, parts = _extract_attachments_and_redact(prompt)
        assert parts == []

    def test_extracts_short_valid_base64_data_fields(self) -> None:
        prompt = {
            "input": {
                "attachments": [{"mime_type": "image/jpeg", "data": "YWJjZA=="}],
            },
        }
        redacted, parts = _extract_attachments_and_redact(prompt)

        assert len(parts) == 1
        assert parts[0]["mime_type"] == "image/jpeg"
        assert parts[0]["data"] == "YWJjZA=="
        assert redacted["input"]["attachments"][0]["data"] == "<redacted: 8 chars>"

    def test_skips_schema_subtrees(self) -> None:
        """Should not recurse into 'actions' / 'available_actions' / 'schema'."""
        b64 = "S" * 500
        prompt = {
            "available_actions": {
                "some_action": {
                    "attachments": [{"mime_type": "image/png", "data": b64}]
                }
            }
        }
        _, parts = _extract_attachments_and_redact(prompt)
        assert parts == []

    def test_handles_base64_key_variant(self) -> None:
        b64 = "B" * 600
        prompt = {"input": {"attachments": [{"mime_type": "audio/ogg", "base64": b64}]}}
        _, parts = _extract_attachments_and_redact(prompt)
        assert len(parts) == 1
        assert parts[0]["data"] == b64

    def test_does_not_mutate_original(self) -> None:
        b64 = "D" * 500
        prompt = {"input": {"attachments": [{"mime_type": "image/jpeg", "data": b64}]}}
        _extract_attachments_and_redact(prompt)
        # Original must be untouched
        assert prompt["input"]["attachments"][0]["data"] == b64


# ---------------------------------------------------------------------------
# _messages_to_gemini — multimodal content parts
# ---------------------------------------------------------------------------


class TestMessagesToGeminiMultimodal:
    """Verify _messages_to_gemini handles inline_data and image_url parts."""

    @staticmethod
    def _call(messages: list[dict]) -> tuple[str, list[dict]]:
        from core.external_endpoints.adapters.gemini_adapter import (
            _messages_to_gemini,
        )

        return _messages_to_gemini(messages)

    def test_text_only_message(self) -> None:
        sys, contents = self._call([{"role": "user", "content": "hello"}])
        assert sys == ""
        assert len(contents) == 1
        assert contents[0]["parts"] == [{"text": "hello"}]

    def test_inline_data_part(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {
                        "type": "inline_data",
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": "AAAA",
                        },
                    },
                ],
            }
        ]
        _, contents = self._call(msgs)
        assert len(contents) == 1
        parts = contents[0]["parts"]
        assert len(parts) == 2
        assert parts[0] == {"text": "describe this"}
        assert parts[1] == {"inline_data": {"mime_type": "image/jpeg", "data": "AAAA"}}

    def test_image_url_data_uri(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,iVBOR"},
                    },
                ],
            }
        ]
        _, contents = self._call(msgs)
        parts = contents[0]["parts"]
        assert len(parts) == 2
        assert parts[1] == {"inline_data": {"mime_type": "image/png", "data": "iVBOR"}}

    def test_system_instruction_extracted(self) -> None:
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "hi"},
        ]
        sys, contents = self._call(msgs)
        assert sys == "You are helpful"
        assert len(contents) == 1

    def test_empty_parts_list_skipped(self) -> None:
        """Content list with no recognised item types produces no content entry."""
        msgs = [
            {
                "role": "user",
                "content": [{"type": "unknown", "value": "???"}],
            }
        ]
        _, contents = self._call(msgs)
        assert contents == []


# ---------------------------------------------------------------------------
# _clamp_messages_to_char_budget — downstream payload hard cap
# ---------------------------------------------------------------------------


import types  # noqa: E402

from core.external_endpoints.bridges.cortex_bridge import (  # noqa: E402
    ExternalCortexEngine,
)


def _make_engine(extra_config: dict | None = None) -> ExternalCortexEngine:
    """Build an ExternalCortexEngine over minimal endpoint/adapter stubs."""
    endpoint = types.SimpleNamespace(
        name="stub-endpoint",
        display_label="Stub",
        extra_config=extra_config or {},
        default_model="stub-model",
        available_models=[],
    )
    adapter = types.SimpleNamespace(_engine_label="")
    return ExternalCortexEngine(endpoint, adapter)  # type: ignore[arg-type]


class TestMessageContentLen:
    def test_string_content(self) -> None:
        assert ExternalCortexEngine._message_content_len("hello") == 5

    def test_multipart_counts_only_text(self) -> None:
        content = [
            {"type": "text", "text": "abc"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "de"},
        ]
        assert ExternalCortexEngine._message_content_len(content) == 5

    def test_non_string_fallback(self) -> None:
        assert ExternalCortexEngine._message_content_len(123) == 3


class TestTruncateMessageContent:
    def test_zero_remove_is_noop(self) -> None:
        assert ExternalCortexEngine._truncate_message_content("abc", 0) == "abc"

    def test_string_trimmed_with_marker(self) -> None:
        text = "x" * 100
        out = ExternalCortexEngine._truncate_message_content(text, 40)
        assert isinstance(out, str)
        assert out.endswith("[...context trimmed to fit the model's input budget...]")
        assert len(out) < len(text)

    def test_multipart_image_preserved(self) -> None:
        content = [
            {"type": "text", "text": "y" * 100},
            {"type": "image_url", "image_url": {"url": "keep-me"}},
        ]
        out = ExternalCortexEngine._truncate_message_content(content, 30)
        assert isinstance(out, list)
        # Image part is untouched
        assert any(p.get("type") == "image_url" for p in out)
        img = next(p for p in out if p.get("type") == "image_url")
        assert img["image_url"]["url"] == "keep-me"


class TestClampMessagesToCharBudget:
    def test_under_budget_unchanged(self) -> None:
        engine = _make_engine({"downstream_char_budget": 1000})
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "body"},
        ]
        out = engine._clamp_messages_to_char_budget(msgs)
        assert out[1]["content"] == "body"

    def test_over_budget_trims_user_preserves_system(self) -> None:
        engine = _make_engine({"downstream_char_budget": 100})
        system_text = "S" * 50
        user_text = "U" * 400
        msgs = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]
        out = engine._clamp_messages_to_char_budget(msgs)
        # System is preserved verbatim (contains the action catalog).
        assert out[0]["content"] == system_text
        # User body is shortened.
        assert len(out[1]["content"]) < len(user_text)
        # Total now within budget.
        total = sum(
            ExternalCortexEngine._message_content_len(m["content"]) for m in out
        )
        assert total <= 100

    def test_system_only_over_budget_left_as_is(self) -> None:
        engine = _make_engine({"downstream_char_budget": 10})
        msgs = [{"role": "system", "content": "S" * 100}]
        out = engine._clamp_messages_to_char_budget(msgs)
        # Nothing trimmable besides system → sent as-is (never drop the catalog).
        assert out[0]["content"] == "S" * 100

    def test_disabled_when_budget_non_positive(self) -> None:
        engine = _make_engine({"downstream_char_budget": 0})
        msgs = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "U" * 5000},
        ]
        out = engine._clamp_messages_to_char_budget(msgs)
        assert len(out[1]["content"]) == 5000
