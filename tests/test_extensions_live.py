"""Live engines in extensions, and calls held by them (``live.*``, ``mode: "live"``)."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.live import LiveEngine, LiveSession
from synth_sdk.rpc import ErrorCode

from core.call_manager import CallManager
from core.extensions.api import register_core_api
from core.extensions.calls import CAP_CALLS, CallPorts, register_call_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import InterfacePorts, register_interface_api
from core.extensions.live import (
    CAP_LIVE,
    MAX_AUDIO_BYTES,
    RemoteLiveEngine,
    register_live_api,
)
from core.extensions.manifest import (
    InterfaceProvision,
    Manifest,
    Permissions,
    Provides,
)
from core.extensions.media import MediaBridge
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from core.live_call_bridge import BridgePorts, LiveCalls, LiveCallBridge
from plugins.live_base import LiveEngineBase, LiveEvent, LiveEventType
from tests.test_extensions_calls import FakePipeline, FakeVideo
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_interface_extras import answering

CAPS = (CAP_LIVE,)
PCM = b"\x01\x00" * 480  # 20 ms at 24 kHz
LONG = b"\x01\x00" * 12000  # 500 ms at 24 kHz


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


async def settle(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


class MemoryLiveHost:
    def __init__(self, builtin: tuple[str, ...] = ("gemini",)) -> None:
        self.engines: dict[str, tuple[RemoteLiveEngine, str]] = {}
        self.builtin = set(builtin)

    def taken(self, name: str) -> bool:
        return name in self.builtin

    def register(self, name: str, engine: Any, label: str) -> None:
        self.engines[name] = (engine, label)

    def unregister(self, name: str) -> None:
        self.engines.pop(name, None)


def live_manifest(*, engines: tuple[str, ...] = ("live",)) -> Manifest:
    return Manifest(
        id="dev/live",
        name="dev/live",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=CAPS),
        provides=Provides(engines=engines),
    )


@pytest_asyncio.fixture
async def provider_env() -> AsyncIterator[tuple[Env, MemoryLiveHost]]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host = MemoryLiveHost()
    register_live_api(gateway, host)
    env = Env(gateway, grants, ports)
    try:
        yield env, host
    finally:
        await env.close()


async def announce(
    env: Env, host: MemoryLiveHost, name: str = "fake_live"
) -> tuple[Client, RemoteLiveEngine]:
    client = await env.connect(live_manifest(), grant=CAPS)
    await client.call("live.provider", name=name, label="Fake Live")
    engine = host.engines[name][0]
    return client, engine


class TestAnnouncing:
    @pytest.mark.asyncio
    async def test_an_engine_is_offered_under_its_name_and_label(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client = await env.connect(live_manifest(), grant=CAPS)
        await client.call(
            "live.provider",
            name="fake_live",
            label="Fake Live",
            max_session_seconds=600,
        )
        engine, label = host.engines["fake_live"]
        assert label == "Fake Live" and isinstance(engine, LiveEngineBase)
        assert engine.MAX_SESSION_SECONDS == 600 and engine.supports_input

    @pytest.mark.asyncio
    async def test_the_manifest_has_to_declare_a_live_engine(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client = await env.connect(live_manifest(engines=("llm",)), grant=CAPS)
        await client.expect(ErrorCode.PERMISSION_DENIED, "live.provider", name="x_live")
        assert host.engines == {}

    @pytest.mark.asyncio
    async def test_the_capability_has_to_be_granted(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client = await env.connect(live_manifest(), grant=())
        await client.expect(ErrorCode.PERMISSION_DENIED, "live.provider", name="x_live")
        assert host.engines == {}

    @pytest.mark.asyncio
    async def test_a_built_in_engine_cannot_be_replaced(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client = await env.connect(live_manifest(), grant=CAPS)
        await client.expect(ErrorCode.PERMISSION_DENIED, "live.provider", name="gemini")
        await client.expect(ErrorCode.INVALID_PARAMS, "live.provider", name="Bad Name")
        assert host.engines == {}

    @pytest.mark.asyncio
    async def test_an_event_needs_an_announced_engine(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, _host = provider_env
        client = await env.connect(live_manifest(), grant=CAPS)
        await client.expect(
            ErrorCode.INVALID_PARAMS, "live.event", session_id="s", type="turn_complete"
        )

    @pytest.mark.asyncio
    async def test_the_engine_goes_when_the_extension_does(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        answering(client, {"live.open": {"ok": True}})
        await engine.open_session("s1")
        await client.peer.close()
        await env.tasks[0]  # the session ends with its pipe
        await settle()
        assert host.engines == {}
        kinds = [event.type async for event in engine.receive_events("s1")]
        assert kinds == []  # the stream is already ended and cleared


class TestSession:
    @pytest.mark.asyncio
    async def test_opening_hands_the_extension_the_neutral_session(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        seen = answering(client, {"live.open": {"ok": True}})
        tools = [{"name": "say", "description": "say it", "parameters": []}]
        await engine.open_session(
            "s1",
            system_instruction="You are Rei.",
            tools=tools,
            voice="Aoede",
            language="it",
            resume_handle="h1",
        )
        assert seen == [
            (
                "live.open",
                {
                    "session_id": "s1",
                    "system_instruction": "You are Rei.",
                    "tools": tools,
                    "voice": "Aoede",
                    "language": "it",
                    "resume_handle": "h1",
                    "options": {},
                },
            )
        ]
        assert engine.has_session("s1")

    @pytest.mark.asyncio
    async def test_a_refused_session_raises_and_leaves_nothing_open(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        from synth_sdk.rpc import RpcError

        env, host = provider_env
        client, engine = await announce(env, host)
        answering(client, {"live.open": RpcError(ErrorCode.SERVER_ERROR, "no key")})
        with pytest.raises(RuntimeError, match="no key"):
            await engine.open_session("s1")
        assert not engine.has_session("s1")

    @pytest.mark.asyncio
    async def test_what_the_model_does_arrives_in_order(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        answering(client, {"live.open": {"ok": True}, "live.close": {}})
        await engine.open_session("s1")
        await client.call(
            "live.event", session_id="s1", type="state", state="connected"
        )
        await client.call(
            "live.event", session_id="s1", type="audio", pcm=b64(PCM), rate=24000
        )
        await client.call(
            "live.event",
            session_id="s1",
            type="transcript",
            text=" ciao ",
            side="input",
        )
        await client.call(
            "live.event",
            session_id="s1",
            type="tool_call",
            call_id="c1",
            name="say",
            args={"text": "hi"},
        )
        await client.call("live.event", session_id="s1", type="interrupted")
        await client.call("live.event", session_id="s1", type="turn_complete")
        await client.call("live.event", session_id="s1", type="error", detail="boom")
        collected: list[LiveEvent] = []

        async def read() -> None:
            async for event in engine.receive_events("s1"):
                collected.append(event)

        reader = asyncio.create_task(read())
        await settle()
        await engine.close_session("s1")
        await asyncio.wait_for(reader, 2)
        kinds = [e.type for e in collected]
        assert kinds == [
            LiveEventType.STATE,
            LiveEventType.AUDIO,
            LiveEventType.TRANSCRIPT,
            LiveEventType.TOOL_CALL,
            LiveEventType.INTERRUPTED,
            LiveEventType.TURN_COMPLETE,
            LiveEventType.ERROR,
        ]
        assert collected[0].detail == "connected"
        assert collected[1].audio == PCM and collected[1].metadata["rate"] == 24000
        assert collected[2].text == "ciao" and collected[2].metadata["side"] == "input"
        call = collected[3].tool_call
        assert call is not None and (call.call_id, call.name, call.args) == (
            "c1",
            "say",
            {"text": "hi"},
        )
        assert collected[6].detail == "boom"

    @pytest.mark.asyncio
    async def test_a_late_event_of_a_closed_session_is_ignored(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        answering(client, {"live.open": {"ok": True}, "live.close": {}})
        await engine.open_session("s1")
        await engine.close_session("s1")
        result = await client.call("live.event", session_id="s1", type="turn_complete")
        assert result == {"accepted": False}

    @pytest.mark.asyncio
    async def test_bad_events_are_refused(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        answering(client, {"live.open": {"ok": True}})
        await engine.open_session("s1")
        for params in (
            {"type": "audio", "pcm": "!!not base64!!"},
            {"type": "audio", "pcm": b64(b"\x01")},  # half a sample
            {"type": "audio", "pcm": ""},
            {"type": "transcript", "text": "  "},
            {"type": "tool_call", "call_id": "", "name": ""},
            {"type": "state", "state": "exploded"},
            {"type": "nonsense"},
        ):
            await client.expect(
                ErrorCode.INVALID_PARAMS, "live.event", session_id="s1", **params
            )

    @pytest.mark.asyncio
    async def test_audio_text_and_tool_results_reach_the_extension(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        seen = answering(
            client,
            {
                "live.open": {"ok": True},
                "live.audio": {},
                "live.text": {},
                "live.tool_result": {},
            },
        )
        await engine.open_session("s1")
        await engine.send_audio("s1", PCM, 16000)
        await engine.send_text("s1", "Marco joined", "context")
        await engine.send_text("s1", "say hi", "bogus")
        await engine.send_tool_response("s1", "c1", "say", {"status": "ok"})
        await settle()
        by_method = {m: p for m, p in seen}
        assert base64.b64decode(by_method["live.audio"]["pcm"]) == PCM
        assert by_method["live.audio"]["rate"] == 16000
        texts = [p for m, p in seen if m == "live.text"]
        assert [t["kind"] for t in texts] == ["context", "context"]
        assert by_method["live.tool_result"] == {
            "session_id": "s1",
            "call_id": "c1",
            "name": "say",
            "result": {"status": "ok"},
        }

    @pytest.mark.asyncio
    async def test_a_session_that_is_not_open_is_not_written_to(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        seen = answering(client, {"live.text": {}, "live.audio": {}})
        await engine.send_audio("nope", PCM)
        await engine.send_text("nope", "x")
        await settle()
        assert seen == []

    @pytest.mark.asyncio
    async def test_one_audio_event_is_bounded(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        env, host = provider_env
        client, engine = await announce(env, host)
        answering(client, {"live.open": {"ok": True}})
        await engine.open_session("s1")
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "live.event",
            session_id="s1",
            type="audio",
            pcm=b64(b"\x00\x00" * (MAX_AUDIO_BYTES // 2 + 1)),
        )


# --------------------------------------------------------------------------- the SDK


class FakeCtx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handlers: dict[str, Callable[..., Any]] = {}

    def handle(self, method: str, handler: Callable[..., Any]) -> None:
        self.handlers[method] = handler

    async def call(
        self, method: str, _timeout: float | None = None, **params: Any
    ) -> Any:
        self.calls.append((method, params))
        return {"ok": True, "accepted": True}


class EchoEngine(LiveEngine):
    label = "Echo"
    max_session_seconds = 300

    def __init__(self) -> None:
        self.opened: list[LiveSession] = []
        self.heard: list[tuple[str, bytes, int]] = []
        self.texts: list[tuple[str, str]] = []
        self.results: list[tuple[str, str, dict[str, Any]]] = []
        self.closed: list[str] = []
        self.refuse = False

    async def open(self, session: LiveSession) -> None:
        if self.refuse:
            raise RuntimeError("no api key")
        self.opened.append(session)

    async def audio(self, session_id: str, pcm: bytes, rate: int) -> None:
        self.heard.append((session_id, pcm, rate))

    async def text(self, session_id: str, text: str, kind: str) -> None:
        self.texts.append((kind, text))

    async def tool_result(
        self, session_id: str, call_id: str, name: str, result: dict[str, Any]
    ) -> None:
        self.results.append((call_id, name, result))

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)


class TestSdk:
    @pytest.mark.asyncio
    async def test_serve_announces_and_wires_the_handlers(self) -> None:
        from synth_sdk.runtime import Live

        ctx, engine = FakeCtx(), EchoEngine()
        live = Live(ctx)  # type: ignore[arg-type]
        await live.serve("echo_live", engine)
        assert ctx.calls == [
            (
                "live.provider",
                {
                    "name": "echo_live",
                    "label": "Echo",
                    "input_rate": 16000,
                    "output_rate": 24000,
                    "max_session_seconds": 300,
                },
            )
        ]
        assert set(ctx.handlers) == {
            "live.open",
            "live.audio",
            "live.text",
            "live.tool_result",
            "live.close",
        }
        await ctx.handlers["live.open"](
            {
                "session_id": "s1",
                "system_instruction": "persona",
                "tools": [{"name": "say"}],
                "voice": "v",
                "language": "it",
                "resume_handle": None,
            }
        )
        opened = engine.opened[0]
        assert (opened.system_instruction, opened.voice, opened.language) == (
            "persona",
            "v",
            "it",
        )
        await ctx.handlers["live.audio"](
            {"session_id": "s1", "pcm": b64(PCM), "rate": 16000}
        )
        await ctx.handlers["live.text"](
            {"session_id": "s1", "text": "hi", "kind": "turn"}
        )
        await ctx.handlers["live.tool_result"](
            {"session_id": "s1", "call_id": "c", "name": "say", "result": {"a": 1}}
        )
        await ctx.handlers["live.close"]({"session_id": "s1"})
        assert engine.heard == [("s1", PCM, 16000)]
        assert engine.texts == [("turn", "hi")]
        assert engine.results == [("c", "say", {"a": 1})]
        assert engine.closed == ["s1"]
        # Nothing reaches an engine for a session it does not hold.
        await ctx.handlers["live.audio"]({"session_id": "s1", "pcm": b64(PCM)})
        assert len(engine.heard) == 1

    @pytest.mark.asyncio
    async def test_an_engine_that_cannot_open_refuses_with_its_reason(self) -> None:
        from synth_sdk.rpc import RpcError
        from synth_sdk.runtime import Live

        ctx, engine = FakeCtx(), EchoEngine()
        engine.refuse = True
        live = Live(ctx)  # type: ignore[arg-type]
        await live.serve("echo_live", engine)
        with pytest.raises(RpcError, match="no api key"):
            await ctx.handlers["live.open"]({"session_id": "s1"})
        assert live._sessions == {}  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_small_audio_chunks_are_coalesced_and_ordered(self) -> None:
        sent: list[dict[str, Any]] = []

        async def emit(params: dict[str, Any]) -> None:
            sent.append(params)

        session = LiveSession("s1", _emit=emit)
        tiny = b"\x01\x00" * 120  # 5 ms at 24 kHz
        for _ in range(30):  # 150 ms in total
            await session.audio(tiny, 24000)
        await session.transcript("hello")
        pcm_events = [e for e in sent if e["type"] == "audio"]
        # 100 ms went out as one event when it filled; the rest before the transcript.
        assert len(pcm_events) == 2
        total = sum(len(base64.b64decode(e["pcm"])) for e in pcm_events)
        assert total == len(tiny) * 30
        assert sent[-1]["type"] == "transcript" and sent[-2]["type"] == "audio"

    @pytest.mark.asyncio
    async def test_interrupting_drops_the_audio_still_held(self) -> None:
        sent: list[dict[str, Any]] = []

        async def emit(params: dict[str, Any]) -> None:
            sent.append(params)

        session = LiveSession("s1", _emit=emit)
        await session.audio(b"\x01\x00" * 100, 24000)
        await session.interrupted()
        assert [e["type"] for e in sent] == ["interrupted"]


class WireCtx:
    """An SDK context whose calls go over a real protocol channel to the gateway."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.handlers: dict[str, Callable[..., Any]] = {}
        original = client.peer._handler  # noqa: SLF001

        async def dispatch(method: str, params: Any, notification: bool) -> Any:
            if method in self.handlers:
                return await self.handlers[method](params or {})
            return await original(method, params, notification)

        client.peer._handler = dispatch  # noqa: SLF001

    def handle(self, method: str, handler: Callable[..., Any]) -> None:
        self.handlers[method] = handler

    async def call(
        self, method: str, _timeout: float | None = None, **params: Any
    ) -> Any:
        return await self.client.call(method, **params)


