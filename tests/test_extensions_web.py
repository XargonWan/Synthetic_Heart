"""Web content an extension adds: static files and forwarded HTTP requests."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions import web
from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.manifest import (
    Manifest,
    ManifestError,
    Permissions,
    Provides,
    WebProvision,
    parse_manifest,
)
from core.extensions.memory_ports import memory_ports
from tests.test_extensions_gateway import Client, Env


def _data(web_table: Any) -> dict[str, Any]:
    return {
        "extension": {"id": "dev/site", "name": "Site", "version": "1.0"},
        "compat": {"core_api": ">=0.1"},
        "provides": {"web": web_table},
    }


def _errors(web_table: Any) -> tuple[str, ...]:
    with pytest.raises(ManifestError) as info:
        parse_manifest(_data(web_table), source="test")
    return info.value.errors


class TestManifest:
    def test_a_web_provision_is_parsed(self) -> None:
        m = parse_manifest(
            _data({"static": "web", "alias": "stage", "routes": True}), source="t"
        )
        assert m.provides.web == WebProvision("web", "stage", True)

    def test_no_web_table_means_none(self) -> None:
        data = _data({"static": "web"})
        del data["provides"]
        assert parse_manifest(data, source="t").provides.web is None

    @pytest.mark.parametrize(
        "bad",
        [
            {"static": "../up"},
            {"static": "/abs"},
            {"static": "a/../b"},
            {"static": "a//b"},
            {},  # declares nothing
            {"alias": "x"},  # alias without static
            {"static": "web", "alias": "Stage"},
            {"static": "web", "alias": "a"},
            {"static": "web", "alias": "api"},
            {"static": "web", "alias": "x"},
            {"static": "web", "alias": "with/slash"},
            {"static": "web", "extra": 1},
            {"static": "web", "routes": "yes"},
        ],
    )
    def test_a_bad_declaration_is_refused(self, bad: dict[str, Any]) -> None:
        assert _errors(bad)


class TestStaticFiles:
    @pytest.fixture
    def root(self, tmp_path: Path) -> Path:
        site = tmp_path / "web"
        (site / "assets").mkdir(parents=True)
        (site / "index.html").write_text("<h1>hi</h1>")
        (site / "assets" / "app.js").write_text("1")
        (tmp_path / "secret.txt").write_text("top secret")
        return site

    def test_files_and_the_index_are_found(self, root: Path) -> None:
        assert web.resolve_static(root, "") == (root / "index.html").resolve()
        assert (
            web.resolve_static(root, "assets/app.js")
            == (root / "assets" / "app.js").resolve()
        )
        assert web.resolve_static(root, "assets") is None  # no index there

    @pytest.mark.parametrize(
        "relative",
        ["../secret.txt", "assets/../../secret.txt", "..", "a/./b", "x\x00y", "nope"],
    )
    def test_nothing_outside_the_directory_is_served(
        self, root: Path, relative: str
    ) -> None:
        assert web.resolve_static(root, relative) is None

    def test_a_symlink_out_of_the_directory_is_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        (root / "link.txt").symlink_to(tmp_path / "secret.txt")
        (root / "linkdir").symlink_to(tmp_path)
        assert web.resolve_static(root, "link.txt") is None
        assert web.resolve_static(root, "linkdir/secret.txt") is None

    def test_a_directory_whose_index_is_a_symlink_out_is_refused(
        self, root: Path, tmp_path: Path
    ) -> None:
        sub = root / "sub"
        sub.mkdir()
        (sub / "index.html").symlink_to(tmp_path / "secret.txt")
        assert web.resolve_static(root, "sub") is None

    def test_media_types(self, root: Path) -> None:
        assert web.media_type(root / "a.html").startswith("text/html")
        assert web.media_type(root / "noext") == "application/octet-stream"


class TestResponseCleaning:
    def ok(self, **kw: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "status": 201,
            "headers": {"Content-Type": "text/plain", "Set-Cookie": "a=b"},
            "body": base64.b64encode(b"hello").decode(),
        }
        base.update(kw)
        return base

    def test_a_good_answer_passes_and_cookies_are_dropped(self) -> None:
        status, headers, body = web.clean_response(self.ok())
        assert (status, body) == (201, b"hello")
        assert headers == {"content-type": "text/plain"}

    @pytest.mark.parametrize(
        "bad",
        [
            "text",
            {"status": 99},
            {"status": 600},
            {"status": True},
            {"status": "200"},
            {"body": 5},
            {"body": "***not base64***"},
            {"headers": []},
            {"headers": {"location": "x\r\nSet-Cookie: a=b"}},
            {"headers": {f"etag{i}": "x" for i in range(40)}},
            {"body": base64.b64encode(b"x" * (web.MAX_BODY + 1)).decode()},
        ],
    )
    def test_an_unacceptable_answer_is_refused(self, bad: Any) -> None:
        raw = bad if not isinstance(bad, dict) else self.ok(**bad)
        with pytest.raises(ValueError):
            web.clean_response(raw)

    def test_credentials_are_not_forwarded_to_the_extension(self) -> None:
        params = web.request_params(
            "POST",
            "a/b",
            {"q": "1"},
            {
                "Authorization": "Bearer secret",
                "Cookie": "s=1",
                "Content-Type": "application/json",
                "X-Forwarded-For": "1.2.3.4",
            },
            b"{}",
        )
        assert params["headers"] == {"content-type": "application/json"}
        assert base64.b64decode(params["body"]) == b"{}"


def manifest(
    web_provision: WebProvision | None, *caps: str, ext: str = "dev/site"
) -> Manifest:
    return Manifest(
        id=ext,
        name="site",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        provides=Provides(web=web_provision),
    )


class Rig:
    def __init__(self, env: Env, install: Path) -> None:
        self.env, self.install = env, install
        self.answers: list[dict[str, Any]] = []

    def install_dir(self, extension_id: str) -> Path:
        return self.install / extension_id

    async def connect(
        self,
        provision: WebProvision | None,
        *caps: str,
        grant: tuple[str, ...] | None = None,
        ext: str = "dev/site",
    ) -> Client:
        client = await self.env.connect(
            manifest(provision, *caps, ext=ext), grant=caps if grant is None else grant
        )
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "web.request":
                self.answers.append(dict(params))
                reply = self.reply
                if isinstance(reply, Exception):
                    raise reply
                return reply
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        return client

    reply: Any = {
        "status": 200,
        "headers": {"content-type": "application/json"},
        "body": base64.b64encode(b'{"ok":true}').decode(),
    }


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    env = Env(gateway, grants, ports)
    site = tmp_path / "dev" / "site" / "web"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>site</h1>")
    (site / "app.js").write_text("console.log(1)")
    try:
        yield Rig(env, tmp_path)
    finally:
        await env.close()


class TestTargets:
    @pytest.mark.asyncio
    async def test_a_target_needs_the_declaration_and_the_grant(self, rig: Rig) -> None:
        provision = WebProvision("web", "", True)
        await rig.connect(provision, web.CAP_STATIC, web.CAP_ROUTES)
        gateway = rig.env.gateway
        target = web.target_for(gateway, "dev/site", rig.install_dir("dev/site"))
        assert target is not None and target.static and target.routes
        assert target.root == rig.install_dir("dev/site") / "web"

    @pytest.mark.asyncio
    async def test_a_capability_not_granted_is_not_served(self, rig: Rig) -> None:
        await rig.connect(
            WebProvision("web", "", True),
            web.CAP_STATIC,
            web.CAP_ROUTES,
            grant=(web.CAP_STATIC,),
        )
        target = web.target_for(
            rig.env.gateway, "dev/site", rig.install_dir("dev/site")
        )
        assert target is not None and target.static and not target.routes

    @pytest.mark.asyncio
    async def test_a_capability_not_declared_is_not_served_even_if_granted(
        self, rig: Rig
    ) -> None:
        await rig.connect(
            WebProvision("web", "", True),
            web.CAP_STATIC,
            grant=(web.CAP_STATIC, web.CAP_ROUTES),
        )
        target = web.target_for(
            rig.env.gateway, "dev/site", rig.install_dir("dev/site")
        )
        assert target is not None and not target.routes

    @pytest.mark.asyncio
    async def test_nothing_is_served_without_a_grant_or_a_session(
        self, rig: Rig
    ) -> None:
        assert (
            web.target_for(rig.env.gateway, "dev/site", rig.install_dir("dev/site"))
            is None
        )
        await rig.connect(WebProvision("web", "", True), web.CAP_STATIC, grant=())
        assert (
            web.target_for(rig.env.gateway, "dev/site", rig.install_dir("dev/site"))
            is None
        )

    @pytest.mark.asyncio
    async def test_an_alias_belongs_to_one_running_granted_extension(
        self, rig: Rig
    ) -> None:
        await rig.connect(WebProvision("web", "stage", False), web.CAP_STATIC)
        assert web.alias_owner(rig.env.gateway, "stage") == "dev/site"
        assert web.alias_owner(rig.env.gateway, "other") is None
        await rig.connect(
            WebProvision("web", "mine", False),
            web.CAP_STATIC,
            grant=(),
            ext="dev/other",
        )
        assert web.alias_owner(rig.env.gateway, "mine") is None  # not granted


class TestForward:
    @pytest.mark.asyncio
    async def test_the_answer_comes_back_cleaned(self, rig: Rig) -> None:
        await rig.connect(WebProvision("", "", True), web.CAP_ROUTES)
        status, headers, body = await web.forward(
            rig.env.gateway, "dev/site", web.request_params("GET", "x", {}, {}, b"")
        )
        assert (status, body) == (200, b'{"ok":true}')
        assert headers == {"content-type": "application/json"}
        assert rig.answers[0]["path"] == "x"

    @pytest.mark.asyncio
    async def test_a_missing_extension_a_failure_and_a_bad_answer(
        self, rig: Rig
    ) -> None:
        params = web.request_params("GET", "x", {}, {}, b"")
        with pytest.raises(LookupError):
            await web.forward(rig.env.gateway, "dev/none", params)
        await rig.connect(WebProvision("", "", True), web.CAP_ROUTES)
        rig.reply = RpcError(ErrorCode.INTERNAL_ERROR, "boom")
        with pytest.raises(ValueError, match="failed"):
            await web.forward(rig.env.gateway, "dev/site", params)
        rig.reply = RpcError(ErrorCode.TIMEOUT, "slow")
        with pytest.raises(TimeoutError):
            await web.forward(rig.env.gateway, "dev/site", params)
        rig.reply = {"status": 700}
        with pytest.raises(ValueError, match="status"):
            await web.forward(rig.env.gateway, "dev/site", params)


class TestRouter:
    async def client(self, rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Any:
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
        app.include_router(build_router(rig.env.gateway, rig.install_dir))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    @pytest.mark.asyncio
    async def test_static_files_are_public_and_safe(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await rig.connect(WebProvision("web", "stage", False), web.CAP_STATIC)
        async with await self.client(rig, monkeypatch) as http:
            index = await http.get("/x/dev/site/")
            bare = await http.get("/x/dev/site")
            asset = await http.get("/x/dev/site/app.js")
            alias = await http.get("/stage/")
            alias_asset = await http.get("/stage/app.js")
            outside = await http.get("/x/dev/site/%2e%2e/%2e%2e/secret")
            unknown_alias = await http.get("/nothing/")
            unknown_ext = await http.get("/x/dev/other/app.js")
        assert index.status_code == 200 and "<h1>site</h1>" in index.text
        assert bare.status_code == 200
        assert (
            asset.status_code == 200
            and asset.headers["x-content-type-options"] == "nosniff"
        )
        assert alias.text == index.text and alias_asset.text == asset.text
        assert {
            outside.status_code,
            unknown_alias.status_code,
            unknown_ext.status_code,
        } == {404}

    @pytest.mark.asyncio
    async def test_routes_need_the_webui_token_and_forward(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await rig.connect(WebProvision("", "", True), web.CAP_ROUTES)
        async with await self.client(rig, monkeypatch) as http:
            denied = await http.get("/x/dev/site/api/ping")
            assert denied.status_code == 401 and rig.answers == []
            ok = await http.post(
                "/x/dev/site/api/items/1?x=2",
                headers={"x-ok": "1", "Cookie": "s=1", "Content-Type": "text/plain"},
                content=b"payload",
            )
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        [seen] = rig.answers
        assert (seen["method"], seen["path"], seen["query"]) == (
            "POST",
            "items/1",
            {"x": "2"},
        )
        assert "cookie" not in seen["headers"] and "x-ok" not in seen["headers"]
        assert base64.b64decode(seen["body"]) == b"payload"

    @pytest.mark.asyncio
    async def test_route_errors_map_to_http_statuses(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await rig.connect(WebProvision("", "", True), web.CAP_ROUTES)
        ok = {"x-ok": "1"}
        async with await self.client(rig, monkeypatch) as http:
            rig.reply = RpcError(ErrorCode.TIMEOUT, "slow")
            assert (await http.get("/x/dev/site/api/a", headers=ok)).status_code == 504
            rig.reply = {"status": 5}
            assert (await http.get("/x/dev/site/api/a", headers=ok)).status_code == 502
            big = await http.post(
                "/x/dev/site/api/a", headers=ok, content=b"x" * (web.MAX_BODY + 1)
            )
            assert big.status_code == 413
            assert (await http.get("/x/dev/none/api/a", headers=ok)).status_code == 404

    @pytest.mark.asyncio
    async def test_routes_are_off_without_the_routes_capability(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await rig.connect(WebProvision("web", "", True), web.CAP_STATIC)
        async with await self.client(rig, monkeypatch) as http:
            answer = await http.get("/x/dev/site/api/a", headers={"x-ok": "1"})
        assert answer.status_code == 404 and rig.answers == []


class TestSdk:
    def test_response_bodies_are_encoded_by_type(self) -> None:
        from synth_sdk.runtime import WebResponse

        def decode(r: WebResponse) -> tuple[dict[str, Any], bytes]:
            wire = r._wire()  # noqa: SLF001
            return wire, base64.b64decode(wire["body"])

        wire, data = decode(WebResponse(200, b"\x00\x01"))
        assert data == b"\x00\x01" and "content-type" not in wire["headers"]
        wire, data = decode(WebResponse(200, "héllo"))
        assert data == "héllo".encode() and wire["headers"]["content-type"].startswith(
            "text/plain"
        )
        wire, data = decode(WebResponse(201, {"a": [1]}, {"X-Mine": "1"}))
        assert data == b'{"a": [1]}' and wire["status"] == 201
        assert wire["headers"]["content-type"] == "application/json"
        wire, _ = decode(WebResponse(200, "x", {"Content-Type": "text/csv"}))
        assert wire["headers"]["content-type"] == "text/csv"  # the extension's wins

    @pytest.mark.asyncio
    async def test_the_handler_gets_a_request_and_must_answer_with_a_response(
        self,
    ) -> None:
        from synth_sdk.runtime import Web, WebRequest, WebResponse

        handlers: dict[str, Any] = {}

        class Ctx:
            def handle(self, method: str, handler: Any) -> None:
                handlers[method] = handler

        seen: list[WebRequest] = []

        async def good(request: WebRequest) -> WebResponse:
            seen.append(request)
            return WebResponse(200, {"echo": request.json()})

        Web(Ctx()).on_request(good)  # type: ignore[arg-type]
        params = web.request_params("POST", "p", {"q": "1"}, {}, b'{"n": 3}')
        wire = await handlers["web.request"](params)
        assert seen[0].method == "POST" and seen[0].query == {"q": "1"}
        assert base64.b64decode(wire["body"]) == b'{"echo": {"n": 3}}'

        Web(Ctx()).on_request(lambda request: "not a response")  # type: ignore[arg-type,return-value]
        with pytest.raises(TypeError):
            await handlers["web.request"](params)


async def opened(seen: list[tuple[str, Any]]) -> dict[str, Any]:
    """The ``web.ws.open`` call the extension received (the socket is accepted
    before the extension has answered, so wait for it)."""
    for _ in range(200):
        for method, params in seen:
            if method == "web.ws.open":
                return params
        await asyncio.sleep(0.02)
    raise AssertionError("the extension never saw web.ws.open")


class TestWebSockets:
    """WebSocket connections handed to the extension, over a real server."""

    @pytest_asyncio.fixture
    async def served(
        self, rig: Rig, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncIterator[dict[str, Any]]:
        import asyncio
        import socket

        import uvicorn
        from fastapi import FastAPI

        from core import webui_auth
        from core.extensions.webui_web import build_router

        monkeypatch.setattr(
            webui_auth,
            "websocket_authorized",
            lambda ws: ws.query_params.get("token") == "ok",
        )
        hub = web.WebSocketHub()
        web.register_web_api(rig.env.gateway, hub)
        app = FastAPI()
        app.include_router(build_router(rig.env.gateway, rig.install_dir, hub))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        task = asyncio.ensure_future(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        try:
            yield {"hub": hub, "url": f"ws://127.0.0.1:{port}"}
        finally:
            server.should_exit = True
            await task

    async def extension(
        self, rig: Rig, *, accept: bool = True, grant: bool = True
    ) -> tuple[Client, list[tuple[str, Any]]]:
        caps = (web.CAP_WS,)
        client = await rig.env.connect(
            manifest(WebProvision("", "", False, True), *caps),
            grant=caps if grant else (),
        )
        seen: list[tuple[str, Any]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method.startswith("web.ws."):
                seen.append((method, dict(params)))
                if method == "web.ws.open":
                    return {"ok": accept}
                return {"ok": True}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        return client, seen

    @pytest.mark.asyncio
    async def test_frames_go_both_ways_and_close_is_reported(
        self, rig: Rig, served: dict[str, Any]
    ) -> None:
        import aiohttp

        client, seen = await self.extension(rig)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                f"{served['url']}/x/dev/site/ws/live/1?token=ok&lang=it"
            ) as ws:
                opened_call = await opened(seen)
                assert opened_call["path"] == "live/1"
                assert opened_call["query"] == {"token": "ok", "lang": "it"}
                conn = opened_call["conn_id"]
                await ws.send_str("hello")
                await ws.send_bytes(b"\x00\x01")
                for _ in range(100):
                    if len([1 for m, _ in seen if m == "web.ws.message"]) == 2:
                        break
                    await asyncio.sleep(0.02)
                frames = [p for m, p in seen if m == "web.ws.message"]
                assert frames[0] == {"conn_id": conn, "text": "hello"}
                assert base64.b64decode(frames[1]["data"]) == b"\x00\x01"
                assert await client.call("web.ws.send", conn_id=conn, text="hi") == {
                    "ok": True
                }
                assert (await ws.receive()).data == "hi"
                await client.call(
                    "web.ws.send",
                    conn_id=conn,
                    data=base64.b64encode(b"\x02").decode(),
                )
                assert (await ws.receive()).data == b"\x02"
            for _ in range(100):
                if any(m == "web.ws.closed" for m, _ in seen):
                    break
                await asyncio.sleep(0.02)
        assert ("web.ws.closed", {"conn_id": conn}) in seen
        assert conn not in served["hub"].connections

    @pytest.mark.asyncio
    async def test_the_extension_can_refuse_a_connection(
        self, rig: Rig, served: dict[str, Any]
    ) -> None:
        import aiohttp

        await self.extension(rig, accept=False)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                f"{served['url']}/x/dev/site/ws/a?token=ok"
            ) as ws:
                message = await ws.receive()
                assert message.type == aiohttp.WSMsgType.CLOSE
                assert ws.close_code == 1008

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["no_token", "not_granted", "no_such_extension"])
    async def test_a_connection_that_is_not_allowed_is_refused(
        self, rig: Rig, served: dict[str, Any], case: str
    ) -> None:
        import aiohttp

        _, seen = await self.extension(rig, grant=case != "not_granted")
        target = "other" if case == "no_such_extension" else "site"
        token = "" if case == "no_token" else "?token=ok"
        async with aiohttp.ClientSession() as http:
            with pytest.raises(aiohttp.WSServerHandshakeError):
                async with http.ws_connect(
                    f"{served['url']}/x/dev/{target}/ws/a{token}"
                ):
                    pass
        assert not any(m == "web.ws.open" for m, _ in seen)

    @pytest.mark.asyncio
    async def test_the_extension_can_only_use_its_own_connections(
        self, rig: Rig, served: dict[str, Any]
    ) -> None:
        import aiohttp

        _, seen = await self.extension(rig)
        intruder = await rig.env.connect(
            manifest(WebProvision("", "", False, True), web.CAP_WS, ext="dev/evil"),
            grant=(web.CAP_WS,),
        )
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(f"{served['url']}/x/dev/site/ws/a?token=ok"):
                conn = (await opened(seen))["conn_id"]
                await intruder.expect(
                    ErrorCode.INVALID_PARAMS, "web.ws.send", conn_id=conn, text="x"
                )
                await intruder.expect(
                    ErrorCode.INVALID_PARAMS, "web.ws.close", conn_id=conn
                )

    @pytest.mark.asyncio
    async def test_send_takes_exactly_one_payload_and_valid_base64(
        self, rig: Rig, served: dict[str, Any]
    ) -> None:
        import aiohttp

        client, seen = await self.extension(rig)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(f"{served['url']}/x/dev/site/ws/a?token=ok"):
                conn = (await opened(seen))["conn_id"]
                for bad in ({}, {"text": "a", "data": "AA=="}, {"data": "***"}):
                    await client.expect(
                        ErrorCode.INVALID_PARAMS, "web.ws.send", conn_id=conn, **bad
                    )

    @pytest.mark.asyncio
    async def test_the_extension_can_close_a_connection(
        self, rig: Rig, served: dict[str, Any]
    ) -> None:
        import aiohttp

        client, seen = await self.extension(rig)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                f"{served['url']}/x/dev/site/ws/a?token=ok"
            ) as ws:
                conn = (await opened(seen))["conn_id"]
                await client.call("web.ws.close", conn_id=conn, code=4001)
                assert (await ws.receive()).type == aiohttp.WSMsgType.CLOSE
                assert ws.close_code == 4001

    @pytest.mark.asyncio
    async def test_an_extension_holds_a_bounded_number_of_connections(
        self, rig: Rig, served: dict[str, Any]
    ) -> None:
        import aiohttp

        await self.extension(rig)
        async with aiohttp.ClientSession() as http:
            sockets = []
            try:
                for _ in range(web.MAX_WS_PER_EXTENSION):
                    sockets.append(
                        await http.ws_connect(
                            f"{served['url']}/x/dev/site/ws/a?token=ok"
                        )
                    )
                extra = await http.ws_connect(
                    f"{served['url']}/x/dev/site/ws/a?token=ok"
                )
                assert (await extra.receive()).type == aiohttp.WSMsgType.CLOSE
                assert extra.close_code == 1013
            finally:
                for socket_ in sockets:
                    await socket_.close()

    @pytest.mark.asyncio
    async def test_connections_end_when_the_extension_goes_away(
        self, rig: Rig, served: dict[str, Any]
    ) -> None:
        import aiohttp

        await self.extension(rig)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(
                f"{served['url']}/x/dev/site/ws/a?token=ok"
            ) as ws:
                assert served["hub"].count("dev/site") == 1
                await served["hub"].drop_extension("dev/site")
                assert (await ws.receive()).type == aiohttp.WSMsgType.CLOSE
                assert ws.close_code == 1012
