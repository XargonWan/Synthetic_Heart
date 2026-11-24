"""A reply to an earlier message must reach the model together with what it quotes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.prompt_engine import _assemble_prompt_request
from core.prompt_renderers import OpenAIRenderer

QUOTED = "Ma aspetta un attimo... le circostanze della traccia?"


def _request(reply_payload: dict[str, Any] | None):
    payload: dict[str, Any] = {"text": "Quel messaggio non lo capisco"}
    if reply_payload is not None:
        payload["reply_message_id"] = reply_payload
    return _assemble_prompt_request(
        prompt_dict={"instructions": "test", "input": {"payload": payload}},
        context_section={},
        text="Quel messaggio non lo capisco",
        interface_name="telegram",
        interface_path="telegram/31321637",
        message=SimpleNamespace(
            from_user=SimpleNamespace(
                username="Xargon", first_name="Jay", last_name="Cheshire"
            )
        ),
        is_grillo_internal=False,
        beat_type="",
        is_voice_input=False,
        resolved_language=None,
        resolved_message_tone=None,
        image_data=None,
        attachments=None,
        allowed_action_types=None,
    )


def _last_user_content(req: Any) -> str:
    messages = OpenAIRenderer(req).render()
    users = [m for m in messages if m.get("role") == "user"]
    content = users[-1]["content"]
    return content if isinstance(content, str) else str(content)


def test_the_quoted_message_is_in_what_the_model_is_sent() -> None:
    req = _request({"text": QUOTED, "from": {"username": "Rekku"}})
    sent = _last_user_content(req)
    assert QUOTED in sent
    assert "Rekku" in sent
    assert sent.rstrip().endswith("Quel messaggio non lo capisco")


def test_a_reply_to_a_photo_without_caption_quotes_nothing() -> None:
    """Current behaviour, pinned: a media reply carries no text, so no quote is sent."""
    req = _request({"text": "[Non-text content]", "from": {"username": "Jay"}})
    assert "replying to" not in _last_user_content(req)


def test_no_quote_block_without_a_reply() -> None:
    assert "replying to" not in _last_user_content(_request(None))
