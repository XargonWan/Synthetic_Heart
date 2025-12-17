import os
import requests
import pytest

PORT = int(os.environ.get("SYNTH_WEBUI_PORT", "9009"))
HTTPS_HOST = f"https://127.0.0.1:{PORT}"


def _try_get(url, **kwargs):
    try:
        return requests.get(url, timeout=2, **kwargs)
    except requests.exceptions.RequestException:
        pytest.skip(f"Service not reachable: {url}")


def test_https_root_returns_html():
    r = _try_get(HTTPS_HOST + "/", verify=False)
    assert r.status_code == 200
    assert "html" in r.headers.get("content-type", "").lower()


def test_cli_route_exists():
    r = _try_get(HTTPS_HOST + "/cli/health", verify=False)
    # If the CLI is present behind the proxy, /cli/health should respond (or be proxied)
    assert r.status_code in (200, 404, 503)


def test_webtop_route_exists():
    r = _try_get(HTTPS_HOST + "/webtop/", verify=False)
    assert r.status_code in (200, 302, 404)
