"""Interface extras: own actions, reactions, avatar, call control, late messages."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import (
    MAX_AVATAR_BYTES,
    InterfacePorts,
    RemoteInterface,
    register_interface_api,
)
from core.extensions.manifest import (
    InterfaceProvision,
    Manifest,
    Permissions,
    Provides,
)
from core.extensions.memory_ports import (
    MemoryInterfaceHost,
    MemoryMessaging,
    memory_ports,
)
from tests.test_extensions_gateway import Client, Env
from tests.test_extensions_interfaces import PATTERNS, inbound_args, register

CAPS = ("interfaces.register", "interfaces.inbound")
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
ANSWER = {
    "description": "Pick up or decline a call.",
    "fields": {
        "user_id": {"type": "string", "example": "1", "description": "Caller id."},
        "answer": {"type": "boolean", "example": True},
        "note": {"type": "string", "optional": True},
    },
    "notes": ["Act within a minute."],
}


def manifest(actions: tuple[str, ...] = ("answer_call",)) -> Manifest:
    return Manifest(
        id="dev/chat",
        name="dev/chat",
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=CAPS),
        provides=Provides(
            interfaces=(InterfaceProvision(id="chat", paths=PATTERNS, actions=actions),)
        ),
    )


class Extras:
    def __init__(self, env: Env, host: MemoryInterfaceHost, messaging: MemoryMessaging):
        self.env, self.host, self.messaging = env, host, messaging

    async def connect(self, m: Manifest | None = None) -> Client:
        m = m or manifest()
        return await self.env.connect(m, grant=m.permissions.capabilities)

    def remote(self) -> RemoteInterface:
        remote = self.host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        return remote


@pytest_asyncio.fixture
async def extras() -> AsyncIterator[Extras]:
    ports, grants = memory_ports(), MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    holder: dict[str, Extras] = {}
    register_interface_api(
        gateway,
        InterfacePorts(host, messaging),
    )
    env = Env(gateway, grants, ports)
    holder["x"] = Extras(env, host, messaging)
    try:
        yield holder["x"]
    finally:
        await env.close()


def answering(client: Client, replies: dict[str, Any]) -> list[tuple[str, Any]]:
    """Answer the Core's calls with ``replies[method]`` and record them."""
    seen: list[tuple[str, Any]] = []
    original = client.peer._handler  # noqa: SLF001

    async def handler(method: str, params: Any, notification: bool) -> Any:
        if method in replies:
            seen.append((method, dict(params)))
            reply = replies[method]
            if isinstance(reply, Exception):
                raise reply
            return reply
        return await original(method, params, notification)

    client.peer._handler = handler  # noqa: SLF001
    return seen


