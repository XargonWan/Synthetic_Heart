"""A user-deleted Zen endpoint must stay deleted across restarts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core.external_endpoints import registry as registry_mod


class _FakeEndpoint:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeRegistry:
    def __init__(self, endpoints: list) -> None:
        self._endpoints = endpoints
        self.added: list[str] = []

    async def list_endpoints(self) -> list:
        return list(self._endpoints)

    async def add_endpoint(self, name: str, **kwargs: object) -> object:
        self.added.append(name)
        return None


def _preset() -> list[dict]:
    return [
        {
            "provider_id": "zen_llm_engine",
            "suggested_name": "zen-llm-engine",
            "suggested_label": "Zen",
            "base_url": "http://synth-zen-llm-engine:8000",
            "protocol": "openai",
        }
    ]


async def _run_with(endpoints: list) -> _FakeRegistry:
    fake = _FakeRegistry([_FakeEndpoint(n) for n in endpoints])
    with (
        patch.object(registry_mod, "get_external_endpoint_registry", return_value=fake),
        patch(
            "core.external_endpoints.preset_registry.load_presets",
            return_value=_preset(),
        ),
    ):
        await registry_mod.ensure_default_zen_endpoint()
    return fake


@pytest.mark.asyncio
async def test_deleted_zen_not_recreated_when_others_exist() -> None:
    fake = await _run_with(endpoints=["ollama"])
    assert fake.added == []


@pytest.mark.asyncio
async def test_zen_seeded_on_fresh_registry() -> None:
    fake = await _run_with(endpoints=[])
    assert fake.added == ["zen-llm-engine"]
