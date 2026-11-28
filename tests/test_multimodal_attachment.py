# tests/test_multimodal_attachment.py
"""Tests for multimodal attachment extraction."""

import base64

from core.multimodal_attachment import (
    get_mime_type,
    is_supported_type,
    encode_bytes_to_base64,
)


class TestMimeType:
    """Tests for MIME type detection."""

    def test_get_mime_type_from_extension(self):
        """Test MIME type detection from file extension."""
        assert get_mime_type("/path/to/image.jpg") == "image/jpeg"
        assert get_mime_type("/path/to/image.png") == "image/png"
        assert get_mime_type("/path/to/doc.pdf") == "application/pdf"
        assert get_mime_type("/path/to/audio.mp3") == "audio/mpeg"

    def test_get_mime_type_from_filename(self):
        """Test MIME type detection from filename only."""
        assert get_mime_type(None, "photo.jpg") == "image/jpeg"
        assert get_mime_type(None, "document.pdf") == "application/pdf"

    def test_get_mime_type_fallback(self):
        """Test MIME type fallback for unknown extensions."""
        assert get_mime_type("/path/to/file.xyz") == "application/octet-stream"


class TestSupportedTypes:
    """Tests for supported type checking."""

    def test_supported_image_types(self):
        """Test that common image types are supported."""
        assert is_supported_type("image/jpeg")
        assert is_supported_type("image/png")
        assert is_supported_type("image/gif")
        assert is_supported_type("image/webp")

    def test_supported_audio_types(self):
        """Test that common audio types are supported."""
        assert is_supported_type("audio/mpeg")
        assert is_supported_type("audio/wav")
        assert is_supported_type("audio/ogg")

    def test_supported_document_types(self):
        """Test that common document types are supported."""
        assert is_supported_type("application/pdf")
        assert is_supported_type("text/plain")
        assert is_supported_type("application/json")

    def test_supported_video_types(self):
        """Test that common video types are supported."""
        assert is_supported_type("video/mp4")
        assert is_supported_type("video/mpeg")
        assert is_supported_type("video/webm")
        assert is_supported_type("video/quicktime")
        assert is_supported_type("video/3gpp")

    def test_unsupported_types(self):
        """Test that unsupported types return False."""
        assert not is_supported_type("application/octet-stream")
        assert not is_supported_type("application/zip")


class TestBase64Encoding:
    """Tests for base64 encoding."""

    def test_encode_bytes_to_base64(self):
        """Test encoding bytes to base64."""
        data = b"Hello, World!"
        encoded = encode_bytes_to_base64(data)
        assert encoded == base64.b64encode(data).decode("utf-8")
