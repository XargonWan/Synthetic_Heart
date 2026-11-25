"""The OpenAI API extension, driven by a real HTTP client against the real process.

The extension is the request/response kind of interface: an HTTP request is one turn
through the Core's message chain, answered with the text that comes back.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
import pytest_asyncio

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.info import register_info_api
from core.extensions.interfaces import (
    InterfacePorts,
    RemoteInterface,
    register_interface_api,
)
from core.extensions.launcher import Launcher
from core.extensions.manifest import load_manifest
from core.extensions.memory_ports import (
    MemoryConfig,
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy
from core.extensions.supervisor import RestartPolicy, Supervisor
from tests.support.fake_fluxer import wait_until
from tests.test_extensions_runtime import Harness

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "openai_api"
EXT_ID = "official/openai_api"


class Rig(SimpleNamespace):
    harness: Harness
    host: MemoryInterfaceHost
    messaging: MemoryMessaging
    base: str
    info: dict[str, Any]


@pytest.fixture(scope="session")
def aiohttp_lib(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is needed to build the extension's lib/")
    target = tmp_path_factory.mktemp("aio") / "lib"
    done = subprocess.run(
        [uv, "pip", "install", "--offline", "--target", str(target), "aiohttp>=3.9"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        pytest.skip(f"cannot build lib/ offline: {done.stderr.strip()[-200:]}")
    return target


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    register_interface_api(gateway, InterfacePorts(host, messaging))
    info: dict[str, Any] = {
        "synth_name": "Rei",
        "capabilities": {"vision": True, "audio": False},
        "in_container": False,
    }
    register_info_api(gateway, lambda: info)
    layout = RuntimeLayout(tmp_path / "extensions")
    launcher = Launcher(gateway, layout, SandboxPolicy(drop_privileges=False))
    supervisor = Supervisor(
        launcher,
        RestartPolicy(
            initial_delay=0.05,
            max_delay=0.1,
            max_failures=3,
            hello_timeout=10.0,
            stop_grace=1.0,
            term_wait=0.5,
        ),
    )
    harness = Harness(layout, gateway, grants, ports, launcher, supervisor)
    port = free_port()
    try:
        yield Rig(
            harness=harness,
            host=host,
            messaging=messaging,
            base=f"http://127.0.0.1:{port}",
            info=info,
            port=port,
        )
    finally:
        await supervisor.stop_all()
        await gateway.close_all()


async def start(rig: Rig, lib: Path, **config: Any) -> RemoteInterface:
    h = rig.harness
    manifest = load_manifest(SOURCE / "synth-extension.toml")
    assert manifest.id == EXT_ID
    directory = h.layout.install_dir(EXT_ID)
    directory.mkdir(parents=True)
    for name in ("ext_main.py", "convert.py"):
        shutil.copy(SOURCE / name, directory / name)
    (directory / "lib").symlink_to(lib)
    h.grants.grant(EXT_ID, *manifest.permissions.capabilities)
    config_port = h.ports.config
    assert isinstance(config_port, MemoryConfig)
    config_port.values[EXT_ID] = {"OPENAI_API_PORT": rig.port, **config}
    h.supervisor.register(manifest)
    await h.supervisor.start(EXT_ID)
    try:
        await wait_until(lambda: "openai_api" in rig.host.interfaces, 20)
        async with aiohttp.ClientSession() as session:
            for _ in range(100):
                try:
                    async with session.get(rig.base + "/") as response:
                        if response.status == 200:
                            break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.1)
    except TimeoutError:
        pytest.fail(f"not up: status={h.supervisor.status()}")
    remote = rig.host.interfaces["openai_api"]
    assert isinstance(remote, RemoteInterface)
    return remote


CHAT = {"messages": [{"role": "user", "content": "Hello?"}]}


async def post(
    rig: Rig, route: str, body: Any, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.post(rig.base + route, json=body, headers=headers) as r:
            return r.status, await r.json()


async def get(
    rig: Rig, route: str, headers: dict[str, str] | None = None
) -> tuple[int, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(rig.base + route, headers=headers) as r:
            return r.status, await r.json()


async def stream(rig: Rig, route: str, body: dict[str, Any]) -> tuple[int, str, str]:
    async with aiohttp.ClientSession() as session:
        async with session.post(rig.base + route, json=body) as r:
            return r.status, r.headers.get("Content-Type", ""), await r.text()


def events(raw: str) -> list[Any]:
    out: list[Any] = []
    for block in raw.split("\n\n"):
        line = block.strip()
        if line.startswith("data: "):
            payload = line[len("data: ") :]
            out.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return out


class TestModels:
    @pytest.mark.asyncio
    async def test_the_synth_is_one_model_named_after_it(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        status, body = await get(rig, "/v1/models")
        assert status == 200 and body["object"] == "list"
        [model] = body["data"]
        assert (model["id"], model["object"]) == ("Rei", "model")
        assert model["capabilities"] == {"vision": True}
        status, one = await get(rig, "/v1/models/Rei")
        assert status == 200 and one["id"] == "Rei"
        status, missing = await get(rig, "/v1/models/gpt-4")
        assert status == 404 and missing["error"]["code"] == "model_not_found"

    @pytest.mark.asyncio
    async def test_the_health_route_and_no_ollama_routes(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        assert await get(rig, "/") == (
            200,
            {"status": "ok", "interface": "openai_api"},
        )
        status, _ = await get(rig, "/api/tags")
        assert status == 404


class TestChat:
    @pytest.mark.asyncio
    async def test_a_reply_comes_back_as_a_chat_completion(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.answer = "Hi there!"
        status, body = await post(rig, "/v1/chat/completions", CHAT)
        assert status == 200
        assert body["object"] == "chat.completion" and body["model"] == "Rei"
        assert body["choices"] == [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hi there!"},
                "finish_reason": "stop",
            }
        ]
        assert set(body["usage"]) == {
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        }
        waited = rig.messaging.waited[0]
        wrapped = waited["message"]
        assert wrapped.text == "Hello?" and waited["interface_id"] == "openai_api"
        assert wrapped.interface_path.startswith("openai_api/")
        assert wrapped.chat_id == wrapped.interface_path.split("/", 1)[1]

    @pytest.mark.asyncio
    async def test_the_clients_conversation_is_the_prompt_memory(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.answer = "ok"
        await post(
            rig,
            "/v1/chat/completions",
            {
                "conversation": "abc",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": [{"type": "text", "text": "first"}]},
                    {"role": "assistant", "content": "second"},
                    {"role": "tool", "content": "ignored"},
                    {"role": "user", "content": "and now?"},
                ],
            },
        )
        waited = rig.messaging.waited[0]
        assert waited["message"].interface_path == "openai_api/c-abc"
        [(_, memory)] = waited["context_memory"].items()
        assert [(m["role"], m["text"]) for m in memory] == [
            ("system", "be brief"),
            ("user", "first"),
            ("assistant", "second"),
            ("user", "and now?"),
        ]

    @pytest.mark.asyncio
    async def test_the_amount_of_history_is_bounded_by_the_setting(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib, OPENAI_API_MAX_HISTORY=2)
        rig.messaging.answer = "ok"
        messages = [
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
            for i in range(6)
        ] + [{"role": "user", "content": "last"}]
        await post(rig, "/v1/chat/completions", {"messages": messages})
        [(_, memory)] = rig.messaging.waited[0]["context_memory"].items()
        assert [m["text"] for m in memory] == ["m4", "m5", "last"]

    @pytest.mark.asyncio
    async def test_a_command_is_answered_without_a_turn(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.command_reply = "pong"
        _, body = await post(
            rig,
            "/v1/chat/completions",
            {"messages": [{"role": "user", "content": "/ping"}]},
        )
        assert body["choices"][0]["message"]["content"] == "pong"
        assert rig.messaging.waited == []

    @pytest.mark.asyncio
    async def test_a_turn_with_no_text_is_an_empty_answer_not_an_error(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.answer = None
        status, body = await post(rig, "/v1/chat/completions", CHAT)
        assert status == 200 and body["choices"][0]["message"]["content"] == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"messages": []},
            {"messages": "hi"},
            {"messages": [{"role": "assistant", "content": "x"}]},
            {"messages": [{"role": "user", "content": "   "}]},
            [1, 2],
        ],
    )
    async def test_a_malformed_request_is_a_400_in_openai_shape(
        self, rig: Rig, aiohttp_lib: Path, body: Any
    ) -> None:
        await start(rig, aiohttp_lib)
        status, answer = await post(rig, "/v1/chat/completions", body)
        assert status == 400
        assert answer["error"]["type"] == "invalid_request_error"
        assert rig.messaging.waited == []

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_a_400(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.base + "/v1/chat/completions", data=b"{nope"
            ) as response:
                assert response.status == 400


class TestCompletions:
    @pytest.mark.asyncio
    async def test_a_prompt_is_a_text_completion(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.answer = "four"
        status, body = await post(
            rig, "/v1/completions", {"prompt": ["2+2", "?"], "model": "Rei"}
        )
        assert status == 200 and body["object"] == "text_completion"
        assert body["choices"][0]["text"] == "four"
        assert rig.messaging.waited[0]["message"].text == "2+2\n?"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt", [None, "", "  ", 3, [1]])
    async def test_a_bad_prompt_is_refused(
        self, rig: Rig, aiohttp_lib: Path, prompt: Any
    ) -> None:
        await start(rig, aiohttp_lib)
        status, _ = await post(rig, "/v1/completions", {"prompt": prompt})
        assert status == 400


class TestStreaming:
    @pytest.mark.asyncio
    async def test_standard_sse_with_a_done_marker(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.answer = "Hi there!"
        status, ctype, raw = await stream(
            rig, "/v1/chat/completions", {**CHAT, "stream": True}
        )
        assert status == 200 and ctype.startswith("text/event-stream")
        assert raw.endswith("data: [DONE]\n\n")
        chunks = events(raw)
        assert chunks[-1] == "[DONE]"
        body = [c for c in chunks if c != "[DONE]"]
        assert {c["object"] for c in body} == {"chat.completion.chunk"}
        assert len({c["id"] for c in body}) == 1  # one id for the whole response
        assert body[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in body)
        assert text == "Hi there!"
        assert body[-1]["choices"][0]["finish_reason"] == "stop"
        assert all(c["choices"][0]["finish_reason"] is None for c in body[:-1])

    @pytest.mark.asyncio
    async def test_a_long_reply_arrives_in_several_chunks(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        long = " ".join(["word"] * 400)
        rig.messaging.answer = long
        _, _, raw = await stream(rig, "/v1/chat/completions", {**CHAT, "stream": True})
        pieces = [
            c["choices"][0]["delta"]["content"]
            for c in events(raw)
            if c != "[DONE]" and c["choices"][0]["delta"].get("content")
        ]
        assert len(pieces) > 1 and "".join(pieces) == long

    @pytest.mark.asyncio
    async def test_a_streamed_text_completion(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.answer = "four"
        _, _, raw = await stream(
            rig, "/v1/completions", {"prompt": "2+2", "stream": True}
        )
        chunks = [c for c in events(raw) if c != "[DONE]"]
        assert "".join(c["choices"][0]["text"] for c in chunks) == "four"
        assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_without_stream_true_the_answer_is_plain_json(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        rig.messaging.answer = "x"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.base + "/v1/chat/completions", json={**CHAT, "stream": False}
            ) as response:
                assert response.headers["Content-Type"].startswith("application/json")


class TestDetachedTurns:
    @pytest.mark.asyncio
    async def test_an_agent_turn_answers_through_a_later_delivery(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        remote = await start(rig, aiohttp_lib)
        rig.messaging.answer = None  # the turn was handed to the agent lane

        def hand_over() -> None:
            path = rig.messaging.waited[-1]["message"].interface_path
            rig.messaging.in_flight.add(path)

        original = rig.messaging.enqueue_and_wait

        async def spy(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            hand_over()
            return result

        rig.messaging.enqueue_and_wait = spy  # type: ignore[method-assign]
        request = asyncio.ensure_future(post(rig, "/v1/chat/completions", CHAT))
        await wait_until(lambda: rig.messaging.waited)
        path = rig.messaging.waited[0]["message"].interface_path
        await asyncio.sleep(0.2)
        assert not request.done()  # still waiting for the agent
        await remote.send_message({"text": "the agent says hi", "interface_path": path})
        status, body = await asyncio.wait_for(request, 10)
        assert status == 200
        assert body["choices"][0]["message"]["content"] == "the agent says hi"

    @pytest.mark.asyncio
    async def test_a_delivery_nobody_waits_for_is_refused(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        remote = await start(rig, aiohttp_lib)
        delivered = await remote.send_message(
            {"text": "hello?", "interface_path": "openai_api/c-nobody"}
        )
        assert delivered is False  # the Core is told: nobody got it


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_one_request_at_a_time_per_conversation(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib)
        gate = asyncio.Event()
        original = rig.messaging.enqueue_and_wait

        async def slow(*args: Any, **kwargs: Any) -> Any:
            await gate.wait()
            return await original(*args, **kwargs)

        rig.messaging.enqueue_and_wait = slow  # type: ignore[method-assign]
        rig.messaging.answer = "done"
        body = {**CHAT, "conversation": "one"}
        first = asyncio.ensure_future(post(rig, "/v1/chat/completions", body))
        await wait_until(lambda: rig.messaging.processing)
        status, busy = await asyncio.wait_for(
            post(rig, "/v1/chat/completions", body), 5
        )
        assert status == 409 and busy["error"]["code"] == "conversation_busy"
        gate.set()
        status, answer = await asyncio.wait_for(first, 10)
        assert status == 200 and answer["choices"][0]["message"]["content"] == "done"
        # and the conversation is free again afterwards
        assert (await post(rig, "/v1/chat/completions", body))[0] == 200


class TestAccess:
    @pytest.mark.asyncio
    async def test_with_a_key_the_routes_need_it_and_health_does_not(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:
        await start(rig, aiohttp_lib, OPENAI_API_TOKEN="s3cret")
        rig.messaging.answer = "ok"
        status, body = await get(rig, "/v1/models")
        assert status == 401 and body["error"]["code"] == "invalid_api_key"
        status, _ = await get(rig, "/v1/models", {"Authorization": "Bearer wrong"})
        assert status == 401
        status, _ = await post(rig, "/v1/chat/completions", CHAT)
        assert status == 401 and rig.messaging.waited == []
        good = {"Authorization": "Bearer s3cret"}
        assert (await get(rig, "/v1/models", good))[0] == 200
        assert (await post(rig, "/v1/chat/completions", CHAT, good))[0] == 200
        assert (await get(rig, "/"))[0] == 200

    @pytest.mark.asyncio
    async def test_without_a_key_it_is_open(self, rig: Rig, aiohttp_lib: Path) -> None:
        await start(rig, aiohttp_lib)
        assert (await get(rig, "/v1/models"))[0] == 200


class TestBindAddress:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("configured", "in_container", "expected"),
        [
            ("", True, "0.0.0.0"),
            ("", False, "127.0.0.1"),
            ("10.1.2.3", True, "10.1.2.3"),
        ],
    )
    async def test_the_address_follows_where_it_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        configured: str,
        in_container: bool,
        expected: str,
    ) -> None:
        monkeypatch.syspath_prepend(str(SOURCE))
        import ext_main

        class Config:
            async def get(self, key: str) -> Any:
                return {"OPENAI_API_HOST": configured}.get(key)

        class Core:
            async def get(self) -> dict[str, Any]:
                return {
                    "synth_name": "",
                    "capabilities": {},
                    "in_container": in_container,
                }

        ctx = SimpleNamespace(config=Config(), core=Core())
        server = ext_main.Server(ctx)  # type: ignore[arg-type]
        assert await server.configure() == expected
        assert server.synth_name == "SyntH"  # no name known: a sensible default


class TestRegistration:
    @pytest.mark.asyncio
    async def test_it_registers_as_a_reply_only_interface(
        self, rig: Rig, aiohttp_lib: Path
    ) -> None:

        await start(rig, aiohttp_lib)
        manifest = load_manifest(SOURCE / "synth-extension.toml")
        [provided] = manifest.provides.interfaces
        assert provided.reply_only is True
        assert provided.paths == ("openai_api/{chat}",)
