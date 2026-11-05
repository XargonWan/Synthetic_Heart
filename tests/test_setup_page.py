"""Tests for the first-run setup page and the redirect that offers it.

The page is the only thing a native install asks the user, so it has to render
without a browser, without a database, and without any external endpoint being
configured. The redirect must also be conservative: pushing an existing
deployment at a setup page would be worse than never showing it.
"""

from __future__ import annotations

import asyncio
import re
import types
from pathlib import Path

import pytest

from core import webui as webui_module

REPO_ROOT = Path(__file__).resolve().parents[1]


def _renderer():
    """Call the renderer without building the whole WebUI interface.

    ``_render_setup`` only touches ``self.logo_url``, so a stub is enough and
    the test stays fast and database-free.
    """
    stub = types.SimpleNamespace(logo_url="/static/synth_logo_bg.png")
    return webui_module.SynthWebUIInterface._render_setup(stub)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_page_renders() -> None:
    html = _renderer()
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_no_placeholder_survives() -> None:
    html = _renderer()
    leftovers = [
        token for token in ("%%", "%%BRAND_NAME%%", "%%TZ_OPTIONS%%") if token in html
    ]
    assert leftovers == []


def test_page_asks_for_the_things_the_installer_deliberately_skips() -> None:
    """Persona, household, and engine are exactly what the page is for."""
    html = _renderer()
    for config_key in (
        "SYNTH_NAME",
        "TRAINER_NAME",
        "SYNTH_PROFILE",
        "TZ",
        "PROMPT_LOCATION",
        "SCENE_NOTE",
        "PROJECT_DEFAULT_LANGUAGE",
        "BASE_CORTEX",
        "SETUP_COMPLETED",
    ):
        assert config_key in html, f"{config_key} is never saved by the page"


def test_timezone_and_language_dropdowns_are_populated() -> None:
    html = _renderer()
    assert html.count("<option") > 100, (
        "the timezone list should be the full IANA catalogue"
    )
    assert 'value="en"' in html, "English must always be offered"


def test_uses_the_configured_accent_colour() -> None:
    html = _renderer()
    assert "--accent: #6bfefe" in html or "--accent: rgb" in html


def test_page_is_self_contained() -> None:
    """No CDN, no build step: it must work offline on a fresh install."""
    html = _renderer()
    for pattern in ("http://", "https://", 'src="//'):
        # The only absolute URL allowed is the provider API addresses the user
        # types in themselves, which appear in placeholders.
        assert pattern not in html.replace("https://api.openai.com", "")


# ---------------------------------------------------------------------------
# the redirect
# ---------------------------------------------------------------------------


def _stub():
    """A stand-in for the WebUI that borrows the real gate methods.

    ``_setup_completed`` is looked up on the class at call time, so the
    monkeypatches below take effect without building the whole interface.
    """
    stub = types.SimpleNamespace()
    stub._setup_completed = lambda: webui_module.SynthWebUIInterface._setup_completed(
        stub
    )
    return stub


def _pending() -> bool:
    return asyncio.run(webui_module.SynthWebUIInterface._first_run_pending(_stub()))


def test_no_redirect_once_the_page_was_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: True
    )
    assert _pending() is False


def test_no_redirect_when_an_endpoint_is_already_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing deployment must never be pushed at a setup page."""

    class _Registry:
        async def list_endpoints(self, enabled_only: bool = False):
            return [object()]

    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: False
    )
    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry",
        lambda: _Registry(),
    )
    assert _pending() is False


def test_a_local_request_on_a_fresh_install_is_offered_the_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive case, which is the entire point of the gate.

    It was never covered: every test here asserted a decline, so a gate that
    declined in every situation looked fully tested.
    """

    class _EmptyRegistry:
        async def list_endpoints(self, enabled_only: bool = False):
            return []

    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: False
    )
    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry",
        lambda: _EmptyRegistry(),
    )
    assert _pending() is True


class _Endpoint:
    """An endpoint with just the fields the gate reads."""

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        probe_status: str | None = "never",
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        if probe_status is not None:
            self.probe_status = probe_status


def _pending_with_endpoints(monkeypatch: pytest.MonkeyPatch, endpoints: list) -> bool:
    class _Registry:
        async def list_endpoints(self, enabled_only: bool = False):
            return endpoints

    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: False
    )
    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry",
        lambda: _Registry(),
    )
    return _pending()


def test_a_shipped_container_preset_does_not_retire_the_setup_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression that shipped: a clean native install met no setup page.

    A fresh database seeds an enabled endpoint for the container build, whose host
    exists only inside Docker, with no key and ``probe_status='never'``. Counting
    that as "configured" both retired this page and left that preset as the only
    engine, so the first thing a new user saw was an avatar scene and a connection
    error. Measured on a fresh Debian VM, that hostname even resolves, so the probe
    verdict - not reachability - is the signal that works.
    """
    endpoints = [
        _Endpoint(base_url="http://synth-zen-llm-engine:8000", probe_status="never")
    ]
    assert _pending_with_endpoints(monkeypatch, endpoints) is True


def test_an_endpoint_that_has_answered_a_probe_retires_the_setup_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local keyless engine that works must not keep sending anyone to setup."""
    endpoints = [_Endpoint(base_url="http://127.0.0.1:11434", probe_status="success")]
    assert _pending_with_endpoints(monkeypatch, endpoints) is False


