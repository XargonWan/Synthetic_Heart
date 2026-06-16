import asyncio
import json

import pytest

from interface.ollama_compat_server import OllamaCompatServer
from fastapi.responses import StreamingResponse


@pytest.mark.asyncio
async def test_streaming_deltas_and_processing_chunk(monkeypatch):
    monkeypatch.setattr(
        OllamaCompatServer, "_schedule_server_startup", lambda self: None
    )
    server = OllamaCompatServer()

    # Capture session_meta calls
    recorded_meta_calls = []

    async def fake_set_session_meta(interface_path, meta):
        recorded_meta_calls.append((interface_path, dict(meta)))

    monkeypatch.setattr("core.session_meta.set_session_meta", fake_set_session_meta)

    async def fake_add_message_to_context(**kwargs):
        return None

    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", fake_add_message_to_context
    )

    # Simulate plugin_instance producing a delayed string response
    async def fake_enqueue_and_wait(
        *,
        bot,
        message,
        context_memory=None,
        history_scope=None,
        priority=None,
        interface_id=None,
        skip_mention_check=None,
        original_message=None,
        timeout=None,
    ):
        # small delay to emulate generation
        await asyncio.sleep(0.01)
        return "hello from synth"

    monkeypatch.setattr("core.message_queue.enqueue_and_wait", fake_enqueue_and_wait)

    payload = {"messages": [{"role": "user", "content": "ciao"}], "stream": True}

    resp = await server._handle_chat_payload(payload)
    assert isinstance(resp, StreamingResponse)

    chunks = []
    async for raw in resp.body_iterator:
        chunks.append(json.loads(raw.decode()))

    # Initial processing chunk must be present
    assert chunks, "No streaming chunks received"
    first = chunks[0]
    assert first.get("type") == "processing" or first.get("processing") is True
    assert first.get("message", {}).get("content", "") == ""
    assert first.get("done") is False

    # All chunks should carry the same stable id when present
    ids = [c.get("id") for c in chunks if c.get("id")]
    assert ids and len(set(ids)) == 1

    # At least one streaming chunk must include an OpenAI-style delta
    assert any(c.get("choices") and c["choices"][0].get("delta") for c in chunks)

    # Final chunk must be done and include a choice.text with full output
    last = chunks[-1]
    assert last.get("done") is True
    assert last.get("choices") and last["choices"][0].get("text") == "hello from synth"

    # session_meta should have been set to True then cleared to False
    assert any(meta.get("processing") is True for _, meta in recorded_meta_calls)
    assert any(meta.get("processing") is False for _, meta in recorded_meta_calls)


@pytest.mark.asyncio
async def test_nonstream_completion_includes_text(monkeypatch):
    monkeypatch.setattr(
        OllamaCompatServer, "_schedule_server_startup", lambda self: None
    )
    server = OllamaCompatServer()

    async def fake_add_message_to_context(**kwargs):
        return None

    async def fake_set_session_meta(interface_path, meta):
        return None

    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", fake_add_message_to_context
    )
    monkeypatch.setattr("core.session_meta.set_session_meta", fake_set_session_meta)

    async def fake_enqueue_and_wait(
        *,
        bot,
        message,
        context_memory=None,
        history_scope=None,
        priority=None,
        interface_id=None,
        skip_mention_check=None,
        original_message=None,
        timeout=None,
    ):
        return "hello from synth"

    monkeypatch.setattr("core.message_queue.enqueue_and_wait", fake_enqueue_and_wait)

    payload = {"messages": [{"role": "user", "content": "ciao"}], "stream": False}
    resp = await server._handle_chat_payload(payload)
    assert resp.status_code == 200
    body = resp.body
    # FastAPI JSONResponse stores bytes in `.body`
    parsed = json.loads(body.decode())
    assert (
        parsed.get("choices") and parsed["choices"][0].get("text") == "hello from synth"
    )
    assert parsed.get("message", {}).get("content") == "hello from synth"
    assert "id" in parsed and parsed["id"].startswith("chatcmpl-")


@pytest.mark.asyncio
async def test_nonstream_completion_with_actions_executed(monkeypatch):
    monkeypatch.setattr(
        OllamaCompatServer, "_schedule_server_startup", lambda self: None
    )
    server = OllamaCompatServer()

    async def fake_add_message_to_context(**kwargs):
        return None

    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", fake_add_message_to_context
    )

    async def fake_set_session_meta(interface_path, meta):
        return None

    async def fake_enqueue_and_wait(
        *,
        bot,
        message,
        context_memory=None,
        history_scope=None,
        priority=None,
        interface_id=None,
        skip_mention_check=None,
        original_message=None,
        timeout=None,
    ):
        return None

    monkeypatch.setattr("core.session_meta.set_session_meta", fake_set_session_meta)
    monkeypatch.setattr("core.message_queue.enqueue_and_wait", fake_enqueue_and_wait)

    payload = {"messages": [{"role": "user", "content": "ciao"}], "stream": False}
    resp = await server._handle_chat_payload(payload)
    assert resp.status_code == 200
    parsed = json.loads(resp.body.decode())

    assert parsed.get("message", {}).get("content") == ""
    assert parsed.get("final_response", "") == ""
    assert parsed.get("response", "") == ""
    assert parsed.get("choices") and parsed["choices"][0].get("text") == ""
    assert parsed.get("id", "").startswith("chatcmpl-")
