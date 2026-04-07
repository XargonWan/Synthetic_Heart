# core/external_endpoints/probe.py
"""Auto-probe an external endpoint and return a ProbeResult.

The probe:
1. Selects the correct adapter for the endpoint's protocol.
2. Calls ``adapter.probe_capabilities()`` to detect supported subsystems.
3. Calls ``adapter.list_models()`` to collect available model names.
4. Calls ``adapter.ping_test()`` to verify cortex connectivity and obtain a
   reply echo.  The ping result sets ``capabilities["cortex"]`` and is
   stored in ``ProbeResult.ping_echo``.
5. Returns a :class:`ProbeResult` with the findings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.logging_utils import log_debug, log_warning


@dataclass
class ProbeResult:
    """Result of probing an external endpoint."""

    status: str  # 'success' | 'failed'
    capabilities: dict[str, bool] = field(default_factory=dict)
    models: list[str] = field(default_factory=list)
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
        timeout = float(
            (endpoint.extra_config or {}).get("timeout", 60.0)
        )
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

            timeout = float(
                (endpoint.extra_config or {}).get("timeout", 60.0)
            )
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
    log_debug(
        f"[probe] Probing endpoint '{endpoint.name}' (protocol={endpoint.protocol})"
    )

    try:
        adapter = get_adapter_for_endpoint(endpoint, api_key)
    except ValueError as exc:
        log_warning(f"[probe] Cannot build adapter for '{endpoint.name}': {exc}")
        return ProbeResult(status="failed", error_message=str(exc))

    # --- Gather capabilities, models, and ping concurrently ---
    import asyncio

    cap_task = asyncio.create_task(adapter.probe_capabilities())
    model_task = asyncio.create_task(adapter.list_models())
    # Ping a first available model; we won't know it yet, so use None (→ "default")
    ping_task = asyncio.create_task(adapter.ping_test())

    capabilities: dict[str, bool] = {}
    models: list[str] = []
    ping_echo: str = ""
    errors: list[str] = []

    try:
        capabilities = await cap_task
    except Exception as exc:
        errors.append(f"capabilities: {exc}")
        log_warning(f"[probe] probe_capabilities failed for '{endpoint.name}': {exc}")

    try:
        model_infos = await model_task
        models = [m.id for m in model_infos]
    except Exception as exc:
        errors.append(f"models: {exc}")
        log_warning(f"[probe] list_models failed for '{endpoint.name}': {exc}")

    try:
        ping_ok, ping_echo = await ping_task
        capabilities["cortex"] = ping_ok
        log_debug(
            f"[probe] ping_test for '{endpoint.name}': ok={ping_ok} echo={ping_echo!r}"
        )
    except Exception as exc:
        errors.append(f"ping: {exc}")
        log_warning(f"[probe] ping_test failed for '{endpoint.name}': {exc}")
        capabilities["cortex"] = False

    if not capabilities and not models:
        return ProbeResult(
            status="failed",
            error_message="; ".join(errors) or "No data returned",
        )

    log_debug(
        f"[probe] '{endpoint.name}' → capabilities={capabilities}, "
        f"models_count={len(models)}, ping_echo={ping_echo!r}"
    )
    return ProbeResult(
        status="success",
        capabilities=capabilities,
        models=models,
        error_message="; ".join(errors),
        ping_echo=ping_echo,
    )
