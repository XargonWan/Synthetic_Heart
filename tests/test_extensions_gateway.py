"""Tests for the extension gateway and the first API surface (milestone M2)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio

from synth_sdk.rpc import (
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    RpcError,
    RpcPeer,
)

from core.extensions.api import register_core_api
from core.extensions.gateway import (
    ApiMethod,
    Gateway,
    Param,
    RateLimit,
    Session,
    validate_params,
)
from core.extensions.grants import MemoryGrantStore
from core.extensions.manifest import (
    ConfigField,
    ExtensionDependency,
    Manifest,
    Permissions,
    Requires,
)
from core.extensions.memory_ports import (
    MemoryConfig,
    MemoryLog,
    MemoryStats,
    MemoryStorage,
    memory_ports,
)
from core.extensions.ports import ApiPorts, StorageLimits

ALL_CAPS = ("log", "config", "storage", "stats", "events")


class PipeWriter:
    def __init__(self, target: asyncio.StreamReader) -> None:
        self._target = target
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ConnectionResetError("closed")
        self._target.feed_data(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._target.feed_eof()


def manifest(
    ext_id: str = "dev/me",
    *,
    caps: tuple[str, ...] = ALL_CAPS,
    config: tuple[ConfigField, ...] = (),
    needs: tuple[str, ...] = (),
) -> Manifest:
    return Manifest(
        id=ext_id,
        name=ext_id,
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        requires=Requires(extensions=tuple(ExtensionDependency(id=n) for n in needs)),
        config=config,
    )


@dataclass
class Client:
    """The extension side of a connection."""

    peer: RpcPeer
    session: Session
    events: list[dict[str, Any]] = field(default_factory=list)

    async def call(self, method: str, **params: Any) -> Any:
        return await self.peer.request(method, params or None, timeout=3)

    async def expect(self, code: ErrorCode, method: str, **params: Any) -> RpcError:
        with pytest.raises(RpcError) as info:
            await self.call(method, **params)
        assert info.value.code == code, info.value
        return info.value


@dataclass
class Env:
    gateway: Gateway
    grants: MemoryGrantStore
    ports: ApiPorts
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    async def connect(
        self,
        m: Manifest | None = None,
        *,
        hello: bool = True,
        grant: tuple[str, ...] | None = None,
    ) -> Client:
        m = m or manifest()
        self.grants.grant(m.id, *(ALL_CAPS if grant is None else grant))
        reader_core = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
        reader_ext = asyncio.StreamReader(limit=MAX_MESSAGE_BYTES + 1)
        session = self.gateway.attach(m, reader_core, PipeWriter(reader_ext))
        client = Client(peer=None, session=session)  # type: ignore[arg-type]

        async def on_core_call(method: str, params: Any, notification: bool) -> Any:
            if method == "events.deliver":
                client.events.append(params)
                return None
            if method == "lifecycle.ping":
                return {"pong": True}
            raise RpcError(ErrorCode.METHOD_NOT_FOUND, method)

        client.peer = RpcPeer(
            reader_ext, PipeWriter(reader_core), on_core_call, name="ext"
        )
        self.tasks.append(asyncio.create_task(session.serve()))
        self.tasks.append(asyncio.create_task(client.peer.serve()))
        if hello:
            await client.call(
                "session.hello",
                protocol=PROTOCOL_VERSION,
                sdk_version="test",
                extension_id=m.id,
            )
        return client

    async def close(self) -> None:
        await self.gateway.close_all()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    ports = memory_ports()
    grants = MemoryGrantStore()
    gateway = Gateway(grants=grants, handler_timeout=1.0)
    register_core_api(gateway, ports)
    e = Env(gateway, grants, ports)
    try:
        yield e
    finally:
        await e.close()


class TestHandshake:
    @pytest.mark.asyncio
    async def test_calls_before_hello_are_refused(self, env: Env) -> None:
        client = await env.connect(hello=False)
        await client.expect(
            ErrorCode.INVALID_REQUEST, "log.write", level="info", message="x"
        )
        assert [r.outcome for r in env.gateway.audit] == ["handshake"]

    @pytest.mark.asyncio
    async def test_hello_reports_the_effective_capabilities(self, env: Env) -> None:
        m = manifest(caps=("log", "storage", "stats"))
        env.grants.grant(m.id, "log", "stats", "events")  # events is not declared
        client = await env.connect(m, hello=False, grant=("log", "stats", "events"))
        result = await client.call(
            "session.hello",
            protocol=PROTOCOL_VERSION,
            sdk_version="t",
            extension_id=m.id,
        )
        assert result["granted"] == ["log", "stats"]  # declared AND granted
        assert result["protocol"] == PROTOCOL_VERSION
        assert result["extension_id"] == m.id

    @pytest.mark.asyncio
    async def test_wrong_protocol_version(self, env: Env) -> None:
        client = await env.connect(hello=False)
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "session.hello",
            protocol=99,
            sdk_version="t",
            extension_id="dev/me",
        )

    @pytest.mark.asyncio
    async def test_claiming_another_identity_closes_the_connection(
        self, env: Env
    ) -> None:
        client = await env.connect(hello=False)
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "session.hello",
            protocol=PROTOCOL_VERSION,
            sdk_version="t",
            extension_id="dev/someone_else",
        )
        await asyncio.wait_for(client.session.peer.wait_closed(), 2)
        assert env.gateway.audit[-1].outcome == "handshake"

    @pytest.mark.asyncio
    async def test_hello_twice_is_refused(self, env: Env) -> None:
        client = await env.connect()
        await client.expect(
            ErrorCode.INVALID_REQUEST,
            "session.hello",
            protocol=PROTOCOL_VERSION,
            sdk_version="t",
            extension_id="dev/me",
        )

    @pytest.mark.asyncio
    async def test_one_session_per_extension(self, env: Env) -> None:
        await env.connect()
        with pytest.raises(ValueError, match="already connected"):
            await env.connect()

    @pytest.mark.asyncio
    async def test_session_is_removed_when_the_connection_closes(
        self, env: Env
    ) -> None:
        client = await env.connect()
        assert "dev/me" in env.gateway.sessions
        await client.peer.close()
        await asyncio.wait_for(client.session.peer.wait_closed(), 2)
        await asyncio.sleep(0.05)
        assert "dev/me" not in env.gateway.sessions

    @pytest.mark.asyncio
    async def test_bye_is_remembered(self, env: Env) -> None:
        client = await env.connect()
        await client.peer.notify("session.bye")
        await asyncio.sleep(0.05)
        assert client.session.said_bye is True

    def test_session_namespace_is_reserved(self, env: Env) -> None:
        async def handler(ctx: Any, args: dict[str, Any]) -> None:
            return None

        with pytest.raises(ValueError, match="reserved"):
            env.gateway.register(ApiMethod("session.evil", None, (), handler))
        with pytest.raises(ValueError, match="already registered"):
            env.gateway.register(ApiMethod("log.write", "log", (), handler))


class TestCapabilities:
    @pytest.mark.asyncio
    async def test_undeclared_capability_is_denied_even_if_granted(
        self, env: Env
    ) -> None:
        client = await env.connect(manifest(caps=("log",)), grant=ALL_CAPS)
        await client.expect(ErrorCode.PERMISSION_DENIED, "storage.get", key="k")
        assert env.gateway.audit[-1].outcome == "denied"
        assert env.gateway.audit[-1].detail == "storage"

    @pytest.mark.asyncio
    async def test_declared_but_ungranted_capability_is_denied(self, env: Env) -> None:
        client = await env.connect(manifest(caps=ALL_CAPS), grant=("log",))
        await client.expect(ErrorCode.PERMISSION_DENIED, "storage.get", key="k")
        await client.call("log.write", level="info", message="allowed")

    @pytest.mark.asyncio
    async def test_a_revoke_takes_effect_on_the_next_call(self, env: Env) -> None:
        client = await env.connect()
        assert (await client.call("storage.get", key="k"))["found"] is False
        env.grants.revoke("dev/me", "storage")
        await client.expect(ErrorCode.PERMISSION_DENIED, "storage.get", key="k")
        env.grants.grant("dev/me", "storage")
        assert (await client.call("storage.get", key="k"))["found"] is False

    @pytest.mark.asyncio
    async def test_unknown_method(self, env: Env) -> None:
        client = await env.connect()
        await client.expect(ErrorCode.METHOD_NOT_FOUND, "core.shutdown")
        assert env.gateway.audit[-1].outcome == "unknown_method"

    @pytest.mark.asyncio
    async def test_nothing_internal_is_exposed_as_a_method(self, env: Env) -> None:
        client = await env.connect()
        for name in (
            "db.query",
            "config.set",
            "os.system",
            "__import__",
            "plugins.load",
        ):
            await client.expect(ErrorCode.METHOD_NOT_FOUND, name)


class TestParameterValidation:
    @pytest.mark.asyncio
    async def test_unknown_parameter_is_refused(self, env: Env) -> None:
        client = await env.connect()
        err = await client.expect(
            ErrorCode.INVALID_PARAMS, "storage.get", key="k", extension_id="other/ext"
        )
        assert "extension_id" in err.message

    @pytest.mark.asyncio
    async def test_missing_and_wrong_types(self, env: Env) -> None:
        client = await env.connect()
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.get")
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.get", key=5)
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.keys", limit=True)
        await client.expect(
            ErrorCode.INVALID_PARAMS, "stats.gauge", name="g", value="x"
        )
        assert env.gateway.audit[-1].outcome == "invalid_params"

    def test_validator_rejects_non_finite_and_bad_shapes(self) -> None:
        async def noop(ctx: Any, args: dict[str, Any]) -> None:
            return None

        method = ApiMethod(
            "x.y",
            None,
            (
                Param("f", "float", required=False),
                Param("o", "object", required=False, max_length=1),
            ),
            noop,
        )
        for bad in (
            {"f": math.inf},
            {"f": math.nan},
            {"o": {"a": 1, "b": 2}},
            {"o": []},
        ):
            with pytest.raises(RpcError) as info:
                validate_params(method, bad)
            assert info.value.code == ErrorCode.INVALID_PARAMS
        with pytest.raises(RpcError):
            validate_params(method, [1, 2])
        assert validate_params(method, None) == {"f": None, "o": None}

    @pytest.mark.asyncio
    async def test_length_and_pattern_limits(self, env: Env) -> None:
        client = await env.connect()
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.get", key="a" * 300)
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.get", key="bad key")
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.get", key="line\nbreak")
        await client.expect(
            ErrorCode.INVALID_PARAMS, "log.write", level="info", message="x" * 5000
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS, "log.write", level="critical", message="x"
        )


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_burst_then_limited_then_refilled(self) -> None:
        clock = FakeClock()
        grants = MemoryGrantStore()
        gateway = Gateway(
            grants=grants, rate_limit=RateLimit(rate=10, burst=3), clock=clock
        )
        register_core_api(gateway, memory_ports())
        e = Env(gateway, grants, memory_ports())
        try:
            client = await e.connect(hello=False)
            await client.call(
                "session.hello",
                protocol=PROTOCOL_VERSION,
                sdk_version="t",
                extension_id="dev/me",
            )
            for _ in range(3):
                await client.call("log.write", level="info", message="x")
            await client.expect(
                ErrorCode.RATE_LIMITED, "log.write", level="info", message="x"
            )
            assert gateway.audit[-1].outcome == "rate_limited"
            clock.now += 0.5  # 5 tokens back, capped at the burst of 3
            for _ in range(3):
                await client.call("log.write", level="info", message="x")
            await client.expect(
                ErrorCode.RATE_LIMITED, "log.write", level="info", message="x"
            )
        finally:
            await e.close()


class TestHandlerFailures:
    @pytest.mark.asyncio
    async def test_a_crashing_handler_is_generic_and_audited(self) -> None:
        grants = MemoryGrantStore()
        seen: list[BaseException] = []
        gateway = Gateway(grants=grants, on_error=lambda m, e: seen.append(e))

        async def boom(ctx: Any, args: dict[str, Any]) -> None:
            raise RuntimeError("db password is hunter2")

        gateway.register(ApiMethod("test.boom", "log", (), boom))
        e = Env(gateway, grants, memory_ports())
        try:
            client = await e.connect()
            err = await client.expect(ErrorCode.INTERNAL_ERROR, "test.boom")
            assert "hunter2" not in err.message
            assert seen and isinstance(seen[0], RuntimeError)
            assert gateway.audit[-1].outcome == "failed"
            assert gateway.audit[-1].detail == "RuntimeError"  # type name only
        finally:
            await e.close()

    @pytest.mark.asyncio
    async def test_a_slow_handler_times_out(self) -> None:
        grants = MemoryGrantStore()
        gateway = Gateway(grants=grants, handler_timeout=0.05)

        async def slow(ctx: Any, args: dict[str, Any]) -> None:
            await asyncio.sleep(5)

        gateway.register(ApiMethod("test.slow", "log", (), slow))
        e = Env(gateway, grants, memory_ports())
        try:
            client = await e.connect()
            await client.expect(ErrorCode.TIMEOUT, "test.slow")
        finally:
            await e.close()

    @pytest.mark.asyncio
    async def test_a_method_can_ask_for_a_longer_timeout_than_the_default(
        self,
    ) -> None:
        grants = MemoryGrantStore()
        gateway = Gateway(grants=grants, handler_timeout=0.05)

        async def patient(ctx: Any, args: dict[str, Any]) -> str:
            await asyncio.sleep(0.2)
            return "done"

        async def impatient(ctx: Any, args: dict[str, Any]) -> str:
            await asyncio.sleep(0.2)
            return "done"

        gateway.register(ApiMethod("test.patient", "log", (), patient, timeout=2.0))
        gateway.register(ApiMethod("test.impatient", "log", (), impatient))
        e = Env(gateway, grants, memory_ports())
        try:
            client = await e.connect()
            assert await client.call("test.patient") == "done"
            await client.expect(ErrorCode.TIMEOUT, "test.impatient")
        finally:
            await e.close()

    @pytest.mark.asyncio
    async def test_audit_listener_failure_does_not_break_the_call(self) -> None:
        grants = MemoryGrantStore()

        def broken(record: Any) -> None:
            raise RuntimeError("listener down")

        gateway = Gateway(grants=grants, on_audit=broken)
        register_core_api(gateway, memory_ports())
        e = Env(gateway, grants, memory_ports())
        try:
            client = await e.connect()
            await client.expect(ErrorCode.METHOD_NOT_FOUND, "nope")
        finally:
            await e.close()


class TestStorage:
    @pytest.mark.asyncio
    async def test_round_trip_of_json_values(self, env: Env) -> None:
        client = await env.connect()
        for value in ({"a": [1, 2, {"b": None}]}, [1, "é"], "text", 3.5, None, True):
            await client.call("storage.set", key="k", value=value)
            assert await client.call("storage.get", key="k") == {
                "found": True,
                "value": value,
            }
        assert await client.call("storage.delete", key="k") == {"deleted": True}
        assert await client.call("storage.delete", key="k") == {"deleted": False}
        assert (await client.call("storage.get", key="k"))["found"] is False

    @pytest.mark.asyncio
    async def test_extensions_cannot_see_each_others_data(self, env: Env) -> None:
        a = await env.connect(manifest("dev/a"))
        b = await env.connect(manifest("dev/b"))
        await a.call("storage.set", key="shared-name", value="secret of a")
        assert (await b.call("storage.get", key="shared-name"))["found"] is False
        await b.call("storage.set", key="shared-name", value="b's own")
        assert (await a.call("storage.get", key="shared-name"))[
            "value"
        ] == "secret of a"
        assert (await b.call("storage.keys"))["keys"] == ["shared-name"]
        assert await b.call("storage.delete", key="shared-name") == {"deleted": True}
        assert (await a.call("storage.get", key="shared-name"))["found"] is True

    @pytest.mark.asyncio
    async def test_keys_with_prefix_and_limit(self, env: Env) -> None:
        client = await env.connect()
        for name in ("a/1", "a/2", "a/3", "b/1"):
            await client.call("storage.set", key=name, value=1)
        assert (await client.call("storage.keys", prefix="a/"))["keys"] == [
            "a/1",
            "a/2",
            "a/3",
        ]
        assert (await client.call("storage.keys", prefix="a/", limit=2))["keys"] == [
            "a/1",
            "a/2",
        ]
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.keys", limit=0)
        await client.expect(ErrorCode.INVALID_PARAMS, "storage.keys", limit=10**6)

    @pytest.mark.asyncio
    async def test_value_size_limit(self, env: Env) -> None:
        client = await env.connect()
        limit = StorageLimits().max_value_bytes
        await client.expect(
            ErrorCode.QUOTA_EXCEEDED, "storage.set", key="big", value="x" * limit
        )
        await client.call("storage.set", key="ok", value="x" * (limit - 100))

    @pytest.mark.asyncio
    async def test_total_and_key_count_quotas(self) -> None:
        grants = MemoryGrantStore()
        limits = StorageLimits(max_keys=3, max_total_bytes=100, max_value_bytes=60)
        ports = ApiPorts(
            log=MemoryLog(),
            config=MemoryConfig(),
            storage=MemoryStorage(limits),
            stats=MemoryStats(),
        )
        gateway = Gateway(grants=grants)
        register_core_api(gateway, ports, storage_limits=limits)
        e = Env(gateway, grants, ports)
        try:
            client = await e.connect()
            for i in range(3):
                await client.call("storage.set", key=f"k{i}", value="v")
            await client.expect(
                ErrorCode.QUOTA_EXCEEDED, "storage.set", key="k3", value="v"
            )
            await client.call(
                "storage.set", key="k0", value="replaced"
            )  # same key is fine
            await client.expect(
                ErrorCode.QUOTA_EXCEEDED, "storage.set", key="k1", value="y" * 80
            )
            await client.call("storage.delete", key="k2")
            await client.call("storage.set", key="k3", value="now fits")
        finally:
            await e.close()

    @pytest.mark.asyncio
    async def test_nan_cannot_even_be_sent(self, env: Env) -> None:
        # The wire only carries JSON, so the only reachable bad value is NaN.
        client = await env.connect()
        with pytest.raises(RpcError):
            await client.peer.request(
                "storage.set", {"key": "k", "value": float("nan")}
            )


class TestConfig:
    @pytest.mark.asyncio
    async def test_only_declared_keys_are_readable(self, env: Env) -> None:
        fields = (
            ConfigField(key="API_TOKEN", sensitive=True),
            ConfigField(key="MAX_ROOMS", type="int", default=5),
        )
        assert isinstance(env.ports.config, MemoryConfig)
        env.ports.config.values["dev/me"] = {"API_TOKEN": "tok"}
        client = await env.connect(manifest(config=fields))
        assert await client.call("config.get", key="API_TOKEN") == {"value": "tok"}
        assert await client.call("config.get", key="MAX_ROOMS") == {"value": 5}
        assert (await client.call("config.all"))["values"] == {
            "API_TOKEN": "tok",
            "MAX_ROOMS": 5,
        }
        err = await client.expect(
            ErrorCode.INVALID_PARAMS, "config.get", key="DB_PASSWORD"
        )
        assert "unknown config key" in err.message
        await client.expect(ErrorCode.INVALID_PARAMS, "config.get", key="lower")

    @pytest.mark.asyncio
    async def test_another_extensions_values_are_not_visible(self, env: Env) -> None:
        field_ = (ConfigField(key="TOKEN"),)
        assert isinstance(env.ports.config, MemoryConfig)
        env.ports.config.values["dev/a"] = {"TOKEN": "a-secret"}
        b = await env.connect(manifest("dev/b", config=field_))
        assert await b.call("config.get", key="TOKEN") == {"value": None}


class TestStats:
    @pytest.mark.asyncio
    async def test_series_are_prefixed_with_the_callers_id(self, env: Env) -> None:
        client = await env.connect(manifest("official/telegram_bot"))
        await client.call("stats.incr", name="messages", labels={"kind": "text"})
        await client.call("stats.observe", name="latency", seconds=0.25)
        await client.call("stats.gauge", name="queue", value=3)
        calls = env.ports.stats.calls  # type: ignore[attr-defined]
        assert calls == [
            ("incr", "ext.official.telegram_bot.messages", 1.0, {"kind": "text"}),
            ("observe", "ext.official.telegram_bot.latency", 0.25, {}),
            ("gauge", "ext.official.telegram_bot.queue", 3.0, {}),
        ]

    @pytest.mark.asyncio
    async def test_a_name_cannot_escape_the_prefix(self, env: Env) -> None:
        client = await env.connect()
        await client.call("stats.incr", name="core.turns.ok")
        series = env.ports.stats.calls[0][1]  # type: ignore[attr-defined]
        assert series == "ext.dev.me.core.turns.ok"
        await client.expect(ErrorCode.INVALID_PARAMS, "stats.incr", name="../x")
        await client.expect(ErrorCode.INVALID_PARAMS, "stats.incr", name="UPPER")
        await client.expect(ErrorCode.INVALID_PARAMS, "stats.incr", name="a" * 101)

    @pytest.mark.asyncio
    async def test_label_limits(self, env: Env) -> None:
        client = await env.connect()
        many = {f"l{i}": "v" for i in range(5)}
        await client.expect(
            ErrorCode.INVALID_PARAMS, "stats.incr", name="x", labels=many
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS, "stats.incr", name="x", labels={"Bad": "v"}
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS, "stats.incr", name="x", labels={"k": 5}
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS, "stats.incr", name="x", labels={"k": "v" * 65}
        )

    @pytest.mark.asyncio
    async def test_numeric_limits(self, env: Env) -> None:
        client = await env.connect()
        await client.expect(ErrorCode.INVALID_PARAMS, "stats.incr", name="x", n=10**9)
        await client.expect(
            ErrorCode.INVALID_PARAMS, "stats.observe", name="x", seconds=-1
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS, "stats.gauge", name="x", value=1e20
        )


class TestLog:
    @pytest.mark.asyncio
    async def test_log_lines_carry_the_callers_id(self, env: Env) -> None:
        client = await env.connect()
        await client.call("log.write", level="warning", message="hello")
        assert env.ports.log.records == [("dev/me", "warning", "hello")]  # type: ignore[attr-defined]


class TestEvents:
    @pytest.mark.asyncio
    async def test_publish_is_namespaced_and_delivered_to_subscribers(
        self, env: Env
    ) -> None:
        pub = await env.connect(manifest("dev/pub"))
        sub = await env.connect(manifest("dev/sub", needs=("dev/pub",)))
        await sub.call("events.subscribe", topic="ext.dev.pub.news")
        assert await pub.call("events.publish", topic="news", payload={"n": 1}) == {
            "delivered": 1
        }
        await asyncio.sleep(0.1)
        assert sub.events == [{"topic": "ext.dev.pub.news", "payload": {"n": 1}}]
        assert pub.events == []  # not subscribed to its own topic

    @pytest.mark.asyncio
    async def test_cannot_subscribe_to_a_strangers_topics(self, env: Env) -> None:
        await env.connect(manifest("dev/pub"))
        sub = await env.connect(manifest("dev/sub"))  # no dependency on dev/pub
        await sub.expect(
            ErrorCode.PERMISSION_DENIED, "events.subscribe", topic="ext.dev.pub.news"
        )
        await sub.expect(
            ErrorCode.PERMISSION_DENIED, "events.subscribe", topic="other.topic"
        )
        assert env.gateway.audit[-1].outcome == "denied"

    @pytest.mark.asyncio
    async def test_cannot_publish_outside_its_own_namespace(self, env: Env) -> None:
        a = await env.connect(manifest("dev/a"))
        b = await env.connect(manifest("dev/b"))
        await b.call("events.subscribe", topic="ext.dev.b.x")
        await b.call("events.subscribe", topic="core.config")
        # "a" can only ever publish under ext.dev.a.*, whatever it passes
        assert (await a.call("events.publish", topic="ext.dev.b.x"))["delivered"] == 0
        assert (await a.call("events.publish", topic="core.config"))["delivered"] == 0
        await asyncio.sleep(0.05)
        assert b.events == []

    @pytest.mark.asyncio
    async def test_core_topics_come_from_the_core(self, env: Env) -> None:
        sub = await env.connect()
        await sub.call("events.subscribe", topic="core.config")
        assert env.gateway.events.publish("core.config", {"changed": ["X"]}) == 1
        await asyncio.sleep(0.05)
        assert sub.events == [{"topic": "core.config", "payload": {"changed": ["X"]}}]

    @pytest.mark.asyncio
    async def test_unsubscribe(self, env: Env) -> None:
        sub = await env.connect()
        await sub.call("events.subscribe", topic="core.t")
        await sub.call("events.unsubscribe", topic="core.t")
        assert env.gateway.events.publish("core.t", 1) == 0

    @pytest.mark.asyncio
    async def test_a_stuck_subscriber_loses_events_instead_of_blocking(
        self, env: Env
    ) -> None:
        sub = await env.connect()
        await sub.call("events.subscribe", topic="core.t")
        sub.session.pending_events = env.gateway.events._max_pending  # noqa: SLF001
        assert env.gateway.events.publish("core.t", 1) == 0
        assert sub.session.dropped_events == 1

    @pytest.mark.asyncio
    async def test_subscriptions_end_with_the_session(self, env: Env) -> None:
        sub = await env.connect()
        await sub.call("events.subscribe", topic="core.t")
        await sub.peer.close()
        await asyncio.wait_for(sub.session.peer.wait_closed(), 2)
        await asyncio.sleep(0.05)
        assert env.gateway.events.publish("core.t", 1) == 0


class TestCoreToExtension:
    @pytest.mark.asyncio
    async def test_the_core_can_call_the_extension(self, env: Env) -> None:
        client = await env.connect()
        assert await client.session.call("lifecycle.ping", timeout=2) == {"pong": True}
        with pytest.raises(RpcError) as info:
            await client.session.call("lifecycle.unknown", timeout=2)
        assert info.value.code == ErrorCode.METHOD_NOT_FOUND
