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


def get_adapter_for_endpoint(
    endpoint: ExternalEndpoint,
    api_key: str = "",
) -> "BaseProtocolAdapter":  # type: ignore[name-defined]  # noqa: F821
    """Return the appropriate protocol adapter for the given endpoint.

    Args:
        endpoint: The :class:`ExternalEndpoint` descriptor.
        api_key:  The decrypted API key (or empty string).

    Returns:
        A :class:`BaseProtocolAdapter` instance ready to use.

    Raises:
        ValueError: If the protocol is unsupported or ``base_url`` is missing
                    when required.
    """
    from core.external_endpoints.adapters.base import BaseProtocolAdapter  # noqa: F401

    proto = endpoint.protocol

    if proto == EndpointProtocol.OPENAI:
        from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter

        if not endpoint.base_url:
            raise ValueError(
                f"[probe] Endpoint '{endpoint.name}' (openai) requires a base_url."
            )
        timeout = float((endpoint.extra_config or {}).get("timeout", 300.0))
        return OpenAICompatAdapter(
            base_url=endpoint.base_url,
            api_key=api_key,
            timeout=timeout,
        )

    if proto == EndpointProtocol.GEMINI:
        from core.external_endpoints.adapters.gemini_adapter import GeminiAdapter

        if not api_key:
            raise ValueError(
                f"[probe] Endpoint '{endpoint.name}' (gemini) requires an API key."
            )
        return GeminiAdapter(api_key=api_key)

    if proto == EndpointProtocol.ANTHROPIC:
        from core.external_endpoints.adapters.anthropic_adapter import AnthropicAdapter

        if not api_key:
            raise ValueError(
                f"[probe] Endpoint '{endpoint.name}' (anthropic) requires an API key."
            )
        base_url = endpoint.base_url or "https://api.anthropic.com"
        return AnthropicAdapter(api_key=api_key, base_url=base_url)

    if proto == EndpointProtocol.HARMONY:
        from core.external_endpoints.adapters.harmony_ai_adapter import (
            HarmonyAIAdapter,
        )

        if not endpoint.base_url:
            raise ValueError(
                f"[probe] Endpoint '{endpoint.name}' (harmony) requires a base_url."
            )
        timeout = float((endpoint.extra_config or {}).get("timeout", 300.0))
        return HarmonyAIAdapter(
            base_url=endpoint.base_url,
            api_key=api_key,
            timeout=timeout,
        )

    if proto == EndpointProtocol.FISH:
        from core.external_endpoints.adapters.fish_audio_adapter import (
            DEFAULT_BASE_URL,
            FishAudioAdapter,
        )

        if not api_key:
            raise ValueError(
                f"[probe] Endpoint '{endpoint.name}' (fish) requires an API key."
            )
        return FishAudioAdapter(
            base_url=endpoint.base_url or DEFAULT_BASE_URL,
            api_key=api_key,
            extra_config=endpoint.extra_config,
        )

    if proto == EndpointProtocol.CUSTOM:
        if endpoint.extra_config.get("legacy_http_tts"):
            from core.external_endpoints.adapters.custom_tts_adapter import (
                LegacyHttpTTSAdapter,
            )

            if not endpoint.base_url:
                raise ValueError(
                    f"[probe] Endpoint '{endpoint.name}' (custom) requires a base_url."
                )
            return LegacyHttpTTSAdapter(
                base_url=endpoint.base_url,
                extra_config=endpoint.extra_config,
            )

        # Fall back to OpenAI-compatible if a base_url is provided
        if endpoint.base_url:
            from core.external_endpoints.adapters.openai_compat import (
                OpenAICompatAdapter,
            )

            timeout = float((endpoint.extra_config or {}).get("timeout", 60.0))
            return OpenAICompatAdapter(
                base_url=endpoint.base_url,
                api_key=api_key,
                timeout=timeout,
            )
        raise ValueError(
            f"[probe] Endpoint '{endpoint.name}' (custom) requires a base_url."
        )

    raise ValueError(f"[probe] Unsupported protocol: {proto!r}")


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
