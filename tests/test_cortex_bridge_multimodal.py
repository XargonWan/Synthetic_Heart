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
