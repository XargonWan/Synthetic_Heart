# tests/test_gemini_multimodal.py
"""Tests for Gemini API multimodal functionality."""

import base64


class TestGeminiMultimodal:
    """Tests for Gemini API multimodal support."""

    def test_get_mime_type(self):
        """Test MIME type detection."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        # Test common image types
        assert plugin._get_mime_type("/path/to/image.jpg") == "image/jpeg"
        assert plugin._get_mime_type("/path/to/image.png") == "image/png"
        assert plugin._get_mime_type("/path/to/image.gif") == "image/gif"

        # Test audio types
        assert plugin._get_mime_type("/path/to/audio.mp3") == "audio/mpeg"
        # Note: mimetypes may return audio/x-wav on some systems
        assert plugin._get_mime_type("/path/to/audio.wav") in (
            "audio/wav",
            "audio/x-wav",
        )

        # Test document types
        assert plugin._get_mime_type("/path/to/doc.pdf") == "application/pdf"
        assert plugin._get_mime_type("/path/to/file.json") == "application/json"

    def test_is_supported_multimodal_type(self):
        """Test supported multimodal type checking."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        # Supported types
        assert plugin._is_supported_multimodal_type("image/jpeg")
        assert plugin._is_supported_multimodal_type("audio/mpeg")
        assert plugin._is_supported_multimodal_type("application/pdf")
        assert plugin._is_supported_multimodal_type("video/mp4")
        assert plugin._is_supported_multimodal_type("video/webm")

        # Unsupported types
        assert not plugin._is_supported_multimodal_type("application/octet-stream")
        assert not plugin._is_supported_multimodal_type("application/zip")

    def test_extract_multimodal_parts_from_dict(self):
        """Test extracting multimodal parts from a prompt dict."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        # Test with attachments in prompt
        fake_base64 = base64.b64encode(b"fake_image_data").decode("utf-8")
        prompt = {
            "input": {
                "message": "What's in this image?",
                "attachments": [
                    {
                        "mime_type": "image/jpeg",
                        "data": fake_base64,
                        "filename": "test.jpg",
                    }
                ],
            }
        }

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 1
        assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
        assert parts[0]["inline_data"]["data"] == fake_base64

    def test_extract_multimodal_parts_empty_for_text_only(self):
        """Test that no parts are extracted for text-only prompts."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        prompt = {
            "input": {
                "message": "Hello, how are you?",
            }
        }

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 0

    def test_extract_multimodal_parts_top_level_attachments(self):
        """Test extracting multimodal parts from top-level attachments."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_base64 = base64.b64encode(b"fake_pdf_data").decode("utf-8")
        prompt = {
            "attachments": [
                {
                    "mime_type": "application/pdf",
                    "data": fake_base64,
                    "filename": "document.pdf",
                }
            ],
            "input": {
                "message": "Summarize this document",
            },
        }

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 1
        assert parts[0]["inline_data"]["mime_type"] == "application/pdf"

    def test_extract_multimodal_parts_video(self):
        """Test extracting video attachments."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_base64 = base64.b64encode(b"fake_video_data").decode("utf-8")
        prompt = {
            "attachments": [
                {
                    "mime_type": "video/mp4",
                    "data": fake_base64,
                    "filename": "video.mp4",
                }
            ],
        }

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 1
        assert parts[0]["inline_data"]["mime_type"] == "video/mp4"

    def test_extract_multimodal_parts_skips_unsupported(self):
        """Test that unsupported MIME types are skipped."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_base64 = base64.b64encode(b"fake_zip_data").decode("utf-8")
        prompt = {
            "attachments": [
                {
                    "mime_type": "application/zip",
                    "data": fake_base64,
                    "filename": "archive.zip",
                }
            ],
        }

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 0

    def test_extract_multimodal_parts_multiple_attachments(self):
        """Test extracting multiple attachments."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_image = base64.b64encode(b"fake_image").decode("utf-8")
        fake_audio = base64.b64encode(b"fake_audio").decode("utf-8")

        prompt = {
            "input": {
                "attachments": [
                    {"mime_type": "image/jpeg", "data": fake_image},
                    {"mime_type": "audio/mpeg", "data": fake_audio},
                ]
            }
        }

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 2
        assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
        assert parts[1]["inline_data"]["mime_type"] == "audio/mpeg"

    def test_copy_and_redact_data_attachments(self):
        """Test that attachment data is properly redacted."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_data = "a" * 10000  # Large base64 string
        prompt = {
            "input": {
                "message": "What's in this?",
                "attachments": [
                    {
                        "mime_type": "image/jpeg",
                        "data": fake_data,
                        "filename": "test.jpg",
                    },
                    {
                        "mime_type": "video/mp4",
                        "base64": fake_data,
                        "filename": "video.mp4",
                    },
                ],
            }
        }

        redacted = plugin._copy_and_redact_data(prompt)

        # Original should be unchanged
        assert prompt["input"]["attachments"][0]["data"] == fake_data
        assert prompt["input"]["attachments"][1]["base64"] == fake_data

        # Redacted should have placeholders
        assert redacted["input"]["attachments"][0]["data"] == "<redacted: 10000 chars>"
        assert (
            redacted["input"]["attachments"][1]["base64"] == "<redacted: 10000 chars>"
        )

    def test_copy_and_redact_data_nested(self):
        """Test that nested attachment data is properly redacted."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_data = "b" * 50000
        prompt = {
            "context": {
                "nested": {
                    "deeply": {
                        "attachments": [
                            {"mime_type": "application/pdf", "data": fake_data}
                        ]
                    }
                }
            }
        }

        redacted = plugin._copy_and_redact_data(prompt)

        # Redacted should have placeholder even for deeply nested
        assert (
            redacted["context"]["nested"]["deeply"]["attachments"][0]["data"]
            == "<redacted: 50000 chars>"
        )

    def test_copy_and_redact_data_legacy_keys(self):
        """Test that legacy keys (images, audio, videos, documents) are redacted."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_data = "c" * 20000
        prompt = {
            "images": [{"data": fake_data}],
            "audio": [{"base64": fake_data}],
            "videos": [{"data": fake_data}],
            "documents": [{"data": fake_data}],
        }

        redacted = plugin._copy_and_redact_data(prompt)

        assert redacted["images"][0]["data"] == "<redacted: 20000 chars>"
        assert redacted["audio"][0]["base64"] == "<redacted: 20000 chars>"
        assert redacted["videos"][0]["data"] == "<redacted: 20000 chars>"
        assert redacted["documents"][0]["data"] == "<redacted: 20000 chars>"

    def test_extract_multimodal_parts_nested(self):
        """Test extracting multimodal parts from deeply nested prompt."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_base64 = base64.b64encode(b"deep_nested_data").decode("utf-8")
        prompt = {
            "context": {
                "payload": {
                    "attachments": [{"mime_type": "audio/mpeg", "data": fake_base64}]
                }
            }
        }

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 1
        assert parts[0]["inline_data"]["mime_type"] == "audio/mpeg"
        assert parts[0]["inline_data"]["data"] == fake_base64

    def test_extract_multimodal_parts_legacy_videos(self):
        """Test extracting video attachments from legacy 'videos' key."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        fake_base64 = base64.b64encode(b"video_data").decode("utf-8")
        prompt = {"videos": [{"mime_type": "video/mp4", "data": fake_base64}]}

        parts = plugin._extract_multimodal_parts(prompt)

        assert len(parts) == 1
        assert parts[0]["inline_data"]["mime_type"] == "video/mp4"

    def test_extract_multimodal_parts_ignores_schema(self):
        """Test that schema definitions with multimodal keys are ignored."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        # Schema-like structure that shouldn't be treated as an attachment
        prompt = {
            "actions": {
                "audio_telegram_bot": {
                    "schema": {
                        "properties": {
                            "audio": {"type": "string", "description": "Field: audio"}
                        }
                    }
                }
            }
        }

        parts = plugin._extract_multimodal_parts(prompt)

        # Should extract nothing
        assert len(parts) == 0

    def test_copy_and_redact_data_ignores_schema(self):
        """Test that schema definitions are not redacted even if they have 'data' key."""
        from cortex.llm_provider.gemini_api import GeminiAPIPlugin

        plugin = GeminiAPIPlugin()

        prompt = {
            "actions": {
                "some_action": {
                    "schema": {
                        "properties": {
                            "data": {
                                "type": "string",
                                "description": "some data description",
                            }
                        }
                    }
                }
            }
        }

        redacted = plugin._copy_and_redact_data(prompt)

        # The description should NOT be redacted because it doesn't look like an attachment
        description = redacted["actions"]["some_action"]["schema"]["properties"][
            "data"
        ]["description"]
        assert description == "some data description"
        assert "<redacted" not in description
