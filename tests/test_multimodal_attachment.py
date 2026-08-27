# tests/test_multimodal_attachment.py
"""Tests for multimodal attachment extraction."""

import base64
from types import SimpleNamespace
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
        mock_message.video = None
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
    async def test_extract_video_from_telegram(self):
        """Test extracting a video from a Telegram message."""
        mock_bot = AsyncMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake_video_data")
        )
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        mock_video = MagicMock()
        mock_video.file_id = "test_video_id"
        mock_video.file_unique_id = "test_unique_video_id"
        mock_video.file_name = "video.mp4"
        mock_video.mime_type = "video/mp4"
        mock_video.file_size = 1024 * 1024  # 1MB - under limit

        mock_message = MagicMock()
        mock_message.photo = None
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video = mock_video
        mock_message.video_note = None
        mock_message.sticker = None
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "video/mp4"
        assert attachments[0]["data"] == encode_bytes_to_base64(b"fake_video_data")

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
        mock_message.video = None
        mock_message.video_note = None
        mock_message.sticker = None
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "application/pdf"
        assert attachments[0]["filename"] == "document.pdf"

    @pytest.mark.asyncio
    async def test_extract_document_from_partial_telegram_message(self):
        """Partial Telegram message objects should not require every media attribute."""
        mock_bot = AsyncMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake_pdf_data")
        )
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        mock_doc = SimpleNamespace(
            file_id="test_doc_id",
            file_unique_id="test_unique_doc_id",
            file_name="document.pdf",
            mime_type="application/pdf",
        )

        mock_message = SimpleNamespace(
            document=mock_doc,
            chat=SimpleNamespace(id=123, type="private"),
        )

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
        mock_message.video = None
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
    async def test_extract_video_from_discord(self):
        """Test extracting a video from a Discord message."""
        mock_attachment = AsyncMock()
        mock_attachment.content_type = "video/mp4"
        mock_attachment.filename = "video.mp4"
        mock_attachment.read = AsyncMock(return_value=b"fake_video_data")

        mock_message = MagicMock()
        mock_message.attachments = [mock_attachment]
        mock_message.embeds = []
        mock_message.guild = None  # Private channel

        attachments = await extract_multimodal_from_discord(mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "video/mp4"
        assert attachments[0]["filename"] == "video.mp4"
        assert attachments[0]["data"] == encode_bytes_to_base64(b"fake_video_data")

    @pytest.mark.asyncio
    async def test_skip_unsupported_attachment(self):
        """Test that unsupported attachments are skipped."""
        mock_attachment = AsyncMock()
        mock_attachment.content_type = "application/zip"
        mock_attachment.filename = "archive.zip"

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


class TestTelegramStickerExtraction:
    """Tests for Telegram sticker extraction."""

    @pytest.mark.asyncio
    async def test_extract_static_sticker_from_telegram(self):
        """Test extracting a static sticker from a Telegram message."""
        mock_bot = AsyncMock()
        mock_file = AsyncMock()
        mock_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake_sticker_data")
        )
        mock_bot.get_file = AsyncMock(return_value=mock_file)

        mock_sticker = MagicMock()
        mock_sticker.file_id = "sticker_file_id"
        mock_sticker.file_unique_id = "sticker_unique_id"
        mock_sticker.is_animated = False
        mock_sticker.is_video = False
        mock_sticker.emoji = "🦖"
        mock_sticker.set_name = "godzilla_stickers"

        mock_message = MagicMock()
        mock_message.photo = None
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video = None
        mock_message.video_note = None
        mock_message.sticker = mock_sticker
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "image/webp"
        assert attachments[0]["filename"] == "sticker_sticker_unique_id.webp"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["data"] == encode_bytes_to_base64(b"fake_sticker_data")
        assert attachments[0]["media_metadata"]["type"] == "sticker"
        assert attachments[0]["media_metadata"]["emoji"] == "🦖"
        assert attachments[0]["media_metadata"]["set_name"] == "godzilla_stickers"

    @pytest.mark.asyncio
    async def test_extract_animated_sticker_thumb_from_telegram(self):
        """Test that animated Telegram stickers fall back to their static thumb."""
        mock_bot = AsyncMock()
        mock_thumb_file = AsyncMock()
        mock_thumb_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake_thumb_data")
        )
        mock_bot.get_file = AsyncMock(return_value=mock_thumb_file)

        mock_thumb = MagicMock()
        mock_thumb.file_id = "thumb_file_id"
        mock_thumb.file_unique_id = "thumb_unique_id"

        mock_sticker = MagicMock()
        mock_sticker.file_id = "sticker_file_id"
        mock_sticker.file_unique_id = "sticker_unique_id"
        mock_sticker.is_animated = True
        mock_sticker.is_video = False
        mock_sticker.thumb = mock_thumb
        mock_sticker.emoji = "🦖"
        mock_sticker.set_name = "godzilla_stickers"

        mock_message = MagicMock()
        mock_message.photo = None
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video = None
        mock_message.video_note = None
        mock_message.sticker = mock_sticker
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "image/webp"
        assert attachments[0]["filename"] == "sticker_sticker_unique_id_thumb.webp"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["data"] == encode_bytes_to_base64(b"fake_thumb_data")
        assert attachments[0]["media_metadata"]["type"] == "sticker"
        assert attachments[0]["media_metadata"]["animated"] is True
        assert attachments[0]["media_metadata"]["thumbnail"] is True
        assert attachments[0]["media_metadata"]["emoji"] == "🦖"
        assert attachments[0]["media_metadata"]["set_name"] == "godzilla_stickers"
        mock_bot.get_file.assert_called_once_with("thumb_file_id")

    @pytest.mark.asyncio
    async def test_extract_video_sticker_thumb_from_telegram(self):
        """Test that video Telegram stickers fall back to their static thumb."""
        mock_bot = AsyncMock()
        mock_thumb_file = AsyncMock()
        mock_thumb_file.download_as_bytearray = AsyncMock(
            return_value=bytearray(b"fake_thumb_data")
        )
        mock_bot.get_file = AsyncMock(return_value=mock_thumb_file)

        mock_thumb = MagicMock()
        mock_thumb.file_id = "thumb_file_id"
        mock_thumb.file_unique_id = "thumb_unique_id"

        mock_sticker = MagicMock()
        mock_sticker.file_id = "sticker_file_id"
        mock_sticker.file_unique_id = "sticker_unique_id"
        mock_sticker.is_animated = False
        mock_sticker.is_video = True
        mock_sticker.thumb = mock_thumb
        mock_sticker.emoji = "🦖"
        mock_sticker.set_name = "godzilla_stickers"

        mock_message = MagicMock()
        mock_message.photo = None
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video = None
        mock_message.video_note = None
        mock_message.sticker = mock_sticker
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "image/webp"
        assert attachments[0]["filename"] == "sticker_sticker_unique_id_thumb.webp"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["media_metadata"]["video"] is True
        assert attachments[0]["media_metadata"]["thumbnail"] is True
        assert attachments[0]["media_metadata"]["emoji"] == "🦖"
        assert attachments[0]["media_metadata"]["set_name"] == "godzilla_stickers"
        mock_bot.get_file.assert_called_once_with("thumb_file_id")

    @pytest.mark.asyncio
    async def test_animated_sticker_without_thumb_generates_placeholder(self):
        """Test that an animated sticker without a thumb gets a text placeholder."""
        mock_bot = AsyncMock()

        mock_sticker = MagicMock()
        mock_sticker.file_id = "sticker_file_id"
        mock_sticker.file_unique_id = "sticker_unique_id"
        mock_sticker.is_animated = True
        mock_sticker.is_video = False
        mock_sticker.thumb = None
        mock_sticker.emoji = "🦖"
        mock_sticker.set_name = "godzilla_stickers"

        mock_message = MagicMock()
        mock_message.photo = None
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video = None
        mock_message.video_note = None
        mock_message.sticker = mock_sticker
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "text/plain"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["media_metadata"]["type"] == "sticker"
        assert attachments[0]["media_metadata"]["viewable"] is False
        assert b"animated sticker" in base64.b64decode(attachments[0]["data"])
        assert attachments[0]["media_metadata"]["emoji"] == "🦖"
        assert attachments[0]["media_metadata"]["set_name"] == "godzilla_stickers"
        mock_bot.get_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_video_sticker_without_thumb_generates_placeholder(self):
        """Test that a video sticker without a thumb gets a text placeholder."""
        mock_bot = AsyncMock()

        mock_sticker = MagicMock()
        mock_sticker.file_id = "sticker_file_id"
        mock_sticker.file_unique_id = "sticker_unique_id"
        mock_sticker.is_animated = False
        mock_sticker.is_video = True
        mock_sticker.thumb = None
        mock_sticker.emoji = "🦖"
        mock_sticker.set_name = "godzilla_stickers"

        mock_message = MagicMock()
        mock_message.photo = None
        mock_message.document = None
        mock_message.audio = None
        mock_message.voice = None
        mock_message.video = None
        mock_message.video_note = None
        mock_message.sticker = mock_sticker
        mock_message.chat = MagicMock(id=123, type="private")

        attachments = await extract_multimodal_from_telegram(mock_bot, mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "text/plain"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["media_metadata"]["type"] == "sticker"
        assert attachments[0]["media_metadata"]["viewable"] is False
        assert b"video sticker" in base64.b64decode(attachments[0]["data"])
        assert attachments[0]["media_metadata"]["emoji"] == "🦖"
        assert attachments[0]["media_metadata"]["set_name"] == "godzilla_stickers"
        mock_bot.get_file.assert_not_called()


class TestDiscordStickerExtraction:
    """Tests for Discord sticker extraction."""

    @pytest.mark.asyncio
    async def test_extract_standard_sticker_from_discord(self, monkeypatch):
        """Test extracting a standard PNG sticker from a Discord message."""
        mock_sticker = MagicMock()
        mock_sticker.id = 123456789
        mock_sticker.name = "test_sticker"
        mock_sticker.format = 1  # PNG
        mock_sticker.url = "https://cdn.discordapp.com/stickers/123456789.png"

        mock_message = MagicMock()
        mock_message.attachments = []
        mock_message.embeds = []
        mock_message.guild = None
        mock_message.stickers = [mock_sticker]

        mock_response = MagicMock()
        mock_response.content = b"fake_sticker_png_data"
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient = MagicMock(return_value=mock_client)
        monkeypatch.setitem(__import__("sys").modules, "httpx", mock_httpx)

        attachments = await extract_multimodal_from_discord(mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "image/png"
        assert attachments[0]["filename"] == "sticker_123456789_test_sticker.png"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["media_metadata"]["type"] == "sticker"
        assert attachments[0]["media_metadata"]["name"] == "test_sticker"
        assert attachments[0]["media_metadata"]["viewable"] is True

    @pytest.mark.asyncio
    async def test_skip_lottie_sticker_from_discord(self, monkeypatch):
        """Test that Lottie Discord stickers are skipped with a placeholder."""
        mock_sticker = MagicMock()
        mock_sticker.id = 987654321
        mock_sticker.name = "lottie_sticker"
        mock_sticker.format = 3  # LOTTIE

        mock_message = MagicMock()
        mock_message.attachments = []
        mock_message.embeds = []
        mock_message.guild = None
        mock_message.stickers = [mock_sticker]

        attachments = await extract_multimodal_from_discord(mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "text/plain"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["media_metadata"]["type"] == "sticker"
        assert attachments[0]["media_metadata"]["viewable"] is False
        assert b"lottie_sticker" in base64.b64decode(attachments[0]["data"])

    @pytest.mark.asyncio
    async def test_sticker_download_failure_generates_placeholder(self, monkeypatch):
        """Test that a failed sticker download generates a placeholder."""
        mock_sticker = MagicMock()
        mock_sticker.id = 111111111
        mock_sticker.name = "broken_sticker"
        mock_sticker.format = 1  # PNG
        mock_sticker.url = "https://cdn.discordapp.com/stickers/111111111.png"

        mock_message = MagicMock()
        mock_message.attachments = []
        mock_message.embeds = []
        mock_message.guild = None
        mock_message.stickers = [mock_sticker]

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=Exception("network error")
        )

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_response)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient = MagicMock(return_value=mock_client)
        monkeypatch.setitem(__import__("sys").modules, "httpx", mock_httpx)

        attachments = await extract_multimodal_from_discord(mock_message)

        assert len(attachments) == 1
        assert attachments[0]["mime_type"] == "text/plain"
        assert attachments[0]["is_sticker"] is True
        assert attachments[0]["media_metadata"]["viewable"] is False
        assert b"broken_sticker" in base64.b64decode(attachments[0]["data"])