class ScriptedEngine(LiveEngine):
    """Speaks when it hears the caller, calls a tool when asked to, like a model would."""

    label = "Scripted"

    def __init__(self) -> None:
        self.sessions: dict[str, LiveSession] = {}
        self.results: list[tuple[str, str, dict[str, Any]]] = []

    async def open(self, session: LiveSession) -> None:
        self.sessions[session.session_id] = session
        await session.state("connected")

    async def audio(self, session_id: str, pcm: bytes, rate: int) -> None:
        session = self.sessions[session_id]
        await session.transcript("hello there", "input")
        await session.audio(LONG, 24000)
        await session.transcript("hi!", "output")
        await session.tool_call("c1", "say", {"text": "hi"})
        await session.turn_complete()

    async def tool_result(
        self, session_id: str, call_id: str, name: str, result: dict[str, Any]
    ) -> None:
        self.results.append((call_id, name, result))


class TestSdkOverTheWire:
    @pytest.mark.asyncio
    async def test_an_sdk_engine_and_the_core_agree_on_the_protocol(
        self, provider_env: tuple[Env, MemoryLiveHost]
    ) -> None:
        from synth_sdk.runtime import Live

        env, host = provider_env
        client = await env.connect(live_manifest(), grant=CAPS)
        ctx = WireCtx(client)
        scripted = ScriptedEngine()
        await Live(ctx).serve("scripted", scripted)  # type: ignore[arg-type]
        engine = host.engines["scripted"][0]
        assert host.engines["scripted"][1] == "Scripted"
        await engine.open_session(
            "s1",
            system_instruction="persona",
            tools=[{"name": "say", "description": "d", "parameters": []}],
            voice="Aoede",
            language="it",
        )
        opened = scripted.sessions["s1"]
        assert (opened.system_instruction, opened.voice, opened.language) == (
            "persona",
            "Aoede",
            "it",
        )
        assert opened.tools[0]["name"] == "say"
        await engine.send_audio("s1", PCM, 16000)
        collected: list[LiveEvent] = []

        async def read() -> None:
            async for event in engine.receive_events("s1"):
                collected.append(event)

        reader = asyncio.create_task(read())
        await settle(0.2)
        await engine.send_tool_response("s1", "c1", "say", {"status": "ok"})
        await engine.send_text("s1", "Marco joined", "context")
        await settle()
        await engine.close_session("s1")
        await asyncio.wait_for(reader, 2)
        assert [e.type for e in collected] == [
            LiveEventType.STATE,
            LiveEventType.TRANSCRIPT,
            LiveEventType.AUDIO,
            LiveEventType.TRANSCRIPT,
            LiveEventType.TOOL_CALL,
            LiveEventType.TURN_COMPLETE,
        ]
        assert collected[2].audio == LONG and collected[2].metadata["rate"] == 24000
        assert collected[4].tool_call.name == "say"  # type: ignore[union-attr]
        assert scripted.results == [("c1", "say", {"status": "ok"})]


