"""Declarative setup panels: what an extension may show, and how the WebUI reaches it."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.manifest import (
    InterfaceProvision,
    Manifest,
    Permissions,
    Provides,
)
from core.extensions.memory_ports import memory_ports
from core.extensions.panels import (
    CAP_PANEL,
    MAX_BLOCKS,
    PanelStore,
    clean_values,
    register_panel_api,
    run_action,
    validate_state,
)
from tests.test_extensions_gateway import Client, Env

LOGIN = {
    "title": "Account login",
    "blocks": [
        {"type": "text", "text": "Not logged in."},
        {"type": "error", "text": "Wrong code."},
        {
            "type": "form",
            "action": "verify_code",
            "submit": "Verify",
            "fields": [
                {"name": "code", "label": "Code", "kind": "code", "value": "1234"},
                {"name": "phone", "label": "Phone", "kind": "tel", "value": "+39 1"},
            ],
        },
        {"type": "button", "action": "logout", "label": "Log out"},
    ],
}


def manifest() -> Manifest:
    return Manifest(
        id="dev/chat",
        name="dev/chat",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=(CAP_PANEL,)),
        provides=Provides(interfaces=(InterfaceProvision(id="chat"),)),
    )


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[tuple[Env, PanelStore, Client]]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    store = PanelStore()
    register_panel_api(gateway, store)
    env = Env(gateway, grants, ports)
    client = await env.connect(manifest(), grant=(CAP_PANEL,))
    try:
        yield env, store, client
    finally:
        await env.close()


class TestValidation:
    def test_a_good_state_is_kept_and_secret_fields_are_not_echoed(self) -> None:
        state = validate_state(LOGIN)
        form = state["blocks"][2]
        assert form["fields"][0]["value"] == ""  # a login code is never sent back
        assert form["fields"][1]["value"] == "+39 1"
        assert state["title"] == "Account login"

    def test_unknown_keys_are_dropped_not_passed_through(self) -> None:
        state = validate_state(
            {
                "blocks": [
                    {
                        "type": "text",
                        "text": "x",
                        "html": "<script>1</script>",
                        "onclick": "x()",
                    }
                ],
                "script": "x",
            }
        )
        assert state == {"title": "", "blocks": [{"type": "text", "text": "x"}]}

    @pytest.mark.parametrize(
        "bad",
        [
            "text",
            {"blocks": "no"},
            {"blocks": [{"type": "iframe", "src": "http://x"}]},
            {"blocks": [{"type": "button", "action": "Bad Action", "label": "x"}]},
            {
                "blocks": [
                    {
                        "type": "form",
                        "action": "a",
                        "fields": [{"name": "x", "kind": "file"}],
                    }
                ]
            },
            {
                "blocks": [
                    {
                        "type": "form",
                        "action": "a",
                        "fields": [{"name": "A!", "kind": "text"}],
                    }
                ]
            },
            {"blocks": [{"type": "text", "text": 5}]},
            {"blocks": [{"type": "text", "text": "x"}] * (MAX_BLOCKS + 1)},
        ],
    )
    def test_anything_outside_the_vocabulary_is_refused(self, bad: Any) -> None:
        with pytest.raises(RpcError) as caught:
            validate_state(bad)
        assert caught.value.code == ErrorCode.INVALID_PARAMS

    def test_long_text_is_cut(self) -> None:
        state = validate_state({"blocks": [{"type": "text", "text": "a" * 5000}]})
        assert len(state["blocks"][0]["text"]) == 600

    def test_values_from_the_browser_are_bounded_strings(self) -> None:
        assert clean_values({"code": "1" * 900, "Bad Key": "x", "n": 5, "ok": "y"}) == {
            "code": "1" * 400,
            "ok": "y",
        }
        assert clean_values("nope") == {}
        assert len(clean_values({f"k{i}": "v" for i in range(30)})) == 6


class TestApi:
    @pytest.mark.asyncio
    async def test_set_stores_the_cleaned_state_per_interface(
        self, rig: tuple[Env, PanelStore, Client]
    ) -> None:
        _, store, client = rig
        assert await client.call(
            "panels.set", interface_id="chat", panel="login", state=LOGIN
        ) == {"ok": True}
        [entry] = store.for_interface("chat")
        assert entry["extension"] == "dev/chat" and entry["panel"] == "login"
        assert entry["state"]["blocks"][2]["fields"][0]["value"] == ""
        assert store.for_interface("other") == []

    @pytest.mark.asyncio
    async def test_an_interface_the_manifest_does_not_declare_is_refused(
        self, rig: tuple[Env, PanelStore, Client]
    ) -> None:
        _, store, client = rig
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "panels.set",
            interface_id="telegram_bot",
            panel="p",
            state={},
        )
        assert store.states == {}

    @pytest.mark.asyncio
    async def test_a_bad_state_is_invalid_params(
        self, rig: tuple[Env, PanelStore, Client]
    ) -> None:
        _, store, client = rig
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "panels.set",
            interface_id="chat",
            panel="login",
            state={"blocks": [{"type": "iframe"}]},
        )
        assert store.states == {}

    @pytest.mark.asyncio
    async def test_the_capability_is_required(self) -> None:
        ports, grants = memory_ports(), MemoryGrantStore()
        gateway = Gateway(grants=grants)
        register_core_api(gateway, ports)
        register_panel_api(gateway, PanelStore())
        env = Env(gateway, grants, ports)
        try:
            client = await env.connect(manifest(), grant=())
            await client.expect(
                ErrorCode.PERMISSION_DENIED,
                "panels.set",
                interface_id="chat",
                panel="login",
                state={},
            )
        finally:
            await env.close()

    @pytest.mark.asyncio
    async def test_panels_disappear_when_the_extension_goes_away(
        self, rig: tuple[Env, PanelStore, Client]
    ) -> None:
        import asyncio

        _, store, client = rig
        await client.call("panels.set", interface_id="chat", panel="login", state=LOGIN)
        await client.peer.close()
        await asyncio.sleep(0.05)
        assert store.for_interface("chat") == []


class TestActions:
    @pytest.mark.asyncio
    async def test_an_action_reaches_the_extension_with_the_values(
        self, rig: tuple[Env, PanelStore, Client]
    ) -> None:
        env, _, client = rig
        seen: list[dict[str, Any]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "panel.action":
                seen.append(dict(params))
                return {"ok": True}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        result = await run_action(
            env.gateway, "dev/chat", "chat", "login", "verify_code", {"code": "99"}
        )
        assert result == {"ok": True, "error": ""}
        assert seen == [
            {
                "interface_id": "chat",
                "panel": "login",
                "action": "verify_code",
                "values": {"code": "99"},
            }
        ]

    @pytest.mark.asyncio
    async def test_a_failing_or_missing_extension_is_reported_not_raised(
        self, rig: tuple[Env, PanelStore, Client]
    ) -> None:
        env, _, client = rig
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "panel.action":
                raise RpcError(ErrorCode.INTERNAL_ERROR, "boom")
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        assert await run_action(env.gateway, "dev/chat", "chat", "login", "x", {}) == {
            "ok": False,
            "error": "boom",
        }
        missing = await run_action(env.gateway, "dev/gone", "chat", "login", "x", {})
        assert missing["ok"] is False


class TestWebUiRoutes:
    """The two HTTP routes, with the real FastAPI router and a stand-in token check."""

    @pytest.mark.asyncio
    async def test_list_and_act_through_http(
        self, rig: tuple[Env, PanelStore, Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import FastAPI
        from core import webui_auth
        from core.extensions.webui_panels import build_router

        env, store, client = rig
        await client.call("panels.set", interface_id="chat", panel="login", state=LOGIN)
        seen: list[dict[str, Any]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "panel.action":
                seen.append(dict(params))
                return {"ok": True}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        monkeypatch.setattr(
            webui_auth,
            "request_authorized",
            lambda request: request.headers.get("x-ok") == "1",
        )
        app = FastAPI()
        app.include_router(build_router(env.gateway, store))

        import httpx

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as web:
            ok = {"x-ok": "1"}
            denied = await web.get("/api/extensions/panels/chat")
            listed = await web.get("/api/extensions/panels/chat", headers=ok)
            acted = await web.post(
                "/api/extensions/panels/chat/login/action",
                headers=ok,
                json={
                    "action": "verify_code",
                    "values": {"code": "12", "Bad Key": "x"},
                },
            )
            missing = await web.post(
                "/api/extensions/panels/chat/nope/action",
                headers=ok,
                json={"action": "x"},
            )
        assert denied.status_code == 401
        assert listed.json()["panels"][0]["panel"] == "login"
        assert acted.json() == {"ok": True, "error": ""}
        assert (
            seen[0]["values"] == {"code": "12"} and seen[0]["action"] == "verify_code"
        )
        assert missing.status_code == 404
