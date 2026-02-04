# tests/test_gemini_multimodal.py
"""Tests for Gemini API multimodal functionality."""

import base64


class TestGeminiMultimodal:
    """Tests for Gemini API multimodal support."""

    def test_get_mime_type(self):
        """Test MIME type detection."""
        from llm_engines.gemini_api import GeminiAPIPlugin

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
        from llm_engines.gemini_api import GeminiAPIPlugin

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
        from llm_engines.gemini_api import GeminiAPIPlugin

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
        from llm_engines.gemini_api import GeminiAPIPlugin

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
        from llm_engines.gemini_api import GeminiAPIPlugin

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
        from llm_engines.gemini_api import GeminiAPIPlugin

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
        from llm_engines.gemini_api import GeminiAPIPlugin

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
        from llm_engines.gemini_api import GeminiAPIPlugin

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
