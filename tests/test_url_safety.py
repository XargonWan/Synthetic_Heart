"""LLM-chosen URLs must not reach private, loopback or metadata addresses."""

from __future__ import annotations

import socket
from typing import Any

import pytest
import requests

from core import url_safety
from core.url_safety import UnsafeURLError, check_public_url, safe_get


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8000/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://192.168.1.42:14848/api/status",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "http:///nohost",
    ],
)
def test_non_public_urls_rejected(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        check_public_url(url)


def test_public_address_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0))],
    )
    check_public_url("https://example.com/page")


def test_hostname_resolving_to_private_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, 0, 0, "", ("172.18.0.3", 0))],
    )
    with pytest.raises(UnsafeURLError):
        check_public_url("http://synth-db/")


def test_redirect_to_private_host_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, *a: Any, **k: Any) -> list[Any]:
        ip = "93.184.216.34" if host == "example.com" else "127.0.0.1"
        return [(socket.AF_INET, 0, 0, "", (ip, 0))]

    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> requests.Response:
        calls.append(url)
        resp = requests.Response()
        resp.status_code = 302
        resp.headers["location"] = "http://internal.example.test/secret"
        return resp

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(url_safety.requests, "get", fake_get)
    with pytest.raises(UnsafeURLError):
        safe_get("https://example.com/")
    assert calls == ["https://example.com/"]
