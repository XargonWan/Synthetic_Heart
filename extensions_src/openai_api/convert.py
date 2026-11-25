"""Pure helpers of the OpenAI-compatible API: parsing requests, shaping responses.

No I/O here, so every shape the clients depend on can be tested directly.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

INTERFACE_ID = "openai_api"
ROLES = ("system", "user", "assistant")
#: What a client may name a conversation by, in the order they are looked at.
CONVERSATION_FIELDS = ("conversation", "session", "user", "thread_id")


class BadRequest(ValueError):
    """The request is malformed; the message is for the client."""


def now() -> int:
    return int(time.time())


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def text_of(content: Any) -> str:
    """The text of a message ``content``: a string, or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text"):
                value = part.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "\n".join(p for p in parts if p)
    return ""


def split_messages(messages: Any, max_history: int) -> tuple[str, list[dict[str, str]]]:
    """The question (the last message, which must be the user's) and what came before.

    Earlier turns are kept as the conversation the Core builds the prompt from; roles
    other than system/user/assistant (tool, function...) are not part of it."""
    if not isinstance(messages, list) or not messages:
        raise BadRequest("'messages' must be a non-empty list")
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        raise BadRequest("the last message must have role 'user'")
    question = text_of(last.get("content")).strip()
    if not question:
        raise BadRequest("the last message has no text")
    history: list[dict[str, str]] = []
    for item in messages[:-1]:
        if not isinstance(item, dict) or item.get("role") not in ROLES:
            continue
        text = text_of(item.get("content")).strip()
        if text:
            history.append({"role": str(item["role"]), "text": text})
    return question, history[-max_history:] if max_history > 0 else []


def prompt_text(prompt: Any) -> str:
    """``/v1/completions`` takes a string or a list of strings."""
    if isinstance(prompt, str):
        text = prompt
    elif isinstance(prompt, list) and all(isinstance(p, str) for p in prompt):
        text = "\n".join(prompt)
    else:
        raise BadRequest("'prompt' must be a string or a list of strings")
    if not text.strip():
        raise BadRequest("'prompt' must not be empty")
    return text


def conversation_id(body: dict[str, Any]) -> str | None:
    for field in CONVERSATION_FIELDS:
        value = body.get(field)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def chat_id_for(conversation: str | None) -> str:
    """The chat a request belongs to. Without a conversation every request is its own."""
    if not conversation:
        return f"api-{uuid.uuid4().hex[:16]}"
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in conversation)
    return f"c-{safe[:64]}"


def path_for(chat: str) -> str:
    return f"{INTERFACE_ID}/{chat}"


def chat_of(path: Any) -> str | None:
    parts = str(path or "").split("/")
    if len(parts) == 2 and parts[0] == INTERFACE_ID and parts[1]:
        return parts[1]
    return None


# ------------------------------------------------------------------- responses


def model_descriptor(name: str, capabilities: dict[str, bool]) -> dict[str, Any]:
    return {
        "id": name,
        "object": "model",
        "created": now(),
        "owned_by": "synthetic-heart",
        "capabilities": dict(capabilities),
    }


def models_list(name: str, capabilities: dict[str, bool]) -> dict[str, Any]:
    return {"object": "list", "data": [model_descriptor(name, capabilities)]}


USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def chat_completion(request_id: str, model: str, text: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": now(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": dict(USAGE),
    }


def text_completion(request_id: str, model: str, text: str) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "text_completion",
        "created": now(),
        "model": model,
        "choices": [
            {"index": 0, "text": text, "logprobs": None, "finish_reason": "stop"}
        ],
        "usage": dict(USAGE),
    }


def chat_chunk(
    request_id: str,
    model: str,
    *,
    created: int,
    delta: dict[str, Any],
    finish: str | None = None,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def text_chunk(
    request_id: str,
    model: str,
    *,
    created: int,
    text: str,
    finish: str | None = None,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "object": "text_completion",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "text": text, "logprobs": None, "finish_reason": finish}
        ],
    }


def sse(data: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n"


SSE_DONE = b"data: [DONE]\n\n"
SSE_KEEPALIVE = b": keep-alive\n\n"


def error_body(message: str, kind: str, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": kind, "param": None, "code": code}}


def split_for_stream(text: str, size: int = 400) -> list[str]:
    """The reply in pieces, cut at whitespace, so a client sees it arrive."""
    pieces: list[str] = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind(" ", 0, size)
        cut = cut + 1 if cut > 0 else size
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces
