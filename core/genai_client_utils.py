"""Helpers for safe google-genai client lifecycle handling.

The google-genai SDK can raise an unhandled task exception during client
cleanup on some versions when ``BaseApiClient.aclose()`` runs before an async
HTTP client was created. We defensively attach a no-op async client so SDK
shutdown paths remain safe.
"""

from __future__ import annotations

from typing import Any


class _NoopAsyncHttpClient:
    async def aclose(self) -> None:
        return


def harden_genai_client_for_async_close(client: Any) -> Any:
    """Ensure google-genai BaseApiClient has a safe async close target.

    Returns the same client for convenient inline usage.
    """
    try:
        api_client = getattr(client, "_api_client", None)
        if api_client is None:
            return client

        async_httpx_client = getattr(api_client, "_async_httpx_client", None)
        if async_httpx_client is None:
            setattr(api_client, "_async_httpx_client", _NoopAsyncHttpClient())
    except Exception:
        # Best-effort hardening only; never break caller flow.
        return client

    return client
