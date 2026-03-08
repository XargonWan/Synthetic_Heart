import asyncio
import json

import pytest

from interface.ollama_compat_server import OllamaCompatServer
from fastapi.responses import StreamingResponse


@pytest.mark.asyncio
async def test_streaming_deltas_and_processing_chunk(monkeypatch):
    server = OllamaCompatServer()

    # Capture session_meta calls
    recorded_meta_calls = []

    async def fake_set_session_meta(interface_path, meta):
        recorded_meta_calls.append((interface_path, dict(meta)))

    monkeypatch.setattr("core.session_meta.set_session_meta", fake_set_session_meta)

    # Simulate plugin_instance producing a delayed string response
    async def fake_handle_incoming_message(
        interface, message_obj, context_memory, iface_id
    ):
        # small delay to emulate generation
        await asyncio.sleep(0.01)
        return "hello from synth"

    monkeypatch.setattr(
        "core.plugin_instance.handle_incoming_message", fake_handle_incoming_message
    )

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
    server = OllamaCompatServer()

    async def fake_handle_incoming_message(
        interface, message_obj, context_memory, iface_id
    ):
        return "hello from synth"

    monkeypatch.setattr(
        "core.plugin_instance.handle_incoming_message", fake_handle_incoming_message
    )

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
