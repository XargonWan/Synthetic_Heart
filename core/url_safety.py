"""Outbound-URL safety for fetches whose target is chosen by an LLM or a user.

Blocks loopback, private, link-local, multicast and otherwise non-public
addresses so a prompt-injected URL cannot reach ``synth-db``, the Zen engine,
``host.docker.internal`` or a cloud metadata endpoint. Every redirect hop is
re-validated. Resolution happens before the request, so DNS rebinding between
check and connect is not fully excluded; this removes the practical attacks.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse

import requests

MAX_REDIRECTS = 4


class UnsafeURLError(ValueError):
    """The URL is not a public http(s) destination."""


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped  # type: ignore[assignment]
    return ip.is_global and not ip.is_multicast


def check_public_url(url: str) -> None:
    """Raise :class:`UnsafeURLError` unless *url* is http(s) to a public host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURLError("only http(s) URLs with a host are allowed")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host: {exc}") from exc
    addresses = {str(info[4][0]) for info in infos}
    if not addresses or not all(_is_public(a) for a in addresses):
        raise UnsafeURLError("host resolves to a non-public address")


def safe_get(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10,
    max_redirects: int = MAX_REDIRECTS,
    **kwargs: Any,
) -> requests.Response:
    """``requests.get`` that validates the URL and each redirect target."""
    current = url
    for _ in range(max_redirects + 1):
        check_public_url(current)
        response = requests.get(
            current,
            headers=dict(headers or {}),
            timeout=timeout,
            allow_redirects=False,
            **kwargs,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            if not location:
                return response
            current = urljoin(current, location)
            continue
        return response
    raise UnsafeURLError("too many redirects")