# ------------------------------------------------------------------------ the bridge


class FakeLiveEngine(LiveEngineBase):
    display_name = "Fake"

    def __init__(self) -> None:
        self.events: asyncio.Queue[LiveEvent | None] = asyncio.Queue()
        self.opened: dict[str, dict[str, Any]] = {}
        self.audio: list[tuple[str, bytes, int]] = []
        self.texts: list[tuple[str, str, str]] = []
        self.responses: list[tuple[str, str, str, dict[str, Any]]] = []
        self.closed: list[str] = []
        self.fail_open = False

    async def open_session(self, session_id: str, **kwargs: Any) -> None:
        if self.fail_open:
            raise RuntimeError("closed for the night")
        self.opened[session_id] = kwargs

    async def close_session(self, session_id: str) -> None:
        self.closed.append(session_id)
        self.events.put_nowait(None)

    async def send_audio(
        self, session_id: str, chunk: bytes, sample_rate: int = 16000
    ) -> None:
        self.audio.append((session_id, chunk, sample_rate))

    async def send_text(
        self, session_id: str, text: str, kind: str = "context"
    ) -> None:
        self.texts.append((session_id, text, kind))

    async def send_tool_response(
        self, session_id: str, call_id: str, name: str, result: dict[str, Any]
    ) -> None:
        self.responses.append((session_id, call_id, name, result))

    async def receive_events(self, session_id: str) -> AsyncIterator[LiveEvent]:
        while True:
            event = await self.events.get()
            if event is None:
                return
            yield event

    def emit(self, kind: LiveEventType, **fields: Any) -> None:
        self.events.put_nowait(LiveEvent(type=kind, **fields))


