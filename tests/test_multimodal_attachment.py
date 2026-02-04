# tests/test_multimodal_attachment.py
"""Tests for multimodal attachment extraction."""

import base64
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.multimodal_attachment import (
    get_mime_type,
    is_supported_type,
    encode_bytes_to_base64,
    extract_multimodal_from_telegram,
    extract_multimodal_from_discord,
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

    def test_unsupported_types(self):
        """Test that unsupported types return False."""
        assert not is_supported_type("video/mp4")
        assert not is_supported_type("application/octet-stream")


class TestBase64Encoding:
    """Tests for base64 encoding."""

    def test_encode_bytes_to_base64(self):
        """Test encoding bytes to base64."""
        data = b"Hello, World!"
        encoded = encode_bytes_to_base64(data)
        assert encoded == base64.b64encode(data).decode("utf-8")


class TestTelegramExtraction:
    """Tests for Telegram multimodal extraction."""

    @pytest.mark.asyncio
    async def test_extract_photo_from_telegram(self):
        """Test extracting a photo from a Telegram message."""
        # Mock the bot
        mock_bot = AsyncMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake_image_data")
        )
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        # Mock the message with a photo
        mock_photo = MagicMock()
        mock_photo.file_id = "test_file_id"
        mock_photo.file_unique_id = "test_unique_id"

        mock_message = MagicMock()
        mock_message.photo = [mock_photo]
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video_note = None
        mock_message.sticker = None
        mock_message.chat = MagicMock(id=123, type="private")

        # Extract attachments
        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "image/jpeg"
        assert attachments[0]["filename"].startswith("photo_")
        assert attachments[0]["data"] == encode_bytes_to_base64(b"fake_image_data")

    @pytest.mark.asyncio
    async def test_extract_document_from_telegram(self):
        """Test extracting a document from a Telegram message."""
        mock_bot = AsyncMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake_pdf_data")
        )
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        mock_doc = MagicMock()
        mock_doc.file_id = "test_doc_id"
        mock_doc.file_unique_id = "test_unique_doc_id"
        mock_doc.file_name = "document.pdf"
        mock_doc.mime_type = "application/pdf"

        mock_message = MagicMock()
        mock_message.photo = None
        mock_message.document = mock_doc
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video_note = None
        mock_message.sticker = None
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "application/pdf"
        assert attachments[0]["filename"] == "document.pdf"

    @pytest.mark.asyncio
    async def test_skip_non_whitelisted_group(self):
        """Test that attachments are skipped for non-whitelisted groups."""
        mock_bot = AsyncMock()
        mock_photo = MagicMock()
        mock_photo.file_id = "test_file_id"
        mock_photo.file_unique_id = "test_unique_id"

        mock_message = MagicMock()
        mock_message.photo = [mock_photo]
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video_note = None
        mock_message.sticker = None
        mock_message.chat = MagicMock(id=456, type="group")

        # Mock image processor that denies access
        mock_processor = MagicMock()
        mock_processor.should_process_image = MagicMock(return_value=False)

        attachments = await extract_multimodal_from_telegram(
            mock_bot, mock_message, image_processor=mock_processor
        )

        assert len(attachments) == 0


class TestDiscordExtraction:
    """Tests for Discord multimodal extraction."""

    @pytest.mark.asyncio
    async def test_extract_attachment_from_discord(self):
        """Test extracting an attachment from a Discord message."""
        mock_attachment = AsyncMock()
        mock_attachment.content_type = "image/png"
        mock_attachment.filename = "image.png"
        mock_attachment.read = AsyncMock(return_value=b"fake_png_data")

        mock_message = MagicMock()
        mock_message.attachments = [mock_attachment]
        mock_message.embeds = []
        mock_message.guild = None  # Private channel

        attachments = await extract_multimodal_from_discord(mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "image/png"
        assert attachments[0]["filename"] == "image.png"
        assert attachments[0]["data"] == encode_bytes_to_base64(b"fake_png_data")

    @pytest.mark.asyncio
    async def test_skip_unsupported_attachment(self):
        """Test that unsupported attachments are skipped."""
        mock_attachment = AsyncMock()
        mock_attachment.content_type = "video/mp4"
        mock_attachment.filename = "video.mp4"

        mock_message = MagicMock()
        mock_message.attachments = [mock_attachment]
        mock_message.embeds = []
        mock_message.guild = None

        attachments = await extract_multimodal_from_discord(mock_message)

        assert len(attachments) == 0

    @pytest.mark.asyncio
    async def test_skip_non_whitelisted_guild_channel(self):
        """Test that attachments are skipped for non-whitelisted guild channels."""
        mock_attachment = AsyncMock()
        mock_attachment.content_type = "image/png"
        mock_attachment.filename = "image.png"

        mock_message = MagicMock()
        mock_message.attachments = [mock_attachment]
        mock_message.embeds = []
        mock_message.guild = MagicMock()  # Has a guild (server)
        mock_message.channel = MagicMock(id=789)

        # Mock image processor that denies access
        mock_processor = MagicMock()
        mock_processor.should_process_image = MagicMock(return_value=False)

        attachments = await extract_multimodal_from_discord(
            mock_message, image_processor=mock_processor
        )

        assert len(attachments) == 0
