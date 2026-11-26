# core/external_endpoints/probe.py
"""Auto-probe an external endpoint and return a ProbeResult.

The probe:
1. Selects the correct adapter for the endpoint's protocol.
2. Calls ``adapter.list_models()`` ONCE and shares that listing with the other
   steps, so a slow provider is asked for its model catalogue a single time per
   probe run instead of once per sub-task.
3. Calls ``adapter.probe_capabilities(models=...)`` to detect supported
   subsystems.
4. Calls ``adapter.ping_test(..., models=...)`` to verify cortex connectivity
   and obtain a reply echo.  The ping result sets ``capabilities["cortex"]`` and
   is stored in ``ProbeResult.ping_echo``.
5. Returns a :class:`ProbeResult` with the findings.

Every step is bounded by its own budget (see the ``EXTERNAL_ENDPOINT_PROBE_*``
env vars below).  This function therefore always returns a result — including
the collected models — instead of being cancelled by the caller's outer
timeout, which used to discard a fully successful model listing whenever one
slow step overran.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.logging_utils import log_debug, log_info, log_warning

# Per-step budgets (seconds) for a single probe run.
#
# The whole run must finish well inside the WebUI's own
# ``EXTERNAL_ENDPOINT_PROBE_TIMEOUT_SECONDS`` guard (300 s): when that guard
# fires, nothing is persisted and the endpoint keeps its stale model list, which
# is exactly the failure mode these budgets remove.
_PROBE_MODELS_TIMEOUT_ENV = "EXTERNAL_ENDPOINT_PROBE_MODELS_TIMEOUT_SECONDS"
_PROBE_CAPABILITIES_TIMEOUT_ENV = "EXTERNAL_ENDPOINT_PROBE_CAPABILITIES_TIMEOUT_SECONDS"
_PROBE_PING_TIMEOUT_ENV = "EXTERNAL_ENDPOINT_PROBE_PING_TIMEOUT_SECONDS"

_DEFAULT_MODELS_TIMEOUT_SECONDS = 90.0
_DEFAULT_CAPABILITIES_TIMEOUT_SECONDS = 90.0
_DEFAULT_PING_TIMEOUT_SECONDS = 60.0


def _probe_step_timeout(env_name: str, default: float) -> float:
    """Return a positive per-step budget from *env_name*, else *default*."""
    raw = os.getenv(env_name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


@dataclass
class ProbeResult:
    """Result of probing an external endpoint."""

    status: str  # 'success' | 'failed'
    capabilities: dict[str, bool] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
    models_metadata: list[dict] = field(default_factory=list)
    error_message: str = ""
    ping_echo: str = ""


#: Builds the adapter for one endpoint: ``(endpoint, api_key) -> BaseProtocolAdapter``.
AdapterFactory = Callable[[ExternalEndpoint, str], "BaseProtocolAdapter"]  # type: ignore[name-defined]  # noqa: F821

#: Protocol -> how to build its adapter. Each protocol registers itself; nothing in
#: ``get_adapter_for_endpoint`` names a protocol.
_ADAPTER_FACTORIES: dict[EndpointProtocol, AdapterFactory] = {}


def register_protocol(protocol: EndpointProtocol, factory: AdapterFactory) -> None:
    """Make ``protocol`` usable: ``factory`` builds the adapter for an endpoint of it.

    Registering a protocol again replaces the earlier factory."""
    _ADAPTER_FACTORIES[protocol] = factory


def unregister_protocol(protocol: EndpointProtocol) -> None:
    _ADAPTER_FACTORIES.pop(protocol, None)


def registered_protocols() -> list[EndpointProtocol]:
    return list(_ADAPTER_FACTORIES)


def _need_base_url(endpoint: ExternalEndpoint) -> str:
    if not endpoint.base_url:
        raise ValueError(
            f"[probe] Endpoint '{endpoint.name}' ({endpoint.protocol.value}) requires a base_url."
        )
    return endpoint.base_url


def _need_api_key(endpoint: ExternalEndpoint, api_key: str) -> str:
    if not api_key:
        raise ValueError(
            f"[probe] Endpoint '{endpoint.name}' ({endpoint.protocol.value}) requires an API key."
        )
    return api_key


def _timeout(endpoint: ExternalEndpoint, default: float = 300.0) -> float:
    return float((endpoint.extra_config or {}).get("timeout", default))


def _openai_adapter(endpoint: ExternalEndpoint, api_key: str) -> Any:
    from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter

    return OpenAICompatAdapter(
        base_url=_need_base_url(endpoint),
        api_key=api_key,
        timeout=_timeout(endpoint),
    )


def _gemini_adapter(endpoint: ExternalEndpoint, api_key: str) -> Any:
    from core.external_endpoints.adapters.gemini_adapter import GeminiAdapter

    return GeminiAdapter(api_key=_need_api_key(endpoint, api_key))


def _anthropic_adapter(endpoint: ExternalEndpoint, api_key: str) -> Any:
    from core.external_endpoints.adapters.anthropic_adapter import AnthropicAdapter

    return AnthropicAdapter(
        api_key=_need_api_key(endpoint, api_key),
        base_url=endpoint.base_url or "https://api.anthropic.com",
    )


def _custom_adapter(endpoint: ExternalEndpoint, api_key: str) -> Any:
    if endpoint.extra_config.get("legacy_http_tts"):
        from core.external_endpoints.adapters.custom_tts_adapter import (
            LegacyHttpTTSAdapter,
        )

        return LegacyHttpTTSAdapter(
            base_url=_need_base_url(endpoint),
            extra_config=endpoint.extra_config,
        )
    # Without a special mode a custom endpoint speaks the OpenAI-compatible dialect.
    from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter

    return OpenAICompatAdapter(
        base_url=_need_base_url(endpoint),
        api_key=api_key,
        timeout=_timeout(endpoint, 60.0),
    )


for _protocol, _factory in (
    (EndpointProtocol.OPENAI, _openai_adapter),
    (EndpointProtocol.GEMINI, _gemini_adapter),
    (EndpointProtocol.ANTHROPIC, _anthropic_adapter),
    (EndpointProtocol.CUSTOM, _custom_adapter),
):
    register_protocol(_protocol, _factory)


def get_adapter_for_endpoint(
    endpoint: ExternalEndpoint,
    api_key: str = "",
) -> "BaseProtocolAdapter":  # type: ignore[name-defined]  # noqa: F821
    """Return the adapter registered for the endpoint's protocol.

    Args:
        endpoint: The :class:`ExternalEndpoint` descriptor.
        api_key:  The decrypted API key (or empty string).

    Raises:
        ValueError: If no adapter is registered for the protocol, or the endpoint
                    lacks what its adapter requires (``base_url``, API key).
    """
    factory = _ADAPTER_FACTORIES.get(endpoint.protocol)
    if factory is None:
        raise ValueError(f"[probe] Unsupported protocol: {endpoint.protocol!r}")
    return factory(endpoint, api_key)


async def probe_endpoint(endpoint: ExternalEndpoint, api_key: str = "") -> ProbeResult:
    """Run a full probe against an external endpoint.

    Returns a :class:`ProbeResult` regardless of success or failure.  Never
    raises — errors are captured in ``ProbeResult.error_message``.
    """
    import asyncio
    import time as _time

    started = _time.monotonic()
    log_info(
        f"[probe] Probing endpoint '{endpoint.name}' "
        f"(protocol={endpoint.protocol}, base_url={endpoint.base_url!r})"
    )

    try:
        adapter = get_adapter_for_endpoint(endpoint, api_key)
    except ValueError as exc:
        log_warning(f"[probe] Cannot build adapter for '{endpoint.name}': {exc}")
        return ProbeResult(status="failed", error_message=str(exc))

    capabilities: dict[str, bool] = {}
    models: list[str] = []
    models_metadata: list[dict[str, Any]] = []
    ping_echo: str = ""
    errors: list[str] = []

    # --- Step 1: the model listing, fetched once and shared ---------------
    # Every later step used to call ``list_models()`` again, so a provider
    # whose /models takes ~40 s paid that cost three to four times per probe
    # and the whole run blew past the caller's timeout (nothing was then
    # persisted, and the model list in the WebUI never updated).
    model_infos: list[Any] = []
    models_timeout = _probe_step_timeout(
        _PROBE_MODELS_TIMEOUT_ENV, _DEFAULT_MODELS_TIMEOUT_SECONDS
    )
    try:
        model_infos = await asyncio.wait_for(
            adapter.list_models(), timeout=models_timeout
        )
        models = [m.id for m in model_infos]
        # Preserve per-model metadata (type, modalities, languages, caps) so it
        # can be persisted and used to filter engine selectors in the WebUI.
        models_metadata = [m.to_dict() for m in model_infos if getattr(m, "id", "")]
        # Derive endpoint-level capabilities as the union of its models' caps.
        # Generalizes the previous vision-only union to cortex/vox/auris/vision.
        for m in model_infos:
            for cap_name, cap_val in (m.capabilities or {}).items():
                if cap_val:
                    capabilities[cap_name] = True
    except asyncio.TimeoutError:
        message = f"models: timed out after {models_timeout:.0f}s"
        errors.append(message)
        log_warning(f"[probe] list_models timed out for '{endpoint.name}': {message}")
    except Exception as exc:
        errors.append(f"models: {exc}")
        log_warning(f"[probe] list_models failed for '{endpoint.name}': {exc}")

    # --- Step 2: capabilities and ping, concurrently, reusing the listing ---
    capabilities_timeout = _probe_step_timeout(
        _PROBE_CAPABILITIES_TIMEOUT_ENV, _DEFAULT_CAPABILITIES_TIMEOUT_SECONDS
    )
    ping_timeout = _probe_step_timeout(
        _PROBE_PING_TIMEOUT_ENV, _DEFAULT_PING_TIMEOUT_SECONDS
    )

    cap_task = asyncio.create_task(adapter.probe_capabilities(models=model_infos))
    # Prefer the endpoint's configured default_model for the ping so capacity
    # errors on a random first-in-list model don't block probing. When none is
    # configured yet, ping with the model the auto-selection will settle on
    # (ENDPOINT_MODEL_PREFERENCES), so the probe validates what will actually be
    # used instead of an arbitrary first-in-list model that may be rate-limited.
    from core.external_endpoints.model_choice import select_default_model

    ping_model = endpoint.default_model or select_default_model(model_infos)
    ping_task = asyncio.create_task(
        adapter.ping_test(model=ping_model or None, models=model_infos)
    )

    try:
        capabilities.update(
            await asyncio.wait_for(cap_task, timeout=capabilities_timeout)
        )
    except asyncio.TimeoutError:
        message = f"capabilities: timed out after {capabilities_timeout:.0f}s"
        errors.append(message)
        log_warning(f"[probe] probe_capabilities timed out for '{endpoint.name}'")
    except Exception as exc:
        errors.append(f"capabilities: {exc}")
        log_warning(f"[probe] probe_capabilities failed for '{endpoint.name}': {exc}")

    try:
        ping_ok, ping_echo = await asyncio.wait_for(ping_task, timeout=ping_timeout)
        capabilities["cortex"] = ping_ok
        log_debug(
            f"[probe] ping_test for '{endpoint.name}': ok={ping_ok} echo={ping_echo!r}"
        )
    except asyncio.TimeoutError:
        message = f"ping: timed out after {ping_timeout:.0f}s"
        errors.append(message)
        log_warning(f"[probe] ping_test timed out for '{endpoint.name}'")
        capabilities["cortex"] = False
    except Exception as exc:
        errors.append(f"ping: {exc}")
        log_warning(f"[probe] ping_test failed for '{endpoint.name}': {exc}")
        capabilities["cortex"] = False

    if not models and not any(capabilities.values()):
        # Nothing was gathered: every step failed or timed out.  Reporting
        # "success" here would let an empty result overwrite usable stored data, and
        # would call an endpoint healthy on the strength of a placeholder model the
        # adapter invents when its listing call cannot connect at all.
        return ProbeResult(
            status="failed",
            error_message="; ".join(errors)
            or "no answer from the endpoint (it did not resolve, refused the "
            "connection, or timed out before sending a response)",
        )

    log_info(
        f"[probe] '{endpoint.name}' done in {_time.monotonic() - started:.1f}s — "
        f"capabilities={capabilities}, models_count={len(models)}"
    )
    return ProbeResult(
        status="success",
        capabilities=capabilities,
        models=models,
        models_metadata=models_metadata,
        error_message="; ".join(errors),
        ping_echo=ping_echo,
    )