class FakeSink:
    def __init__(self) -> None:
        self.played: list[tuple[bytes, int]] = []
        self.flushes = 0

    async def stream(self, pcm: bytes, rate: int) -> None:
        self.played.append((pcm, rate))

    async def flush(self) -> None:
        self.flushes += 1


class Recorder:
    def __init__(self, engine: FakeLiveEngine | None) -> None:
        self.engine = engine
        self.saved: list[dict[str, Any]] = []
        self.memories: list[tuple[str, str]] = []
        self.diaries: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.tools: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.tool_result: dict[str, Any] | Exception = {"status": "ok", "sent": True}
        self.tool_delay = 0.0

    async def run_tool(
        self, name: str, args: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        self.tools.append((name, args, context))
        if self.tool_delay:
            await asyncio.sleep(self.tool_delay)
        if isinstance(self.tool_result, Exception):
            raise self.tool_result
        return self.tool_result

    async def save(self, **kwargs: Any) -> None:
        self.saved.append(kwargs)

    async def memory(self, user: str, model: str) -> None:
        self.memories.append((user, model))

    async def diary(
        self, interface: str, path: str, turns: list[tuple[str, str]]
    ) -> None:
        self.diaries.append((interface, path, turns))

    async def instruction(self) -> str:
        return "You are Rei."

    def ports(self) -> BridgePorts:
        return BridgePorts(
            resolve_engine=lambda: ("fake", self.engine) if self.engine else None,
            system_instruction=self.instruction,
            tools=lambda: [{"name": "say", "description": "say", "parameters": []}],
            run_tool=self.run_tool,
            save_message=self.save,
            record_memory=self.memory,
            record_diary=self.diary,
            voice=lambda: ("Aoede", "it"),
        )


async def make_bridge(
    rec: Recorder, sink: FakeSink | None = None, on_lost: Any = None
) -> tuple[LiveCallBridge, FakeSink]:
    sink = sink or FakeSink()
    calls = LiveCalls(rec.ports)
    bridge = await calls.begin("call1", "chat", "chat/42", sink, on_lost=on_lost)
    assert bridge is not None
    return bridge, sink


class TestBridge:
    @pytest.mark.asyncio
    async def test_the_session_opens_with_persona_tools_and_voice(self) -> None:
        engine = FakeLiveEngine()
        bridge, _ = await make_bridge(Recorder(engine))
        assert engine.opened["call1"] == {
            "system_instruction": "You are Rei.",
            "tools": [{"name": "say", "description": "say", "parameters": []}],
            "voice": "Aoede",
            "language": "it",
            "resume_handle": None,
        }
        await bridge.end()

    @pytest.mark.asyncio
    async def test_no_engine_or_a_refused_session_leaves_the_call_classic(self) -> None:
        assert (
            await LiveCalls(Recorder(None).ports).begin(
                "c", "chat", "chat/1", FakeSink()
            )
            is None
        )
        engine = FakeLiveEngine()
        engine.fail_open = True
        calls = LiveCalls(Recorder(engine).ports)
        assert await calls.begin("c", "chat", "chat/1", FakeSink()) is None
        assert calls.active() == []

    @pytest.mark.asyncio
    async def test_what_the_caller_says_goes_in_at_16_khz(self) -> None:
        engine = FakeLiveEngine()
        bridge, _ = await make_bridge(Recorder(engine))
        await bridge.feed(PCM, "42", "Marco")
        assert engine.audio == [("call1", PCM, 16000)]
        await bridge.tell("Marco shared a link", "context")
        assert engine.texts == [("call1", "Marco shared a link", "context")]
        await bridge.end()

    @pytest.mark.asyncio
    async def test_the_models_voice_is_played_and_dropped_when_interrupted(
        self,
    ) -> None:
        engine = FakeLiveEngine()
        bridge, sink = await make_bridge(Recorder(engine))
        engine.emit(LiveEventType.AUDIO, audio=LONG, metadata={"rate": 24000})
        await settle()
        assert sink.played == [(LONG, 24000)] and bridge.is_speaking
        engine.emit(LiveEventType.INTERRUPTED)
        await settle()
        assert sink.flushes == 1 and not bridge.is_speaking
        await bridge.end()

    @pytest.mark.asyncio
    async def test_a_turn_is_saved_to_history_memory_and_the_diary(self) -> None:
        engine = FakeLiveEngine()
        rec = Recorder(engine)
        bridge, _ = await make_bridge(rec)
        await bridge.feed(PCM, "42", "Marco")
        engine.emit(
            LiveEventType.TRANSCRIPT, text="how are you", metadata={"side": "input"}
        )
        engine.emit(LiveEventType.TRANSCRIPT, text="fine,", metadata={"side": "output"})
        engine.emit(
            LiveEventType.TRANSCRIPT, text="thanks", metadata={"side": "output"}
        )
        engine.emit(LiveEventType.TURN_COMPLETE)
        await settle()
        assert [
            (m["interface_path"], m["sender_name"], m["message_text"])
            for m in rec.saved
        ] == [
            ("live_chat_42", "Marco", "how are you"),
            ("live_chat_42", "self", "fine, thanks"),
        ]
        assert rec.saved[0]["sender_id"] == "42" and rec.saved[1]["sender_id"] == "self"
        assert rec.memories == [("how are you", "fine, thanks")]
        await bridge.end()
        assert rec.diaries == [("chat", "chat/42", [("how are you", "fine, thanks")])]

    @pytest.mark.asyncio
    async def test_a_half_finished_turn_is_kept_when_the_call_ends(self) -> None:
        engine = FakeLiveEngine()
        rec = Recorder(engine)
        bridge, _ = await make_bridge(rec)
        engine.emit(
            LiveEventType.TRANSCRIPT, text="goodbye", metadata={"side": "input"}
        )
        await settle()
        await bridge.end()
        assert [m["message_text"] for m in rec.saved] == ["goodbye"]
        assert rec.memories == []  # no answer: nothing to remember as an exchange
        assert engine.closed == ["call1"]

    @pytest.mark.asyncio
    async def test_a_tool_call_runs_through_the_actions_and_answers_the_model(
        self,
    ) -> None:
        engine = FakeLiveEngine()
        rec = Recorder(engine)
        bridge, _ = await make_bridge(rec)
        from plugins.live_base import ToolCallPayload

        engine.emit(
            LiveEventType.TOOL_CALL,
            tool_call=ToolCallPayload("c1", "say", {"text": "hi"}),
        )
        await settle()
        name, args, context = rec.tools[0]
        assert (name, args) == ("say", {"text": "hi"})
        assert (
            context["interface_path"] == "chat/42" and context["source"] == "live_voice"
        )
        assert engine.responses == [
            ("call1", "c1", "say", {"status": "ok", "sent": True})
        ]
        await bridge.end()

    @pytest.mark.asyncio
    async def test_a_failing_tool_tells_the_model_and_the_call_goes_on(self) -> None:
        engine = FakeLiveEngine()
        rec = Recorder(engine)
        rec.tool_result = RuntimeError("secret internals")
        bridge, _ = await make_bridge(rec)
        from plugins.live_base import ToolCallPayload

        engine.emit(LiveEventType.TOOL_CALL, tool_call=ToolCallPayload("c1", "say", {}))
        await settle()
        assert engine.responses[0][3] == {
            "status": "error",
            "error": "the action failed",
        }
        assert "secret" not in str(engine.responses)
        assert bridge.started
        await bridge.end()

    @pytest.mark.asyncio
    async def test_a_tool_does_not_hold_up_the_voice(self) -> None:
        engine = FakeLiveEngine()
        rec = Recorder(engine)
        rec.tool_delay = 0.5
        bridge, sink = await make_bridge(rec)
        from plugins.live_base import ToolCallPayload

        engine.emit(LiveEventType.TOOL_CALL, tool_call=ToolCallPayload("c1", "say", {}))
        engine.emit(LiveEventType.AUDIO, audio=PCM, metadata={"rate": 24000})
        await settle()
        assert sink.played and not engine.responses
        await bridge.end()  # cancels the slow tool: no answer is sent afterwards
        assert engine.responses == []

    @pytest.mark.asyncio
    async def test_a_stream_that_ends_by_itself_ends_the_call(self) -> None:
        engine = FakeLiveEngine()
        lost: list[str] = []

        async def on_lost(call_id: str) -> None:
            lost.append(call_id)

        await make_bridge(Recorder(engine), on_lost=on_lost)
        engine.events.put_nowait(None)
        await settle()
        assert lost == ["call1"]

    @pytest.mark.asyncio
    async def test_an_engine_that_keeps_failing_is_given_up(self) -> None:
        engine = FakeLiveEngine()
        lost: list[str] = []

        async def on_lost(call_id: str) -> None:
            lost.append(call_id)

        await make_bridge(Recorder(engine), on_lost=on_lost)
        for _ in range(5):
            engine.emit(LiveEventType.ERROR, detail="boom")
        await settle()
        assert lost == ["call1"]

    @pytest.mark.asyncio
    async def test_audio_between_errors_resets_the_count(self) -> None:
        engine = FakeLiveEngine()
        lost: list[str] = []

        async def on_lost(call_id: str) -> None:
            lost.append(call_id)

        bridge, _ = await make_bridge(Recorder(engine), on_lost=on_lost)
        for _ in range(4):
            engine.emit(LiveEventType.ERROR, detail="boom")
        engine.emit(LiveEventType.AUDIO, audio=PCM, metadata={"rate": 24000})
        for _ in range(4):
            engine.emit(LiveEventType.ERROR, detail="boom")
        await settle()
        assert lost == []
        await bridge.end()

    @pytest.mark.asyncio
    async def test_the_resume_handle_is_kept_for_the_next_session(self) -> None:
        engine = FakeLiveEngine()
        bridge, _ = await make_bridge(Recorder(engine))
        engine.emit(
            LiveEventType.STATE, detail="connected", metadata={"resume_handle": "h9"}
        )
        await settle()
        assert bridge.resume_handle == "h9"
        await bridge.end()

    @pytest.mark.asyncio
    async def test_the_persona_tags_the_model_speaks_back_are_not_stored(self) -> None:
        engine = FakeLiveEngine()
        rec = Recorder(engine)
        bridge, _ = await make_bridge(rec)
        engine.emit(
            LiveEventType.TRANSCRIPT, text="how are you", metadata={"side": "input"}
        )
        engine.emit(
            LiveEventType.TRANSCRIPT,
            text="{emotion neutral intensity 3.0} fine,  thanks",
            metadata={"side": "output"},
        )
        engine.emit(LiveEventType.TURN_COMPLETE)
        await settle()
        assert [m["message_text"] for m in rec.saved] == ["how are you", "fine, thanks"]
        await bridge.end()

    @pytest.mark.asyncio
    async def test_a_call_is_found_by_its_history_path_and_told_what_happens_elsewhere(
        self,
    ) -> None:
        engine = FakeLiveEngine()
        calls = LiveCalls(Recorder(engine).ports)
        bridge = await calls.begin("call1", "chat", "chat/42", FakeSink())
        assert bridge is not None and bridge.history_path == "live_chat_42"
        assert calls.by_history_path("live_chat_42") is bridge
        assert calls.by_history_path("live_chat_43") is None
        await calls.tell_all("Marco wrote in the group")
        assert engine.texts == [("call1", "Marco wrote in the group", "context")]
        await calls.end("call1")
        await calls.tell_all("nobody is listening")  # no call: nothing happens
        assert len(engine.texts) == 1

    @pytest.mark.asyncio
    async def test_ending_twice_is_harmless(self) -> None:
        engine = FakeLiveEngine()
        rec = Recorder(engine)
        bridge, _ = await make_bridge(rec)
        engine.emit(LiveEventType.TRANSCRIPT, text="a", metadata={"side": "input"})
        engine.emit(LiveEventType.TRANSCRIPT, text="b", metadata={"side": "output"})
        await settle()
        await bridge.end()
        await bridge.end()
        assert len(rec.diaries) == 1 and engine.closed == ["call1"]


# ------------------------------------------------------------ a call held by Live


CALL_CAPS = ("interfaces.register", "interfaces.inbound", CAP_CALLS)
PATTERNS = ("chat/{chat}",)


def call_manifest() -> Manifest:
    return Manifest(
        id="dev/chat",
        name="dev/chat",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=CALL_CAPS),
        provides=Provides(interfaces=(InterfaceProvision(id="chat", paths=PATTERNS),)),
    )


