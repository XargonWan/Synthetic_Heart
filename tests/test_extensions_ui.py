"""The fixed WebUI slots extensions put windows in: activity, logs, more."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from core.extensions import ui, web
from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.manifest import (
    Manifest,
    ManifestError,
    Permissions,
    Provides,
    UiEntry,
    WebProvision,
    parse_manifest,
)
from core.extensions.memory_ports import memory_ports
from tests.test_extensions_gateway import Env


def _errors(entries: Any, web_table: Any = None) -> tuple[str, ...]:
    data: dict[str, Any] = {
        "extension": {"id": "dev/win", "name": "Win", "version": "1.0"},
        "compat": {"core_api": ">=0.1"},
        "provides": {
            "ui": entries,
            "web": {"static": "web"} if web_table is None else web_table,
        },
    }
    with pytest.raises(ManifestError) as info:
        parse_manifest(data, source="t")
    return info.value.errors


class TestManifest:
    def test_windows_are_parsed(self) -> None:
        data = {
            "extension": {"id": "dev/win", "name": "Win", "version": "1.0"},
            "compat": {"core_api": ">=0.1"},
            "provides": {
                "web": {"static": "web", "same_origin": True},
                "ui": [
                    {"slot": "more", "id": "dash", "title": "Dashboard", "icon": "🎛️"},
                    {
                        "slot": "activity",
                        "id": "radio",
                        "title": "Radio",
                        "page": "r.html",
                    },
                ],
            },
        }
        provides = parse_manifest(data, source="t").provides
        assert provides.ui == (
            UiEntry("more", "dash", "Dashboard", "🎛️", "index.html"),
            UiEntry("activity", "radio", "Radio", "", "r.html"),
        )
        assert provides.web is not None and provides.web.same_origin is True

    @pytest.mark.parametrize(
        "bad",
        [
            {"slot": "top", "id": "x", "title": "X"},  # not a slot
            {"slot": "more", "id": "X!", "title": "X"},
            {"slot": "more", "id": "x", "title": ""},
            {"slot": "more", "id": "x", "title": "t" * 41},
            {"slot": "more", "id": "x", "title": "X", "icon": "123456789"},
            {"slot": "more", "id": "x", "title": "X", "page": "../up.html"},
            {"slot": "more", "id": "x", "title": "X", "page": "/abs.html"},
            {"slot": "more", "id": "x", "title": "X", "page": "a/../b.html"},
            {"slot": "more", "id": "x", "title": "X", "extra": 1},
            {"id": "x", "title": "X"},
        ],
    )
    def test_a_bad_window_is_refused(self, bad: dict[str, Any]) -> None:
        assert _errors([bad])

    def test_windows_need_static_files_to_show(self) -> None:
        errors = _errors(
            [{"slot": "more", "id": "x", "title": "X"}], web_table={"routes": True}
        )
        assert any("provides.web.static" in e for e in errors)

    def test_a_window_is_not_declared_twice_and_not_too_many(self) -> None:
        one = {"slot": "more", "id": "x", "title": "X"}
        assert any("twice" in e for e in _errors([one, one]))
        many = [{"slot": "more", "id": f"w{i}", "title": "W"} for i in range(9)]
        assert any("at most" in e for e in _errors(many))

    def test_ui_must_be_a_list_of_tables(self) -> None:
        assert _errors("more")


def manifest(
    *windows: UiEntry, same_origin: bool = False, ext: str = "dev/win"
) -> Manifest:
    caps = (web.CAP_STATIC, web.CAP_ORIGIN)
    return Manifest(
        id=ext,
        name="win",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        provides=Provides(ui=windows, web=WebProvision("web", same_origin=same_origin)),
    )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    e = Env(gateway, grants, ports)
    site = tmp_path / "dev" / "win" / "web"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>window</h1>")
    try:
        yield e
    finally:
        await e.close()


DASH = UiEntry("more", "dash", "Dashboard", "🎛️")
RADIO = UiEntry("activity", "radio", 'Radio <b>"live"</b>', "📻", "radio.html")
LOGS = UiEntry("logs", "trace", "Trace")


class TestEntries:
    @pytest.mark.asyncio
    async def test_only_granted_running_extensions_are_listed(self, env: Env) -> None:
        await env.connect(manifest(DASH, RADIO, LOGS), grant=(web.CAP_STATIC,))
        listed = ui.entries(env.gateway)
        assert [(e["slot"], e["id"]) for e in listed] == [
            ("more", "dash"),
            ("activity", "radio"),
            ("logs", "trace"),
        ]
        assert listed[0]["url"] == "/x/dev/win/index.html"
        assert listed[1]["url"] == "/x/dev/win/radio.html"
        assert all(e["same_origin"] is False for e in listed)

    @pytest.mark.asyncio
    async def test_without_the_static_grant_nothing_is_listed(self, env: Env) -> None:
        await env.connect(manifest(DASH), grant=())
        assert ui.entries(env.gateway) == []

    @pytest.mark.asyncio
    async def test_no_extension_no_entries(self, env: Env) -> None:
        assert ui.entries(env.gateway) == []

    @pytest.mark.asyncio
    async def test_same_origin_needs_the_declaration_and_the_grant(
        self, env: Env
    ) -> None:
        await env.connect(manifest(DASH, same_origin=True), grant=(web.CAP_STATIC,))
        assert ui.entries(env.gateway)[0]["same_origin"] is False
        await env.connect(
            manifest(DASH, same_origin=True, ext="dev/trusted"),
            grant=(web.CAP_STATIC, web.CAP_ORIGIN),
        )
        flags = {e["extension_id"]: e["same_origin"] for e in ui.entries(env.gateway)}
        assert flags == {"dev/trusted": True, "dev/win": False}
        await env.connect(
            manifest(DASH, same_origin=False, ext="dev/humble"),
            grant=(web.CAP_STATIC, web.CAP_ORIGIN),
        )
        flags = {e["extension_id"]: e["same_origin"] for e in ui.entries(env.gateway)}
        assert flags["dev/humble"] is False  # granted, but never declared


class TestSectionTabs:
    @pytest.mark.asyncio
    async def test_activity_and_logs_get_their_own_markup(self, env: Env) -> None:
        await env.connect(manifest(DASH, RADIO, LOGS), grant=(web.CAP_STATIC,))
        [radio] = ui.section_tabs(env.gateway, "history")
        assert 'class="sub-nav-btn"' in radio["button_html"]
        assert 'id="subtab-ext-dev-win-radio"' in radio["panel_html"]
        [trace] = ui.section_tabs(env.gateway, "logs")
        assert 'class="logs-subnav-btn"' in trace["button_html"]
        assert 'data-logs-subtab="ext-dev-win-trace"' in trace["panel_html"]
        assert ui.section_tabs(env.gateway, "settings") == []

    @pytest.mark.asyncio
    async def test_an_extensions_text_is_never_markup(self, env: Env) -> None:
        await env.connect(manifest(RADIO), grant=(web.CAP_STATIC,))
        [tab] = ui.section_tabs(env.gateway, "history")
        html = tab["button_html"] + tab["panel_html"]
        assert "<b>" not in html and "&lt;b&gt;" in html and "&quot;live&quot;" in html

    @pytest.mark.asyncio
    async def test_the_frame_is_sandboxed_unless_same_origin_is_granted(
        self, env: Env
    ) -> None:
        await env.connect(manifest(RADIO), grant=(web.CAP_STATIC,))
        [tab] = ui.section_tabs(env.gateway, "history")
        assert "allow-same-origin" not in tab["panel_html"]
        assert 'loading="lazy"' in tab["panel_html"]
        await env.connect(
            manifest(RADIO, same_origin=True, ext="dev/trusted"),
            grant=(web.CAP_STATIC, web.CAP_ORIGIN),
        )
        trusted = [
            t
            for t in ui.section_tabs(env.gateway, "history")
            if "dev-trusted" in t["tab_id"]
        ]
        assert "allow-same-origin" in trusted[0]["panel_html"]


class TestRoutes:
    async def client(
        self, env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Any:
        import httpx
        from fastapi import FastAPI

        from core import webui_auth
        from core.extensions.webui_web import build_router

        monkeypatch.setattr(
            webui_auth,
            "request_authorized",
            lambda request: request.headers.get("x-ok") == "1",
        )
        app = FastAPI()
        app.include_router(build_router(env.gateway, lambda ext: tmp_path / ext))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        )

    @pytest.mark.asyncio
    async def test_the_list_needs_the_webui_token(
        self, env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await env.connect(manifest(DASH), grant=(web.CAP_STATIC,))
        async with await self.client(env, tmp_path, monkeypatch) as http:
            assert (await http.get("/api/extensions/ui")).status_code == 401
            ok = await http.get("/api/extensions/ui", headers={"x-ok": "1"})
        assert ok.status_code == 200
        assert [e["id"] for e in ok.json()["entries"]] == ["dash"]

    @pytest.mark.asyncio
    async def test_pages_carry_a_sandbox_policy_unless_same_origin(
        self, env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await env.connect(manifest(DASH), grant=(web.CAP_STATIC,))
        site = tmp_path / "dev" / "trusted" / "web"
        site.mkdir(parents=True)
        (site / "index.html").write_text("<h1>trusted</h1>")
        await env.connect(
            manifest(DASH, same_origin=True, ext="dev/trusted"),
            grant=(web.CAP_STATIC, web.CAP_ORIGIN),
        )
        async with await self.client(env, tmp_path, monkeypatch) as http:
            plain = await http.get("/x/dev/win/index.html")
            trusted = await http.get("/x/dev/trusted/index.html")
        assert plain.status_code == 200
        assert plain.headers["content-security-policy"] == web.SANDBOX_CSP
        assert "allow-same-origin" not in web.SANDBOX_CSP
        assert trusted.status_code == 200
        assert "content-security-policy" not in trusted.headers

    @pytest.mark.asyncio
    async def test_declaring_same_origin_without_the_grant_stays_sandboxed(
        self, env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await env.connect(manifest(DASH, same_origin=True), grant=(web.CAP_STATIC,))
        target = web.target_for(env.gateway, "dev/win", tmp_path / "dev" / "win")
        assert target is not None and target.same_origin is False
        async with await self.client(env, tmp_path, monkeypatch) as http:
            page = await http.get("/x/dev/win/index.html")
        assert page.headers["content-security-policy"] == web.SANDBOX_CSP


class TestWebUiIntegration:
    @pytest.mark.asyncio
    async def test_the_sections_are_served_with_the_extensions_tabs(
        self, env: Env
    ) -> None:
        from starlette.testclient import TestClient

        from core.webui import SynthWebUIInterface

        await env.connect(manifest(RADIO, LOGS), grant=(web.CAP_STATIC,))
        webui = SynthWebUIInterface(autostart=False)
        webui.register_section_tab_provider(
            lambda section: ui.section_tabs(env.gateway, section)
        )
        client = TestClient(webui.app)
        history = client.get("/templates/history.html").text
        logs = client.get("/templates/logs.html").text
        skins = client.get("/templates/skins.html").text
        # Radio is in Activity, and inside the sub-navigation and the panel container.
        nav_end = history.index("</nav>")
        assert history.index('data-subtab="ext-dev-win-radio"') < nav_end
        assert history.index('id="subtab-ext-dev-win-radio"') < history.index(
            "<!-- .sub-tabs-container -->"
        )
        # Trace is in Logs, in its sub-navigation and its panels.
        assert logs.index('data-logs-subtab="ext-dev-win-trace"') < logs.index("</nav>")
        assert logs.index('id="logs-panel-ext-dev-win-trace"') < logs.index(
            "<!-- .logs-subpanels -->"
        )
        assert "ext-dev-win" not in skins
        assert "ext-dev-win-trace" not in history  # a logs window is not in Activity