def test_a_preset_that_has_only_ever_failed_still_offers_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that failed is not a working engine, and setup is where it is fixed."""
    assert (
        _pending_with_endpoints(monkeypatch, [_Endpoint(probe_status="failed")]) is True
    )


def test_an_endpoint_with_an_api_key_retires_the_setup_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key the user typed is their own endpoint, whatever the probe says."""
    endpoints = [
        _Endpoint(
            base_url="http://10.9.9.9:8000", api_key="sk-real", probe_status="never"
        )
    ]
    assert _pending_with_endpoints(monkeypatch, endpoints) is False


def test_an_endpoint_we_cannot_inspect_retires_the_setup_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe_status at all is doubt, and doubt must not redirect anyone."""
    endpoints = [_Endpoint(base_url="http://127.0.0.1:11434", probe_status=None)]
    assert _pending_with_endpoints(monkeypatch, endpoints) is False


def test_one_real_endpoint_is_enough_among_several_presets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoints = [
        _Endpoint(base_url="http://synth-zen-llm-engine:8000", probe_status="never"),
        _Endpoint(base_url="http://192.168.1.13:8000", probe_status="success"),
    ]
    assert _pending_with_endpoints(monkeypatch, endpoints) is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("False", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),
        (None, False),
        (False, False),
        ("True", True),
        ("true", True),
        ("1", True),
        (True, True),
    ],
)
def test_a_textual_flag_is_read_as_a_boolean(raw: object, expected: bool) -> None:
    """`bool("False")` is True, which would retire the page forever, silently."""
    assert webui_module.as_flag(raw) is expected


def _root_stub(monkeypatch: pytest.MonkeyPatch, *, local: bool, pending: bool):
    """A WebUI stub able to run the real root handler.

    The gate methods are bound onto the stub instance, not the class: the stub is a
    plain namespace rather than an instance, so a class-level patch would not be
    visible to it. ``_first_run_pending`` is called with no arguments by the handler.
    """
    stub = _stub()
    stub._render_index = lambda: "<html>landing page</html>"
    stub._is_local_client = lambda request: local

    async def _pending():
        return pending

    stub._first_run_pending = _pending
    return stub


def test_the_root_route_redirects_a_local_browser_to_the_setup_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behaviour the middleware must not bypass: a redirect, not the page."""
    stub = _root_stub(monkeypatch, local=True, pending=True)
    request = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
    response = asyncio.run(webui_module.SynthWebUIInterface.index(stub, request))
    assert response.status_code == 307
    assert response.headers["location"] == "/setup"


def test_an_established_install_gets_the_landing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _root_stub(monkeypatch, local=True, pending=False)
    request = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"))
    response = asyncio.run(webui_module.SynthWebUIInterface.index(stub, request))
    assert response.status_code == 200
    assert b"landing page" in response.body


def test_a_remote_browser_gets_the_landing_page_even_when_the_gate_would_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _root_stub(monkeypatch, local=False, pending=True)
    request = types.SimpleNamespace(client=types.SimpleNamespace(host="192.168.1.50"))
    response = asyncio.run(webui_module.SynthWebUIInterface.index(stub, request))
    assert response.status_code == 200


def test_the_root_middleware_does_not_bypass_the_first_run_gate() -> None:
    """The root middleware must hand off to the handler, not serve the page itself.

    A middleware runs before routing, so a root request it answers itself never
    reaches the root route - and the first-run redirect lives in that route. That
    is exactly what happened on a live install: the middleware logged
    "intercepting root request" four times and `index()` was never called at all,
    so the gate was dead code and the user met the avatar scene.
    """
    source = (REPO_ROOT / "core" / "webui.py").read_text(encoding="utf-8")
    middleware = re.search(
        r"class _IndexMiddleware.*?(?=\n\s*self\.app\.add_middleware)",
        source,
        re.DOTALL,
    )
    assert middleware, "the root middleware was removed or renamed"
    body = middleware.group(0)
    assert "await self.index(request)" in body, (
        "the middleware must delegate to the root handler, or the first-run "
        "redirect in it can never run"
    )


def test_only_a_local_browser_is_ever_redirected() -> None:
    """A remote browser must not be sent to a page about this machine."""
    stub = _stub()
    for host in ("127.0.0.1", "::1", "localhost"):
        request = types.SimpleNamespace(client=types.SimpleNamespace(host=host))
        assert webui_module.SynthWebUIInterface._is_local_client(stub, request) is True
    for host in ("192.168.1.50", "10.0.0.7", ""):
        request = types.SimpleNamespace(client=types.SimpleNamespace(host=host))
        assert webui_module.SynthWebUIInterface._is_local_client(stub, request) is False
    # A request with no client at all must decline rather than raise.
    assert (
        webui_module.SynthWebUIInterface._is_local_client(stub, types.SimpleNamespace())
        is False
    )


def test_no_redirect_when_the_registry_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doubt resolves to 'do not redirect'."""
    monkeypatch.setattr(
        webui_module.SynthWebUIInterface, "_setup_completed", lambda self: False
    )

    def boom():
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry", boom
    )
    assert _pending() is False