class LiveRig:
    def __init__(
        self,
        env: Env,
        manager: CallManager,
        pipeline: FakePipeline,
        engine: FakeLiveEngine,
        rec: Recorder,
        calls: LiveCalls,
    ) -> None:
        self.env, self.manager, self.pipeline = env, manager, pipeline
        self.engine, self.rec, self.live = engine, rec, calls
        self.enabled = True

    async def connect(self, *, stream: bool = True) -> Client:
        client = await self.env.connect(call_manifest(), grant=CALL_CAPS)
        await client.call(
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            capabilities=["call"],
        )
        await client.call("calls.provider", interface_id="chat", stream=stream)
        return client


@pytest_asyncio.fixture
async def live_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LiveRig]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    manager, pipeline = CallManager(), FakePipeline()

    async def no_signal(session: Any, kind: str) -> None:
        return None

    monkeypatch.setattr(manager, "_play_signal", no_signal)
    monkeypatch.setattr(manager, "is_enabled", lambda: True)
    monkeypatch.setattr(manager, "_spawn", lambda coro: coro.close())
    media = MediaBridge(
        data_dir=lambda ext: tmp_path / ext.replace("/", "_"),
        uid=lambda ext: None,
        inbound_root=tmp_path / "media",
        resolve_outbound=lambda raw: (None, "no"),
    )
    register_interface_api(gateway, InterfacePorts(host, messaging, media))
    engine = FakeLiveEngine()
    rec = Recorder(engine)
    calls = LiveCalls(rec.ports)
    call_ports = CallPorts(
        manager=lambda: manager,
        pipeline=lambda: pipeline,
        video=lambda: FakeVideo(),
        jpeg_to_i420=lambda jpeg, w, h: b"",
        video_settings=lambda: None,
        live=lambda: calls,
    )
    register_call_api(gateway, call_ports, messaging, media)
    env = Env(gateway, grants, ports)
    rig = LiveRig(env, manager, pipeline, engine, rec, calls)
    monkeypatch.setattr(manager, "live_enabled", lambda: rig.enabled)
    try:
        yield rig
    finally:
        await env.close()


