"""Tests that setting a model on an ext_* Cortex engine persists to the DB."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.webui import SynthWebUIInterface


def _make_endpoint(
    ep_id: int = 1, default_model: str | None = None
) -> ExternalEndpoint:
    return ExternalEndpoint(
        id=ep_id,
        name="my_ep",
        display_label="My EP",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://localhost:11434",
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
    """POST /api/components/cortex/model for an ext_* engine must call
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
            json={"engine": "ext_my_ep", "model": "model-b"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["model"] == "model-b"

    # The DB persist must have been called exactly once with the right args
    set_default_model_mock.assert_awaited_once_with(1, "model-b")


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
            json={"engine": "ext_my_ep", "model": "model-a"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
