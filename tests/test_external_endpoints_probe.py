"""Tests for the external-endpoint probe orchestration.

The probe used to run three sub-tasks that each fetched the endpoint's model
listing: on a provider whose ``/models`` takes ~40 s the run blew past the
caller's timeout and, because a cancelled run persists nothing, the endpoint's
stored model list never updated.  These tests pin the two properties that fix
that: the listing is fetched ONCE and shared, and a slow sub-step cannot
discard what the other steps already gathered.
"""

from __future__ import annotations

import asyncio

import pytest

from core.external_endpoints.adapters.base import ModelInfo
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.external_endpoints.probe import probe_endpoint


def _endpoint() -> ExternalEndpoint:
    return ExternalEndpoint(
        id=1,
        name="SlowProvider",
        display_label="Slow Provider",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://slow.example/v1",
        api_key_enc="",
        enabled=True,
        capabilities={},
        subsystem_map={},
        available_models=[],
        default_model="chat-model",
        probe_status="never",
        last_probe_at=None,
        extra_config={},
    )


class FakeAdapter:
    """Records how often the listing is fetched and what the steps received."""

    def __init__(
        self, *, models=None, caps_delay: float = 0.0, ping_delay: float = 0.0
    ):
        self._models = (
            models
            if models is not None
            else [
                ModelInfo(id="chat-model", name="Chat", capabilities={"cortex": True})
            ]
        )
        self._caps_delay = caps_delay
        self._ping_delay = ping_delay
        self.list_models_calls = 0
        self.caps_models: list[ModelInfo] | None | str = "unset"
        self.ping_models: list[ModelInfo] | None | str = "unset"
        self.ping_model_arg: str | None | str = "unset"

    async def list_models(self) -> list[ModelInfo]:
        self.list_models_calls += 1
        return self._models

    async def probe_capabilities(self, models: list[ModelInfo] | None = None):
        self.caps_models = models
        if self._caps_delay:
            await asyncio.sleep(self._caps_delay)
        return {
            "cortex": False,
            "vox": True,
            "auris": False,
            "live": False,
            "vision": True,
        }

    async def ping_test(self, model=None, timeout=None, models=None):
        self.ping_model_arg = model
        self.ping_models = models
        if self._ping_delay:
            await asyncio.sleep(self._ping_delay)
        return True, "pong"


@pytest.mark.asyncio
async def test_probe_endpoint_fetches_models_once_and_shares_them(monkeypatch):
    adapter = FakeAdapter()
    monkeypatch.setattr(
        "core.external_endpoints.probe.get_adapter_for_endpoint",
        lambda endpoint, api_key: adapter,
    )

    result = await probe_endpoint(_endpoint(), "key")

    assert result.status == "success"
    assert adapter.list_models_calls == 1
    # Both sub-steps receive the SAME pre-fetched listing (they must not re-query).
    assert adapter.caps_models is adapter._models
    assert adapter.ping_models is adapter._models
    assert adapter.ping_model_arg == "chat-model"
    assert result.models == ["chat-model"]
    assert result.capabilities["cortex"] is True
    assert result.capabilities["vision"] is True
    assert result.ping_echo == "pong"


@pytest.mark.asyncio
async def test_probe_endpoint_keeps_models_when_a_step_hangs(monkeypatch):
    """A hanging capability probe must not lose the model listing (the bug)."""
    monkeypatch.setenv("EXTERNAL_ENDPOINT_PROBE_CAPABILITIES_TIMEOUT_SECONDS", "0.2")
    adapter = FakeAdapter(caps_delay=5.0)
    monkeypatch.setattr(
        "core.external_endpoints.probe.get_adapter_for_endpoint",
        lambda endpoint, api_key: adapter,
    )

    result = await probe_endpoint(_endpoint(), "key")

    assert result.status == "success"
    assert result.models == ["chat-model"]
    assert "capabilities: timed out" in result.error_message
    # The ping step still completed, so cortex reflects the real chat check.
    assert result.capabilities["cortex"] is True


@pytest.mark.asyncio
async def test_probe_endpoint_bounds_a_hanging_model_listing(monkeypatch):
    monkeypatch.setenv("EXTERNAL_ENDPOINT_PROBE_MODELS_TIMEOUT_SECONDS", "0.2")

    class HangingListing(FakeAdapter):
        async def list_models(self):
            self.list_models_calls += 1
            await asyncio.sleep(5.0)
            return []

    adapter = HangingListing()
    monkeypatch.setattr(
        "core.external_endpoints.probe.get_adapter_for_endpoint",
        lambda endpoint, api_key: adapter,
    )

    result = await probe_endpoint(_endpoint(), "key")

    assert result.status == "success"
    assert "models: timed out" in result.error_message
    assert adapter.caps_models == []


@pytest.mark.asyncio
async def test_probe_endpoint_reports_failure_when_nothing_gathered(monkeypatch):
    class Broken(FakeAdapter):
        async def list_models(self):
            self.list_models_calls += 1
            raise RuntimeError("no catalogue")

        async def probe_capabilities(self, models=None):
            raise RuntimeError("no capabilities")

        async def ping_test(self, model=None, timeout=None, models=None):
            raise RuntimeError("no ping")

    monkeypatch.setattr(
        "core.external_endpoints.probe.get_adapter_for_endpoint",
        lambda endpoint, api_key: Broken(),
    )

    result = await probe_endpoint(_endpoint(), "key")

    assert result.status == "failed"
    assert "models: no catalogue" in result.error_message
    assert "capabilities: no capabilities" in result.error_message
    assert "ping: no ping" in result.error_message