class TestCallsInLiveMode:
    @pytest.mark.asyncio
    async def test_a_call_is_held_by_the_live_engine_when_it_is_on(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        result = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        assert result["mode"] == "live"
        assert live_rig.live.get(result["call_id"]) is not None
        assert result["call_id"] in live_rig.engine.opened

    @pytest.mark.asyncio
    async def test_what_the_caller_says_goes_to_the_model_not_the_speech_pipeline(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        call_id = (
            await client.call(
                "calls.open", path="chat/42", user_id="42", announce=False
            )
        )["call_id"]
        raw = b"\x01\x00" * 4800  # 100 ms at 48 kHz mono
        await client.call(
            "calls.audio",
            call_id=call_id,
            pcm=b64(raw),
            rate=48000,
            speaker_id="42",
            speaker_name="Marco",
        )
        assert live_rig.pipeline.fed == []
        ((sid, chunk, rate),) = live_rig.engine.audio
        assert sid == call_id and rate == 16000 and len(chunk) == 1600 * 2

    @pytest.mark.asyncio
    async def test_the_models_voice_and_interruptions_reach_the_extension(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        seen = answering(client, {"call.stream": {}, "call.flush": {}})
        call_id = (
            await client.call(
                "calls.open", path="chat/42", user_id="42", announce=False
            )
        )["call_id"]
        live_rig.engine.emit(LiveEventType.AUDIO, audio=PCM, metadata={"rate": 24000})
        live_rig.engine.emit(LiveEventType.INTERRUPTED)
        await settle()
        assert [m for m, _ in seen] == ["call.stream", "call.flush"]
        stream = seen[0][1]
        assert stream["call_id"] == call_id and stream["interface_id"] == "chat"
        assert base64.b64decode(stream["pcm"]) == PCM and stream["rate"] == 24000
        assert seen[1][1] == {"interface_id": "chat", "call_id": call_id}

    @pytest.mark.asyncio
    async def test_an_interface_that_cannot_stream_stays_classic(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect(stream=False)
        result = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        assert result["mode"] == "classic" and live_rig.live.active() == []
        await client.call(
            "calls.audio", call_id=result["call_id"], pcm=b64(PCM), rate=16000
        )
        assert len(live_rig.pipeline.fed) == 1

    @pytest.mark.asyncio
    async def test_with_the_switch_off_the_call_stays_classic(
        self, live_rig: LiveRig
    ) -> None:
        live_rig.enabled = False
        client = await live_rig.connect()
        result = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        assert result["mode"] == "classic" and live_rig.engine.opened == {}

    @pytest.mark.asyncio
    async def test_a_refusing_engine_leaves_the_call_classic(
        self, live_rig: LiveRig
    ) -> None:
        live_rig.engine.fail_open = True
        client = await live_rig.connect()
        result = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        assert result["mode"] == "classic"
        assert live_rig.manager.get_session(result["call_id"]) is not None

    @pytest.mark.asyncio
    async def test_opening_the_same_chat_again_is_still_live(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        first = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        second = await client.call(
            "calls.open", path="chat/42", user_id="42", announce=False
        )
        assert first["call_id"] == second["call_id"] and second["mode"] == "live"
        assert len(live_rig.engine.opened) == 1

    @pytest.mark.asyncio
    async def test_ending_the_call_ends_the_session_and_writes_the_diary(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        answering(client, {"call.ended": {}})
        call_id = (
            await client.call(
                "calls.open", path="chat/42", user_id="42", announce=False
            )
        )["call_id"]
        live_rig.engine.emit(
            LiveEventType.TRANSCRIPT, text="hi", metadata={"side": "input"}
        )
        live_rig.engine.emit(
            LiveEventType.TRANSCRIPT, text="hello", metadata={"side": "output"}
        )
        await settle()
        await client.call("calls.end", call_id=call_id)
        assert live_rig.engine.closed == [call_id]
        assert live_rig.live.get(call_id) is None
        assert live_rig.rec.diaries == [("chat", "chat/42", [("hi", "hello")])]

    @pytest.mark.asyncio
    async def test_the_core_ending_the_call_ends_the_session_too(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        answering(client, {"call.ended": {}})
        call_id = (
            await client.call(
                "calls.open", path="chat/42", user_id="42", announce=False
            )
        )["call_id"]
        await live_rig.manager.end_call(call_id)
        assert live_rig.engine.closed == [call_id]

    @pytest.mark.asyncio
    async def test_a_session_lost_by_the_engine_hangs_the_call_up(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        seen = answering(client, {"call.ended": {}})
        call_id = (
            await client.call(
                "calls.open", path="chat/42", user_id="42", announce=False
            )
        )["call_id"]
        live_rig.engine.events.put_nowait(None)
        await settle()
        assert live_rig.manager.get_session(call_id) is None
        assert [m for m, _ in seen] == ["call.ended"]

    @pytest.mark.asyncio
    async def test_the_extension_going_away_ends_its_live_calls(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        call_id = (
            await client.call(
                "calls.open", path="chat/42", user_id="42", announce=False
            )
        )["call_id"]
        await client.peer.close()
        await live_rig.env.tasks[0]
        await settle()
        assert live_rig.live.get(call_id) is None
        assert live_rig.engine.closed == [call_id]

    @pytest.mark.asyncio
    async def test_the_agent_counts_as_speaking_while_the_model_talks(
        self, live_rig: LiveRig
    ) -> None:
        client = await live_rig.connect()
        call_id = (
            await client.call(
                "calls.open", path="chat/42", user_id="42", announce=False
            )
        )["call_id"]
        session = live_rig.manager.get_session(call_id)
        provider = live_rig.manager.get_provider("chat")
        assert provider is not None and session is not None
        assert provider.is_agent_speaking(session) is False
        live_rig.engine.emit(LiveEventType.AUDIO, audio=LONG, metadata={"rate": 24000})
        await settle()
        assert provider.is_agent_speaking(session) is True
