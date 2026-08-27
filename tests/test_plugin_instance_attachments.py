"""Tests for inbound text-attachment content injection into the Fast Lane turn."""

import base64
from types import SimpleNamespace

from core.plugin_instance import (
    _TEXT_ATTACHMENT_MAX_CHARS,
    _inject_text_attachment_content,
)


def test_injects_text_attachment_content():
    msg = SimpleNamespace(text="Please read this and tell me what you think")
    attachments = [
        {
            "mime_type": "text/markdown",
            "filename": "TEMP_synth_feature_ideas.md",
            "data": base64.b64encode(b"# Ideas\n- make the agent better\n").decode(),
        }
    ]

    assert _inject_text_attachment_content(msg, attachments) is True
    assert "[Attachment: TEMP_synth_feature_ideas.md]" in msg.text
    assert "make the agent better" in msg.text
    assert msg.text.startswith("Please read this and tell me what you think")


def test_skips_non_text_attachments():
    msg = SimpleNamespace(text="Check this out")
    attachments = [
        # PDF (not text/*) and a broken base64 payload must both be skipped.
        {
            "mime_type": "application/pdf",
            "filename": "doc.pdf",
            "data": base64.b64encode(b"%PDF-1.4 fake").decode(),
        },
        {"mime_type": "text/plain", "filename": "broken.txt", "data": "!!not b64!!"},
        {"mime_type": "text/plain", "filename": "empty.txt", "data": ""},
    ]

    assert _inject_text_attachment_content(msg, attachments) is False
    assert msg.text == "Check this out"


def test_bounds_total_content():
    msg = SimpleNamespace(text="")
    long_content = "x" * (_TEXT_ATTACHMENT_MAX_CHARS + 5000)
    attachments = [
        {
            "mime_type": "text/plain",
            "filename": "huge.txt",
            "data": base64.b64encode(long_content.encode()).decode(),
        }
    ]

    assert _inject_text_attachment_content(msg, attachments) is True
    assert len(msg.text) <= _TEXT_ATTACHMENT_MAX_CHARS


def test_fail_safe_never_raises():
    # Non-dict attachments and a message without a settable text must not raise.
    assert _inject_text_attachment_content(None, [{"mime_type": "text/plain"}]) is False
    assert _inject_text_attachment_content(SimpleNamespace(text="hi"), "junk") is False