class TestOwnActions:
    @pytest.mark.asyncio
    async def test_declared_actions_are_offered_to_the_model(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client, actions={"answer_call": ANSWER})
        remote = extras.remote()
        offered = remote.get_supported_actions()
        assert set(offered) == {"send_message", "answer_call"}
        assert offered["answer_call"]["required_fields"] == ["user_id", "answer"]
        assert offered["answer_call"]["optional_fields"] == ["note"]
        prompt = remote.get_prompt_instructions("answer_call")
        assert prompt["payload"]["answer"]["type"] == "boolean"
        assert prompt["important_notes"] == ["Act within a minute."]

    @pytest.mark.asyncio
    async def test_an_undeclared_action_is_refused(self, extras: Extras) -> None:
        client = await extras.connect(manifest(actions=()))
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            actions={"answer_call": ANSWER},
        )
        assert "chat" not in extras.host.interfaces

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            {"answer_call": "text"},
            {"answer_call": {"fields": {"Bad-Name": {}}}},
            {"answer_call": {"fields": {"x": {"type": "float"}}}},
            {"answer_call": {"fields": [], "notes": "no"}},
        ],
    )
    async def test_malformed_definitions_are_invalid_params(
        self, extras: Extras, bad: dict[str, Any]
    ) -> None:
        client = await extras.connect()
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            actions=bad,
        )

    @pytest.mark.asyncio
    async def test_payloads_are_validated_against_the_definition(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client, actions={"answer_call": ANSWER})
        validate = extras.remote().validate_payload
        assert validate("answer_call", {"user_id": "1", "answer": True}) == []
        assert validate("answer_call", {"user_id": "1"}) == [
            "payload.answer is required"
        ]
        assert validate("answer_call", {"user_id": 1, "answer": "yes"}) == [
            "payload.user_id must be a string",
            "payload.answer must be a boolean",
        ]

    @pytest.mark.asyncio
    async def test_execution_runs_in_the_extension_and_the_result_is_clamped(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client, actions={"answer_call": ANSWER})
        seen = answering(
            client,
            {"interface.action": {"status": "success", "message": "ok" * 400, "x": 1}},
        )
        result = await extras.remote().execute_action(
            {"type": "answer_call", "payload": {"user_id": "1", "answer": True}}
        )
        assert result["status"] == "success" and len(result["message"]) == 500
        assert "x" not in result
        assert seen == [
            (
                "interface.action",
                {
                    "interface_id": "chat",
                    "name": "answer_call",
                    "payload": {"user_id": "1", "answer": True},
                    "context": {},
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_an_unknown_action_or_a_failing_extension_is_a_failed_result(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client, actions={"answer_call": ANSWER})
        answering(
            client, {"interface.action": RpcError(ErrorCode.INTERNAL_ERROR, "boom")}
        )
        remote = extras.remote()
        assert (await remote.execute_action({"type": "nope"}))["status"] == "failed"
        failed = await remote.execute_action({"type": "answer_call", "payload": {}})
        assert failed == {"status": "failed", "message": "boom"}

    @pytest.mark.asyncio
    async def test_a_media_only_message_is_valid(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client)
        validate = extras.remote().validate_payload
        assert validate("send_message", {"media": ["/x.png"]}) == []
        assert validate("send_message", {}) == [
            "payload.text or payload.media is required"
        ]


class TestReactionAvatarCalls:
    @pytest.mark.asyncio
    async def test_capabilities_use_the_cores_own_names(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client, capabilities=["reaction", "avatar", "call"])
        caps = extras.remote().get_capabilities()
        assert {"set_avatar", "reaction", "call", "send_message"} <= caps
        assert "avatar" not in caps

    @pytest.mark.asyncio
    async def test_a_reaction_reaches_the_extension(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client, capabilities=["reaction"])
        seen = answering(client, {"interface.reaction": {"ok": True}})
        message = type("M", (), {"chat_id": "-5", "message_id": "9"})()
        assert await extras.remote().add_reaction(message, "👀") is True
        assert seen == [
            (
                "interface.reaction",
                {
                    "interface_id": "chat",
                    "chat_id": "-5",
                    "message_id": "9",
                    "emoji": "👀",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_without_the_capability_nothing_is_sent(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client)
        seen = answering(
            client,
            {"interface.reaction": {"ok": True}, "interface.set_avatar": {"ok": True}},
        )
        remote = extras.remote()
        assert await remote.add_reaction(type("M", (), {})(), "x") is False
        assert await remote.set_avatar(PNG, "image/png", "v") is False
        assert remote.uses_synth_avatar() is False
        assert (await remote.start_call("1"))["status"] == "failed"
        assert seen == []

    @pytest.mark.asyncio
    async def test_the_avatar_is_handed_over_and_can_be_removed(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client, capabilities=["avatar"])
        seen = answering(client, {"interface.set_avatar": {"ok": True}})
        remote = extras.remote()
        assert remote.uses_synth_avatar() is True
        assert await remote.set_avatar(PNG, "image/png", "v2", force=True) is True
        assert await remote.set_avatar(None) is True
        first, second = (params for _, params in seen)
        assert base64.b64decode(first["image"]) == PNG
        assert (first["version"], first["force"]) == ("v2", True)
        assert "image" not in second

    @pytest.mark.asyncio
    async def test_an_oversized_avatar_is_not_sent(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client, capabilities=["avatar"])
        seen = answering(client, {"interface.set_avatar": {"ok": True}})
        assert await extras.remote().set_avatar(b"0" * (MAX_AVATAR_BYTES + 1)) is False
        assert seen == []

    @pytest.mark.asyncio
    async def test_call_control_reaches_the_extension(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client, capabilities=["call"])
        seen = answering(
            client,
            {
                "interface.start_call": {"status": "success", "message": "ringing"},
                "interface.end_call": {"status": "success", "message": "bye"},
            },
        )
        remote = extras.remote()
        assert await remote.start_call(42) == {
            "status": "success",
            "message": "ringing",
        }
        assert (await remote.end_call("42"))["message"] == "bye"
        assert [(m, p["user_id"]) for m, p in seen] == [
            ("interface.start_call", "42"),
            ("interface.end_call", "42"),
        ]


class TestLateMessages:
    @pytest.mark.asyncio
    async def test_a_backlog_message_is_history_only(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client)
        when = "2026-01-02T03:04:05+00:00"
        result = await client.call(
            "interfaces.inbound",
            **inbound_args(text="/help", timestamp=when, backlog=True),
        )
        assert result == {"stored": True}
        assert extras.messaging.enqueued == [] and extras.messaging.commands == []
        recorded = extras.messaging.recorded[0]
        assert recorded["timestamp"] == datetime.fromisoformat(when)
        assert extras.messaging.touched  # the chat is still tracked

    @pytest.mark.asyncio
    async def test_a_timestamp_sets_the_messages_date_and_never_lies_about_the_future(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client)
        await client.call(
            "interfaces.inbound", **inbound_args(timestamp="2026-01-02T03:04:05")
        )
        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        await client.call("interfaces.inbound", **inbound_args(timestamp=future))
        old, clamped = (m["message"].date for m in extras.messaging.enqueued)
        assert old == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert clamped <= datetime.now(timezone.utc)

    @pytest.mark.asyncio
    async def test_a_bad_timestamp_is_invalid_params(self, extras: Extras) -> None:
        client = await extras.connect()
        await register(client)
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(timestamp="yesterday"),
        )

    @pytest.mark.asyncio
    async def test_seen_tells_what_the_history_already_has(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client)
        when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        extras.messaging.seen_keys.add(("chat/room1", "u1", when))
        args = {"interface_id": "chat", "path": "chat/room1", "sender_id": "u1"}
        assert await client.call(
            "interfaces.seen", timestamp=when.isoformat(), **args
        ) == {"seen": True}
        assert await client.call(
            "interfaces.seen", timestamp="2026-01-02T03:04:06+00:00", **args
        ) == {"seen": False}

    @pytest.mark.asyncio
    async def test_seen_is_only_for_your_own_interface(self, extras: Extras) -> None:
        client = await extras.connect()
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "interfaces.seen",
            interface_id="chat",
            path="chat/room1",
            sender_id="u1",
            timestamp="2026-01-02T03:04:05+00:00",
        )


class TestTrainer:
    @pytest.mark.asyncio
    async def test_the_extension_can_learn_its_interfaces_trainer(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client)
        extras.messaging.trainers["chat"] = "777"
        assert await client.call("interfaces.trainer", interface_id="chat") == {
            "trainer_id": "777"
        }
        extras.messaging.trainers.clear()
        assert await client.call("interfaces.trainer", interface_id="chat") == {
            "trainer_id": None
        }

    @pytest.mark.asyncio
    async def test_only_for_an_interface_it_registered(self, extras: Extras) -> None:
        client = await extras.connect()
        await client.expect(
            ErrorCode.PERMISSION_DENIED, "interfaces.trainer", interface_id="chat"
        )


class TestInterfaceEnabled:
    @pytest.mark.asyncio
    async def test_becoming_ready_asks_the_core_to_bring_the_interface_up_to_date(
        self, extras: Extras
    ) -> None:
        client = await extras.connect()
        await register(client)
        await client.call(
            "interfaces.set_state",
            interface_id="chat",
            enabled=False,
            reason="login required",
        )
        assert extras.host.enabled_events == []
        await client.call("interfaces.set_state", interface_id="chat", enabled=True)
        await client.call(
            "interfaces.set_state", interface_id="chat", enabled=True
        )  # no change
        assert extras.host.enabled_events == ["chat"]
        assert extras.remote().is_enabled is True


class TestWaitForReply:
    """Request/response interfaces (an HTTP API): the turn's text comes back in the call."""

    async def _ready(self, extras: Extras) -> Client:
        client = await extras.connect()
        await register(client)
        return client

    @pytest.mark.asyncio
    async def test_the_reply_is_returned_and_nothing_is_delivered_separately(
        self, extras: Extras
    ) -> None:
        client = await self._ready(extras)
        extras.messaging.answer = "forty-two"
        got = await client.call(
            "interfaces.inbound", **inbound_args(wait_reply=True, reply_timeout=30)
        )
        assert got == {"queued": True, "reply": "forty-two", "pending": False}
        waited = extras.messaging.waited[0]
        assert waited["timeout"] == 30 and waited["context_memory"] is None
        assert extras.messaging.enqueued == []
        assert extras.messaging.processing == [
            ("chat/room1", True),
            ("chat/room1", False),
        ]

    @pytest.mark.asyncio
    async def test_a_timeout_is_reported_not_raised_and_processing_is_cleared(
        self, extras: Extras
    ) -> None:
        client = await self._ready(extras)
        extras.messaging.answer = asyncio.TimeoutError()
        got = await client.call(
            "interfaces.inbound", **inbound_args(wait_reply=True, reply_timeout=1)
        )
        assert got["timed_out"] is True and got["reply"] is None
        assert extras.messaging.processing[-1] == ("chat/room1", False)

    @pytest.mark.asyncio
    async def test_a_detached_agent_turn_is_pending(self, extras: Extras) -> None:
        client = await self._ready(extras)
        extras.messaging.answer = None
        extras.messaging.in_flight.add("chat/room1")
        got = await client.call("interfaces.inbound", **inbound_args(wait_reply=True))
        assert got == {"queued": True, "reply": None, "pending": True}

    @pytest.mark.asyncio
    async def test_a_command_is_answered_in_the_response(self, extras: Extras) -> None:
        client = await self._ready(extras)
        extras.messaging.command_reply = "pong"
        got = await client.call(
            "interfaces.inbound", **inbound_args(text="/ping", wait_reply=True)
        )
        assert got == {"handled": "command", "reply": "pong", "pending": False}

    @pytest.mark.asyncio
    async def test_the_clients_history_becomes_the_prompt_memory(
        self, extras: Extras
    ) -> None:
        client = await self._ready(extras)
        extras.messaging.answer = "ok"
        await client.call(
            "interfaces.inbound",
            **inbound_args(
                wait_reply=True,
                text="and now?",
                history=[
                    {"role": "user", "text": "first"},
                    {"role": "assistant", "text": "second"},
                    {"role": "user", "text": "   "},
                ],
            ),
        )
        waited = extras.messaging.waited[0]
        assert waited["history_scope"] == "local"
        [(_, memory)] = waited["context_memory"].items()
        assert [(m["role"], m["text"]) for m in memory] == [
            ("user", "first"),
            ("assistant", "second"),
            ("user", "and now?"),
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            "nope",
            [{"role": "wizard", "text": "x"}],
            [{"role": "user", "text": 3}],
            ["x"],
        ],
    )
    async def test_a_malformed_history_is_refused(
        self, extras: Extras, bad: Any
    ) -> None:
        client = await self._ready(extras)
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(wait_reply=True, history=bad),
        )
        assert extras.messaging.waited == []

    @pytest.mark.asyncio
    async def test_a_longer_wait_than_the_cap_is_refused(self, extras: Extras) -> None:
        client = await self._ready(extras)
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(wait_reply=True, reply_timeout=100000),
        )

    @pytest.mark.asyncio
    async def test_without_wait_reply_the_message_is_only_queued(
        self, extras: Extras
    ) -> None:
        client = await self._ready(extras)
        got = await client.call("interfaces.inbound", **inbound_args())
        assert got == {"queued": True}
        assert extras.messaging.waited == []
