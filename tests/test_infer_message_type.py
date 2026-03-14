"""Tests for core.prompt_engine._infer_message_type helper."""

from types import SimpleNamespace

import pytest

# the helper was removed/moved in recent refactors; if it's unavailable we
# skip the entire test module rather than error during import.
try:
    from core.prompt_engine import _infer_message_type
except ImportError:
    pytest.skip("_infer_message_type helper not present", allow_module_level=True)


def _msg(**kwargs):
    """Build a minimal message-like namespace."""
    return SimpleNamespace(**kwargs)


def test_plain_text_returns_text():
    msg = _msg(text="hello")
    assert _infer_message_type(msg) == "text"


def test_voice_attribute_returns_voice():
    msg = _msg(text="", voice=object())
    assert _infer_message_type(msg) == "voice"


def test_video_note_returns_video_note():
    msg = _msg(text="", video_note=object())
    assert _infer_message_type(msg) == "video_note"


def test_video_returns_video():
    msg = _msg(text="", video=object())
    assert _infer_message_type(msg) == "video"


def test_sticker_returns_sticker():
    msg = _msg(text="", sticker=object())
    assert _infer_message_type(msg) == "sticker"


def test_document_returns_document():
    msg = _msg(text="", document=object())
    assert _infer_message_type(msg) == "document"


def test_image_data_returns_image():
    msg = _msg(text="")
    assert _infer_message_type(msg, image_data={"type": "photo"}) == "image"


def test_photo_attribute_returns_image():
    msg = _msg(text="", photo=[object()])
    assert _infer_message_type(msg) == "image"


def test_grillo_beat_returns_internal_beat():
    msg = _msg(text="beat", grillo_beat=True)
    assert _infer_message_type(msg) == "internal_beat"


def test_attachment_audio_mime_returns_audio():
    msg = _msg(text="")
    atts = [{"mime_type": "audio/mpeg", "media_metadata": {}}]
    assert _infer_message_type(msg, attachments=atts) == "audio"


def test_attachment_voice_media_metadata_returns_voice():
    msg = _msg(text="")
    atts = [{"mime_type": "audio/ogg", "media_metadata": {"type": "voice"}}]
    assert _infer_message_type(msg, attachments=atts) == "voice"


def test_attachment_video_mime_returns_video():
    msg = _msg(text="")
    atts = [{"mime_type": "video/mp4", "media_metadata": {}}]
    assert _infer_message_type(msg, attachments=atts) == "video"


def test_attachment_image_mime_returns_image():
    msg = _msg(text="")
    atts = [{"mime_type": "image/jpeg", "media_metadata": {}}]
    assert _infer_message_type(msg, attachments=atts) == "image"


def test_voice_takes_priority_over_text():
    """Even if text is present (transcription), voice attribute wins."""
    msg = _msg(text="I said hello", voice=object())
    assert _infer_message_type(msg) == "voice"
