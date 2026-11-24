"""The Iris model selector lists a model only if it declares vision or passed the probe."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from core.external_endpoints import vision_filter
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.external_endpoints.vision_probe import run_vision_probe
from core.webui import SynthWebUIInterface


def _ep(models_metadata: list[dict] | None = None) -> ExternalEndpoint:
    return ExternalEndpoint(
        id=7,
        name="harmonyai",
        display_label="Harmony",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://localhost:1",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"vision": True},
        available_models=["llm", "tts", "embed"],
        default_model="llm",
        probe_status="success",
        last_probe_at=None,
        extra_config={},
        models_metadata=models_metadata
        if models_metadata is not None
        else [
            {"id": "llm", "capabilities": {"vision": True}},
            {"id": "tts", "capabilities": {}},
        ],
    )


def test_listed_when_vision_skill_or_probe_passed() -> None:
    assert vision_filter.is_listed_for_iris({"capabilities": {"vision": True}})
    assert vision_filter.is_listed_for_iris(
        {"capabilities": {}, "vision_probe": {"ok": True}}
    )


def test_not_listed_without_skill_and_without_a_passing_probe() -> None:
    assert not vision_filter.is_listed_for_iris(None)
    assert not vision_filter.is_listed_for_iris({"capabilities": {}})
    assert not vision_filter.is_listed_for_iris(
        {"capabilities": {}, "vision_probe": {"ok": False}}
    )


def test_a_declared_skill_stays_listed_even_when_the_probe_failed() -> None:
    """A model that declares vision but stalls is slow, not unlisted (the test button says so)."""
    assert vision_filter.is_listed_for_iris(
        {"capabilities": {"vision": True}, "vision_probe": {"ok": False}}
    )


def test_models_to_probe_are_the_ones_without_the_skill() -> None:
    meta = [{"id": "llm", "capabilities": {"vision": True}}]
    assert vision_filter.models_to_probe(["llm", "tts", "embed"], meta) == [
        "tts",
        "embed",
    ]


def test_record_probe_updates_or_creates_the_entry() -> None:
    meta = [{"id": "llm", "capabilities": {"vision": True}}]
    out = vision_filter.record_probe(meta, "llm", ok=False, elapsed_ms=120000)
    assert out[0]["vision_probe"]["ok"] is False
    assert out[0]["capabilities"] == {"vision": True}
    created = vision_filter.record_probe(out, "tts", ok=True, elapsed_ms=900)
    assert created[1]["id"] == "tts" and created[1]["vision_probe"]["ok"] is True
    assert "vision_probe" not in meta[0], "the input list is not modified"


def test_a_refreshed_model_list_keeps_the_stored_probe() -> None:
    old = [{"id": "tts", "capabilities": {}, "vision_probe": {"ok": True}}]
    new = [{"id": "tts", "capabilities": {}}, {"id": "other", "capabilities": {}}]
    merged = vision_filter.carry_over_probes(new, old)
    assert merged[0]["vision_probe"] == {"ok": True}
    assert "vision_probe" not in merged[1]


@pytest.mark.asyncio
async def test_run_vision_probe_reports_ok_and_failure_without_raising() -> None:
    good = SimpleNamespace(describe_image=AsyncMock(return_value="a clown"))
    result = await run_vision_probe(_ep(), good, "llm")
    assert result.ok and result.description == "a clown" and result.model == "llm"

    bad = SimpleNamespace(describe_image=AsyncMock(return_value=None))
    failed = await run_vision_probe(_ep(), bad, "tts", timeout_seconds=5)
    assert not failed.ok and failed.error and failed.model == "tts"

    boom = SimpleNamespace(describe_image=AsyncMock(side_effect=RuntimeError("x")))
    # The bridge turns an adapter exception into "no description": still no raise.
    assert not (await run_vision_probe(_ep(), boom, "tts")).ok


class _Req:
    async def json(self) -> dict:
        return {}


@pytest.mark.asyncio
async def test_bulk_probe_tests_only_models_without_the_skill_and_stores_each() -> None:
    ep = _ep()
    recorded: list[tuple[str, bool, int]] = []

    async def _record(ep_id: int, outcomes: list[tuple[str, bool, int]]) -> None:
        recorded.extend(outcomes)

    async def _describe(_img, mime_type=None, prompt=None, model=None, **_k):
        return "a photo" if model == "embed" else None

    registry = SimpleNamespace(
        get_endpoint=AsyncMock(return_value=ep), record_vision_probes=_record
    )
    adapter = SimpleNamespace(describe_image=_describe)
    with (
        patch(
            "core.external_endpoints.registry.get_external_endpoint_registry",
            return_value=registry,
        ),
        patch("core.external_endpoints.crypto.decrypt_api_key", return_value="k"),
        patch(
            "core.external_endpoints.probe.get_adapter_for_endpoint",
            return_value=adapter,
        ),
    ):
        response = await SynthWebUIInterface(
            autostart=False
        ).probe_external_endpoint_vision_models(ep.id)

    data = json.loads(bytes(response.body))
    assert data["probed"] == 2 and data["passed"] == 1
    assert [(m, ok) for m, ok, _ in recorded] == [("tts", False), ("embed", True)]
    assert "llm" not in [m for m, _, _ in recorded], (
        "a declared-vision model is not re-probed"
    )


@pytest.mark.asyncio
async def test_single_test_stores_the_outcome_on_the_model() -> None:
    ep = _ep()
    record = AsyncMock()
    registry = SimpleNamespace(
        get_endpoint=AsyncMock(return_value=ep), record_vision_probes=record
    )
    adapter = SimpleNamespace(describe_image=AsyncMock(return_value="a clown"))
    with (
        patch(
            "core.external_endpoints.registry.get_external_endpoint_registry",
            return_value=registry,
        ),
        patch("core.external_endpoints.crypto.decrypt_api_key", return_value="k"),
        patch(
            "core.external_endpoints.probe.get_adapter_for_endpoint",
            return_value=adapter,
        ),
    ):
        await SynthWebUIInterface(autostart=False).test_external_endpoint_vision(
            ep.id, cast(Request, _Req())
        )

    record.assert_awaited_once()
    assert record.await_args.args[1][0][:2] == ("llm", True)
