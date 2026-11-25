"""Protocols register how their adapter is built; the probe names none of them."""

from __future__ import annotations

from typing import Any

import pytest

from core.external_endpoints import probe
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.external_endpoints.probe import (
    get_adapter_for_endpoint,
    register_protocol,
    registered_protocols,
    unregister_protocol,
)


def endpoint(protocol: EndpointProtocol, **kw: Any) -> ExternalEndpoint:
    base: dict[str, Any] = dict(
        id=1,
        name="ep",
        display_label="ep",
        protocol=protocol,
        base_url="http://127.0.0.1:1",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={},
        available_models=[],
        default_model=None,
        probe_status="never",
        last_probe_at=None,
        extra_config={},
    )
    base.update(kw)
    return ExternalEndpoint(**base)


def test_every_builtin_protocol_is_registered() -> None:
    assert set(registered_protocols()) == set(EndpointProtocol)


@pytest.mark.parametrize(
    ("protocol", "class_name"),
    [
        (EndpointProtocol.OPENAI, "OpenAICompatAdapter"),
        (EndpointProtocol.HARMONY, "HarmonyAIAdapter"),
        (EndpointProtocol.CUSTOM, "OpenAICompatAdapter"),
    ],
)
def test_the_registered_factory_builds_the_adapter(
    protocol: EndpointProtocol, class_name: str
) -> None:
    assert type(get_adapter_for_endpoint(endpoint(protocol))).__name__ == class_name


@pytest.mark.parametrize(
    "protocol",
    [
        EndpointProtocol.OPENAI,
        EndpointProtocol.HARMONY,
        EndpointProtocol.CUSTOM,
    ],
)
def test_a_base_url_is_required_where_the_adapter_needs_one(
    protocol: EndpointProtocol,
) -> None:
    with pytest.raises(ValueError, match="requires a base_url"):
        get_adapter_for_endpoint(endpoint(protocol, base_url=""))


@pytest.mark.parametrize(
    "protocol",
    [EndpointProtocol.GEMINI, EndpointProtocol.ANTHROPIC, EndpointProtocol.FISH],
)
def test_an_api_key_is_required_where_the_adapter_needs_one(
    protocol: EndpointProtocol,
) -> None:
    with pytest.raises(ValueError, match="requires an API key"):
        get_adapter_for_endpoint(endpoint(protocol), api_key="")


def test_the_custom_legacy_tts_mode_picks_its_own_adapter() -> None:
    adapter = get_adapter_for_endpoint(
        endpoint(EndpointProtocol.CUSTOM, extra_config={"legacy_http_tts": True})
    )
    assert type(adapter).__name__ == "LegacyHttpTTSAdapter"


def test_a_protocol_can_be_added_replaced_and_removed() -> None:
    original = probe._ADAPTER_FACTORIES[EndpointProtocol.HARMONY]
    seen: list[tuple[str, str]] = []

    def factory(ep: ExternalEndpoint, key: str) -> Any:
        seen.append((ep.name, key))
        return "adapter"

    try:
        register_protocol(EndpointProtocol.HARMONY, factory)
        assert (
            get_adapter_for_endpoint(endpoint(EndpointProtocol.HARMONY), "k")
            == "adapter"
        )
        assert seen == [("ep", "k")]
        unregister_protocol(EndpointProtocol.HARMONY)
        with pytest.raises(ValueError, match="Unsupported protocol"):
            get_adapter_for_endpoint(endpoint(EndpointProtocol.HARMONY))
    finally:
        register_protocol(EndpointProtocol.HARMONY, original)
