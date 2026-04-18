"""Tests that setting a model on an external Cortex engine persists to the DB."""

import asyncio
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.prompt_request import PromptRequest, RuntimeContext, Turn
from core.webui import SynthWebUIInterface


def _make_endpoint(
    ep_id: int = 1, default_model: str | None = None
) -> ExternalEndpoint:
    return ExternalEndpoint(
        id=ep_id,
        name="my_ep",
        display_label="My EP",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://localhost:11435",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"cortex": True},
        available_models=["model-a", "model-b"],
        default_model=default_model,
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )


def test_set_model_persists_to_db(monkeypatch):
    """POST /api/components/cortex/model for an external endpoint engine must call
    ExternalEndpointRegistry.set_default_model so the selection survives restart."""

    endpoint = _make_endpoint()
    adapter_mock = MagicMock()
    bridge = ExternalCortexEngine(endpoint, adapter_mock)

    # Mock the Cortex registry to return our bridge for "ext_my_ep"
    mock_registry = MagicMock()
    mock_registry.get_engine.return_value = bridge

    set_default_model_mock = AsyncMock()
    mock_ext_registry = MagicMock()
    mock_ext_registry.set_default_model = set_default_model_mock

    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    with (
        patch(
            "core.cortex_registry.get_cortex_registry",
            return_value=mock_registry,
        ),
        patch(
            "core.external_endpoints.registry.get_external_endpoint_registry",
            return_value=mock_ext_registry,
        ),
    ):
        resp = client.post(
            "/api/components/cortex/model",
            json={"engine": "my_ep", "model": "model-b"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model"] == "model-b"

    # The DB persist must have been called exactly once with the right args
    set_default_model_mock.assert_awaited_once_with(1, "model-b")


def test_generate_response_retries_on_connection_error(monkeypatch):
    """ExternalCortexEngine.generate_response should retry connection failures."""

    endpoint = _make_endpoint()
    endpoint.extra_config = {"retry_attempts": 2, "retry_backoff": 0.0}
    adapter_mock = MagicMock()
    adapter_mock.chat_completion = AsyncMock(
        side_effect=[ConnectionError("Connection error"), MagicMock(content="retry-ok")]
    )
    bridge = ExternalCortexEngine(endpoint, adapter_mock)

    with patch(
        "core.external_endpoints.bridges.cortex_bridge.asyncio.sleep", return_value=None
    ):
        result = asyncio.run(
            bridge.generate_response([{"role": "user", "content": "hi"}])
        )

    assert result == "retry-ok"
    assert adapter_mock.chat_completion.call_count == 2


def test_handle_incoming_message_propagates_after_retry_exhaustion(monkeypatch):
    """If retries are exhausted, ExternalCortexEngine.handle_incoming_message should raise."""

    endpoint = _make_endpoint()
    endpoint.extra_config = {"retry_attempts": 2, "retry_backoff": 0.0}
    adapter_mock = MagicMock()
    adapter_mock.chat_completion = AsyncMock(
        side_effect=ConnectionError("Connection error")
    )
    bridge = ExternalCortexEngine(endpoint, adapter_mock)

    with patch(
        "core.external_endpoints.bridges.cortex_bridge.asyncio.sleep", return_value=None
    ):
        with pytest.raises(ConnectionError, match="Connection error"):
            asyncio.run(bridge.handle_incoming_message(None, None, {"foo": "bar"}))


def test_set_model_persist_failure_does_not_break_response(monkeypatch):
    """If the DB persist throws, the endpoint must still return HTTP 200."""

    endpoint = _make_endpoint()
    adapter_mock = MagicMock()
    bridge = ExternalCortexEngine(endpoint, adapter_mock)

    mock_registry = MagicMock()
    mock_registry.get_engine.return_value = bridge

    mock_ext_registry = MagicMock()
    mock_ext_registry.set_default_model = AsyncMock(side_effect=RuntimeError("db down"))

    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    with (
        patch(
            "core.cortex_registry.get_cortex_registry",
            return_value=mock_registry,
        ),
        patch(
            "core.external_endpoints.registry.get_external_endpoint_registry",
            return_value=mock_ext_registry,
        ),
    ):
        resp = client.post(
            "/api/components/cortex/model",
            json={"engine": "my_ep", "model": "model-a"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_handle_incoming_message_prefers_prompt_request_rendering(monkeypatch):
    """PromptRequest should be rendered as messages instead of flattened legacy JSON."""

    endpoint = _make_endpoint()
    adapter_mock = MagicMock()
    adapter_mock.chat_completion = AsyncMock(return_value=MagicMock(content="ok"))
    bridge = ExternalCortexEngine(endpoint, adapter_mock)

    req = PromptRequest(
        system_instruction="SYSTEM RULES",
        context_summary="CONTEXT BLOCK",
        conversation_history=[
            Turn(role="user", content="old-user"),
            Turn(role="assistant", content="old-assistant"),
        ],
        current_text="latest turn",
        runtime_ctx=RuntimeContext(username="scarlet"),
        mode="chat",
    )

    legacy_prompt = {
        "instructions": "legacy-instructions",
        "input": {"text": "legacy-input"},
        "__prompt_request": req,
    }

    result = asyncio.run(bridge.handle_incoming_message(None, None, legacy_prompt))
    assert result == "ok"

    await_args = adapter_mock.chat_completion.await_args
    assert await_args is not None
    sent_messages = await_args.args[0]
    assert sent_messages[0]["role"] == "system"
    assert "SYSTEM RULES" in sent_messages[0]["content"]
    assert "CONTEXT BLOCK" in sent_messages[0]["content"]

    assert sent_messages[-1]["role"] == "user"
    assert "latest turn" in sent_messages[-1]["content"]
    # If legacy flattening was used, this would include serialized keys like "input".
    assert "legacy-input" not in str(sent_messages[-1]["content"])


@pytest.mark.asyncio
async def test_run_auto_probe_uses_300_second_default(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)
    endpoint = _make_endpoint()
    registry = SimpleNamespace(
        get_endpoint=AsyncMock(return_value=endpoint),
        set_probe_result=AsyncMock(),
    )
    probe_result = SimpleNamespace(
        status="success",
        capabilities={"cortex": True},
        models=["gemini-3-flash-preview"],
        ping_echo="pong",
        error_message="",
    )
    captured: dict[str, object] = {}

    async def fake_wait_for(awaitable, timeout):
        captured["timeout"] = timeout
        return await awaitable

    monkeypatch.delenv("EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr("asyncio.wait_for", fake_wait_for)

    with patch(
        "core.external_endpoints.probe.probe_endpoint",
        AsyncMock(return_value=probe_result),
    ):
        result = await webui._run_auto_probe(1, "secret", registry)

    assert captured["timeout"] == 300.0
    assert result["models"] == ["gemini-3-flash-preview"]
    registry.set_probe_result.assert_awaited_once_with(
        1,
        status="success",
        capabilities={"cortex": True},
        models=["gemini-3-flash-preview"],
    )


@pytest.mark.asyncio
async def test_ping_external_endpoint_uses_300_second_timeout(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)
    endpoint = _make_endpoint()
    ping_test = AsyncMock(return_value=(True, "pong"))
    adapter = SimpleNamespace(ping_test=ping_test)
    registry = SimpleNamespace(get_endpoint=AsyncMock(return_value=endpoint))

    class DummyRequest:
        async def json(self):
            return {"model": "gemini-3-flash-preview"}

    with (
        patch(
            "core.external_endpoints.registry.get_external_endpoint_registry",
            return_value=registry,
        ),
        patch(
            "core.external_endpoints.crypto.decrypt_api_key",
            return_value="secret",
        ),
        patch(
            "core.external_endpoints.probe.get_adapter_for_endpoint",
            return_value=adapter,
        ),
    ):
        response = await webui.ping_external_endpoint(1, DummyRequest())

    assert response.status_code == 200
    ping_test.assert_awaited_once_with(
        model="gemini-3-flash-preview",
        timeout=300.0,
    )
