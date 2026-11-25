"""Tests for ``core.extensions.interfaces``: interfaces as extensions (milestone M3).

The extension side is played by a peer on an in-memory pipe, so these tests cover
the gateway rules without starting processes. Real processes and the Core's real
dispatcher are in ``tests/test_extensions_interfaces_runtime.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from synth_sdk.rpc import ErrorCode, RpcError

from core.extensions.api import register_core_api
from core.extensions.gateway import Gateway
from core.extensions.grants import MemoryGrantStore
from core.extensions.interfaces import (
    InterfacePorts,
    InterfaceStats,
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

CAPS = ("interfaces.register", "interfaces.inbound", "log")
PATTERNS = ("chat/{chat}[/{thread}]", "chat/dm/{chat}/x")


def iface_manifest(
    ext_id: str = "dev/chat",
    *,
    interfaces: tuple[tuple[str, tuple[str, ...]], ...] = (("chat", PATTERNS),),
    caps: tuple[str, ...] = CAPS,
) -> Manifest:
    return Manifest(
        id=ext_id,
        name=ext_id,
        version="1.0.0",
        core_api=">=0.1",
        permissions=Permissions(capabilities=caps),
        provides=Provides(
            interfaces=tuple(InterfaceProvision(id=i, paths=p) for i, p in interfaces)
        ),
    )


class Rig:
    """An Env plus the interface ports, and a way to answer the Core's calls."""

    def __init__(
        self, env: Env, host: MemoryInterfaceHost, messaging: MemoryMessaging
    ) -> None:
        self.env, self.host, self.messaging = env, host, messaging
        self.stats: InterfaceStats | None = None

    async def connect(self, m: Manifest | None = None, **kw: Any) -> Client:
        m = m or iface_manifest()
        kw.setdefault("grant", m.permissions.capabilities)
        client = await self.env.connect(m, **kw)
        return client


@pytest_asyncio.fixture
async def rig() -> AsyncIterator[Rig]:
    ports = memory_ports()
    grants = MemoryGrantStore()
    gateway = Gateway(grants=grants)
    register_core_api(gateway, ports)
    host, messaging = MemoryInterfaceHost(), MemoryMessaging()
    stats = register_interface_api(gateway, InterfacePorts(host, messaging))
    env = Env(gateway, grants, ports)
    r = Rig(env, host, messaging)
    r.stats = stats
    try:
        yield r
    finally:
        await env.close()


async def register(client: Client, interface_id: str = "chat", **kw: Any) -> Any:
    params = {"display_name": "Chat", **kw}
    return await client.call("interfaces.register", interface_id=interface_id, **params)


def inbound_args(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "interface_id": "chat",
        "path": "chat/room1",
        "text": "hello",
        "sender_id": "u1",
        "sender_name": "Alice",
    }
    base.update(kw)
    return base


class TestRegistration:
    @pytest.mark.asyncio
    async def test_a_declared_interface_is_registered_as_a_remote_interface(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        result = await register(client, bot_username="synth_bot", bot_id="42")
        assert result == {"registered": "chat", "patterns": list(PATTERNS)}
        remote = rig.host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        assert remote.get_interface_id() == "chat" and remote.display_name == "Chat"
        assert remote.get_capabilities() == frozenset({"send_message"})
        me = await remote.get_me()
        assert (me.id, me.username) == ("42", "synth_bot")

    @pytest.mark.asyncio
    async def test_an_undeclared_interface_id_is_refused(self, rig: Rig) -> None:
        client = await rig.connect()
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "interfaces.register",
            interface_id="telegram_bot",
            display_name="Telegram",
        )
        assert "telegram_bot" not in rig.host.interfaces
        assert rig.env.gateway.audit[-1].outcome == "denied"

    @pytest.mark.asyncio
    async def test_the_capability_must_be_granted(self, rig: Rig) -> None:
        client = await rig.connect(iface_manifest(), grant=("log",))
        await client.expect(
            ErrorCode.PERMISSION_DENIED,
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
        )
        assert rig.host.interfaces == {}

    @pytest.mark.asyncio
    async def test_an_id_already_used_by_another_interface_is_refused(self) -> None:
        ports, grants = memory_ports(), MemoryGrantStore()
        gateway = Gateway(grants=grants)
        host = MemoryInterfaceHost(existing=("chat",))  # e.g. an in-process interface
        register_interface_api(gateway, InterfacePorts(host, MemoryMessaging()))
        env = Env(gateway, grants, ports)
        try:
            client = await env.connect(iface_manifest(), grant=CAPS)
            err = await client.expect(
                ErrorCode.INVALID_PARAMS,
                "interfaces.register",
                interface_id="chat",
                display_name="Chat",
            )
            assert "already registered" in err.message
            assert not isinstance(host.interfaces["chat"], RemoteInterface)  # untouched
        finally:
            await env.close()

    @pytest.mark.asyncio
    async def test_two_extensions_cannot_share_an_id(self, rig: Rig) -> None:
        first = await rig.connect(iface_manifest("dev/one"))
        second = await rig.connect(iface_manifest("dev/two"))
        await register(first)
        await second.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.register",
            interface_id="chat",
            display_name="X",
        )
        assert rig.host.interfaces["chat"]._session.extension_id == "dev/one"  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_registering_again_replaces_its_own_registration(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client, display_name="First")
        first = rig.host.interfaces["chat"]
        await register(client, display_name="Second")
        assert rig.host.interfaces["chat"] is not first
        assert rig.host.interfaces["chat"].display_name == "Second"

    @pytest.mark.asyncio
    async def test_media_capabilities_need_attachments_to_be_enabled(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        err = await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            capabilities=["send_message", "send_file"],
        )
        assert "attachments are not enabled" in err.message
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.register",
            interface_id="chat",
            display_name="Chat",
            capabilities="send_message",
        )

    @pytest.mark.asyncio
    async def test_parameters_are_validated(self, rig: Rig) -> None:
        client = await rig.connect()
        for bad in ("Chat", "1chat", "chat-x", "a" * 70, ""):
            await client.expect(
                ErrorCode.INVALID_PARAMS,
                "interfaces.register",
                interface_id=bad,
                display_name="x",
            )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.register",
            interface_id="chat",
            display_name="x",
            bot_username="bad name!",
        )

    @pytest.mark.asyncio
    async def test_unregister(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client, resolve_names=True)
        assert "chat" in rig.host.resolvers
        assert await client.call("interfaces.unregister", interface_id="chat") == {
            "unregistered": True
        }
        assert rig.host.interfaces == {} and rig.host.resolvers == {}
        assert await client.call("interfaces.unregister", interface_id="chat") == {
            "unregistered": False
        }

    @pytest.mark.asyncio
    async def test_it_cannot_unregister_someone_elses_interface(self, rig: Rig) -> None:
        owner = await rig.connect(iface_manifest("dev/one"))
        other = await rig.connect(
            iface_manifest("dev/two", interfaces=(("other", ()),))
        )
        await register(owner)
        assert await other.call("interfaces.unregister", interface_id="chat") == {
            "unregistered": False
        }
        assert "chat" in rig.host.interfaces

    @pytest.mark.asyncio
    async def test_a_closed_session_removes_its_interfaces(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client, resolve_names=True)
        await client.peer.close()
        await asyncio.wait_for(client.session.peer.wait_closed(), 2)
        await asyncio.sleep(0.1)
        assert rig.host.interfaces == {} and rig.host.resolvers == {}

    @pytest.mark.asyncio
    async def test_the_declaration_comes_from_the_manifest(self, rig: Rig) -> None:
        manifest = iface_manifest(interfaces=(("chat", ()),))  # no paths declared
        client = await rig.connect(manifest)
        result = await register(client)
        assert result["patterns"] == ["chat/{chat}[/{thread}]"]  # the legacy shape


class TestInbound:
    @pytest.mark.asyncio
    async def test_a_message_goes_through_the_cores_pipeline(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        result = await client.call(
            "interfaces.inbound",
            **inbound_args(
                path="chat/room1/t9",
                message_id="m7",
                sender_full_name="Alice A.",
                chat_type="group",
                chat_title="Room One",
                skip_mention_check=True,
                is_explicit_trigger=True,
                human_count=3,
            ),
        )
        assert result == {"queued": True}
        record = rig.messaging.recorded[0]
        assert record["interface_path"] == "chat/room1/t9"
        assert (record["text"], record["sender_name"], record["sender_id"]) == (
            "hello",
            "Alice",
            "u1",
        )
        assert record["message_id"] == "m7" and record["metadata"] is None
        assert rig.messaging.touched == [("chat/room1/t9", "room1", "t9")]
        queued = rig.messaging.enqueued[0]
        assert queued["interface_id"] == "chat" and queued["skip_mention_check"] is True
        assert queued["bot"] is rig.host.interfaces["chat"]
        message = queued["message"]
        assert message.interface_path == "chat/room1/t9"
        assert (message.chat_id, message.thread_id, message.message_id) == (
            "room1",
            "t9",
            "m7",
        )
        assert message.text == "hello"
        assert (message.from_user.id, message.from_user.username) == ("u1", "Alice")
        assert message.from_user.full_name == "Alice A."
        assert (message.chat.id, message.chat.type, message.chat.title) == (
            "room1",
            "group",
            "Room One",
        )
        assert message.is_explicit_trigger is True and message.human_count == 3
        assert message.reply_to_message is None

    @pytest.mark.asyncio
    async def test_defaults_follow_the_legacy_chat_and_thread_convention(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client)
        await client.call("interfaces.inbound", **inbound_args(path="chat/room1"))
        message = rig.messaging.enqueued[0]["message"]
        assert (message.chat_id, message.thread_id) == ("room1", None)
        assert message.chat.type == "private" and message.chat.title == "room1"
        assert message.from_user.full_name == "Alice"
        assert not hasattr(message, "is_explicit_trigger")
        assert rig.messaging.enqueued[0]["skip_mention_check"] is False

    @pytest.mark.asyncio
    async def test_the_extension_can_name_the_chat_and_thread_itself(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client)
        await client.call(
            "interfaces.inbound",
            **inbound_args(path="chat/dm/u1/x", chat_id="dm-u1"),
        )
        message = rig.messaging.enqueued[0]["message"]
        assert message.chat_id == "dm-u1"

    @pytest.mark.asyncio
    async def test_a_reply_is_kept_as_context_in_the_history_and_the_message(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client)
        await client.call(
            "interfaces.inbound",
            **inbound_args(
                reply_to={
                    "message_id": "m1",
                    "text": "original",
                    "sender_name": "Bob",
                    "sender_id": "u2",
                }
            ),
        )
        assert rig.messaging.recorded[0]["metadata"] == {
            "reply_to": {"sender_name": "Bob", "text": "original", "message_id": "m1"}
        }
        reply = rig.messaging.enqueued[0]["message"].reply_to_message
        assert isinstance(reply, SimpleNamespace)
        assert (reply.message_id, reply.text, reply.from_user.id) == (
            "m1",
            "original",
            "u2",
        )

    @pytest.mark.asyncio
    async def test_a_command_is_answered_without_reaching_the_queue(
        self, rig: Rig
    ) -> None:
        rig.messaging.command_reply = "pong"
        client = await rig.connect()
        sent: list[dict[str, Any]] = []
        await register(client)
        # Replies reach the extension as interface.send_message: capture them.
        original = client.peer._handler  # noqa: SLF001

        async def capture(method: str, params: Any, notification: bool) -> Any:
            if method == "interface.send_message":
                sent.append(params)
                return {"ok": True}
            return await original(method, params, notification)

        client.peer._handler = capture  # noqa: SLF001
        result = await client.call(
            "interfaces.inbound", **inbound_args(text="/ping now")
        )
        assert result == {"handled": "command"}
        assert rig.messaging.commands == [("/ping now", "u1", "chat")]
        assert rig.messaging.enqueued == []
        assert sent and sent[0]["payload"] == {
            "text": "pong",
            "interface_path": "chat/room1",
        }

    @pytest.mark.asyncio
    async def test_a_command_with_no_reply_sends_nothing(self, rig: Rig) -> None:
        rig.messaging.command_reply = None
        client = await rig.connect()
        await register(client)
        assert await client.call(
            "interfaces.inbound", **inbound_args(text="/quiet")
        ) == {"handled": "command"}

    @pytest.mark.asyncio
    async def test_the_counters_follow_what_happens(self, rig: Rig) -> None:
        assert rig.stats is not None
        client = await rig.connect()
        await register(client)
        await client.call("interfaces.inbound", **inbound_args())
        await client.call("interfaces.inbound", **inbound_args(text="/x"))
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(path="other/x"),
        )
        assert (
            rig.stats.registered,
            rig.stats.inbound,
            rig.stats.commands,
            rig.stats.rejected,
        ) == (
            1,
            2,
            1,
            1,
        )


class TestInboundIsolation:
    @pytest.mark.asyncio
    async def test_nothing_can_be_injected_before_registering(self, rig: Rig) -> None:
        client = await rig.connect()
        await client.expect(
            ErrorCode.PERMISSION_DENIED, "interfaces.inbound", **inbound_args()
        )
        assert rig.messaging.enqueued == [] and rig.messaging.recorded == []

    @pytest.mark.asyncio
    async def test_an_extension_cannot_speak_through_another_extensions_interface(
        self, rig: Rig
    ) -> None:
        owner = await rig.connect(iface_manifest("dev/one"))
        intruder = await rig.connect(
            iface_manifest("dev/two", interfaces=(("mine", ()),))
        )
        await register(owner)
        await register(intruder, "mine")
        await intruder.expect(
            ErrorCode.PERMISSION_DENIED, "interfaces.inbound", **inbound_args()
        )
        assert rig.messaging.enqueued == []
        assert rig.env.gateway.audit[-1].outcome == "denied"

    @pytest.mark.asyncio
    async def test_the_path_must_belong_to_the_interface(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        for path in ("telegram_bot/123", "chat", "chatter/1", "CHAT/1", "/chat/1"):
            await client.expect(
                ErrorCode.INVALID_PARAMS,
                "interfaces.inbound",
                **inbound_args(path=path),
            )
        # The refusal says why: it is about the interface id, not about the pattern.
        err = await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(path="telegram_bot/123"),
        )
        assert "interface id" in err.message
        assert rig.messaging.enqueued == [] and rig.messaging.recorded == []

    @pytest.mark.asyncio
    async def test_the_path_must_match_a_declared_pattern(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        for path in ("chat/a/b/c", "chat/dm/u1/y", "chat/dm/u1/x/z"):
            err = await client.expect(
                ErrorCode.INVALID_PARAMS,
                "interfaces.inbound",
                **inbound_args(path=path),
            )
            assert "declared in the manifest" in err.message
        for path in ("chat/a", "chat/a/b", "chat/dm/u1", "chat/dm/u1/x"):
            await client.call("interfaces.inbound", **inbound_args(path=path))

    @pytest.mark.asyncio
    async def test_malformed_paths_never_reach_the_history(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        for path in ("chat//a", "chat/a\nb", "chat/" + "x" * 600, "chat/a/ "):
            await client.expect(
                ErrorCode.INVALID_PARAMS,
                "interfaces.inbound",
                **inbound_args(path=path),
            )
        assert rig.messaging.recorded == []

    @pytest.mark.asyncio
    async def test_sizes_are_bounded(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(text="x" * 20_001),
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(sender_name="n" * 201),
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(chat_type="channel"),
        )
        await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(unknown_field=1),
        )

    @pytest.mark.asyncio
    async def test_the_pipeline_failure_is_generic_and_not_leaked(
        self, rig: Rig
    ) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("database password is hunter2")

        rig.messaging.enqueue = boom  # type: ignore[method-assign]
        client = await rig.connect()
        await register(client)
        err = await client.expect(
            ErrorCode.INTERNAL_ERROR, "interfaces.inbound", **inbound_args()
        )
        assert "hunter2" not in err.message


class TestOutbound:
    async def _setup(
        self, rig: Rig, answer: Any
    ) -> tuple[Client, RemoteInterface, list[dict[str, Any]]]:
        client = await rig.connect()
        await register(client)
        received: list[dict[str, Any]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "interface.send_message":
                received.append(params)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        remote = rig.host.interfaces["chat"]
        assert isinstance(remote, RemoteInterface)
        return client, remote, received

    @pytest.mark.asyncio
    async def test_only_known_fields_reach_the_extension(self, rig: Rig) -> None:
        _client, remote, received = await self._setup(rig, {"ok": True})
        origin = SimpleNamespace(message_id=55, interface_path="chat/room1")
        ok = await remote.send_message(
            {
                "text": "hi",
                "interface_path": "chat/room1",
                "reply_to": "m1",
                "media": ["/secret/file.pdf"],
                "send_as_voice": False,
                "internal": {"x": 1},
                "api_key": "sk-secret",
            },
            original_message=origin,
        )
        assert ok is True
        assert received[0]["payload"] == {
            "text": "hi",
            "interface_path": "chat/room1",
            "reply_to": "m1",
            "send_as_voice": False,
        }
        assert received[0]["interface_id"] == "chat"
        assert received[0]["origin"] == {
            "message_id": "55",
            "interface_path": "chat/room1",
        }
        assert (remote.sent, remote.failed) == (1, 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ({"ok": True}, True),
            ({"ok": False}, False),
            (True, True),
            (False, False),
            (None, False),
            ({}, False),
        ],
    )
    async def test_the_result_is_normalised_to_a_bool(
        self, rig: Rig, answer: Any, expected: bool
    ) -> None:
        _client, remote, _received = await self._setup(rig, answer)
        assert await remote.send_message({"text": "x"}) is expected

    @pytest.mark.asyncio
    async def test_a_failing_extension_is_a_failed_delivery_not_an_exception(
        self, rig: Rig
    ) -> None:
        _client, remote, _ = await self._setup(
            rig, RpcError(ErrorCode.INTERNAL_ERROR, "boom")
        )
        assert await remote.send_message({"text": "x"}) is False
        assert remote.failed == 1

    @pytest.mark.asyncio
    async def test_a_gone_extension_is_a_failed_delivery(self, rig: Rig) -> None:
        client, remote, _ = await self._setup(rig, {"ok": True})
        await client.peer.close()
        await asyncio.wait_for(client.session.peer.wait_closed(), 2)
        assert remote.connected is False
        assert await remote.send_message({"text": "x"}) is False

    @pytest.mark.asyncio
    async def test_the_older_call_style_still_works(self, rig: Rig) -> None:
        _client, remote, received = await self._setup(rig, {"ok": True})
        assert (
            await remote.send_message(channel_id="room1", thread_id="t2", text="hi")
            is True
        )
        assert received[0]["payload"] == {
            "text": "hi",
            "interface_path": "chat/room1/t2",
        }

    @pytest.mark.asyncio
    async def test_declaration_methods_for_the_cores_prompt_and_gate(
        self, rig: Rig
    ) -> None:
        _client, remote, _ = await self._setup(rig, {"ok": True})
        assert remote.validate_payload("send_message", {"text": "x"}) == []
        assert remote.validate_payload("send_message", {"text": " "}) != []
        assert remote.validate_payload("other", {}) == []
        assert remote.get_prompt_instructions("send_message")["payload"][
            "interface_path"
        ]
        assert remote.get_prompt_instructions("other") == {}
        assert "chat/{chat}" in remote.get_interface_path_hint()
        actions = remote.get_supported_actions()
        assert list(actions) == ["send_message"]
        assert "chat" in actions["send_message"]["description"]


class TestNameResolution:
    @pytest.mark.asyncio
    async def test_names_come_from_the_extension_and_fail_soft(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client, resolve_names=True)
        answers: list[Any] = [
            {
                "chat_name": "Room One",
                "message_thread_name": "Topic",
                "extra": "dropped",
            },
            {"chat_name": 5},
            "nonsense",
            RpcError(ErrorCode.INTERNAL_ERROR, "boom"),
        ]
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "interface.resolve_names":
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        resolver = rig.host.resolvers["chat"]
        assert await resolver("room1", "t1", None) == {
            "chat_name": "Room One",
            "message_thread_name": "Topic",
        }
        assert await resolver("room1", None, None) == {}
        assert await resolver("room1", None, None) == {}
        assert await resolver("room1", None, None) == {}

    @pytest.mark.asyncio
    async def test_no_resolver_unless_asked_for(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        assert rig.host.resolvers == {}


class TestTraits:
    @pytest.mark.asyncio
    async def test_the_manifest_declares_what_the_core_may_assume(
        self, rig: Rig
    ) -> None:
        manifest = Manifest(
            id="dev/chat",
            name="dev/chat",
            version="1.0.0",
            core_api=">=0.1",
            permissions=Permissions(capabilities=CAPS),
            provides=Provides(
                interfaces=(
                    InterfaceProvision(
                        id="chat",
                        paths=PATTERNS,
                        voice_input_tts_only=True,
                        negative_chat_id_is_group=True,
                        bot_username_suffix="bot",
                        peer_ids=True,
                    ),
                )
            ),
        )
        client = await rig.connect(manifest)
        await register(client)
        traits = rig.host.traits["chat"]
        assert traits.user_facing is True  # the default
        assert traits.voice_input_tts_only and traits.negative_chat_id_is_group
        assert (traits.bot_username_suffix, traits.peer_ids) == ("bot", True)

    @pytest.mark.asyncio
    async def test_an_undeclared_manifest_gets_plain_defaults(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        traits = rig.host.traits["chat"]
        assert (
            traits.voice_input_tts_only,
            traits.peer_ids,
            traits.bot_username_suffix,
        ) == (
            False,
            False,
            None,
        )

    @pytest.mark.asyncio
    async def test_unregistering_and_disconnecting_drop_the_traits(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client)
        await client.call("interfaces.unregister", interface_id="chat")
        assert "chat" not in rig.host.traits
        await register(client)
        assert "chat" in rig.host.traits
        await client.peer.close()
        await asyncio.sleep(0.05)
        assert "chat" not in rig.host.traits


class TestInboundIdentity:
    @pytest.mark.asyncio
    async def test_usernames_reach_the_message_and_aliases_are_learnt(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client)
        await client.call(
            "interfaces.inbound",
            **inbound_args(sender_username="alice_x", chat_username="room_x"),
        )
        message = rig.messaging.enqueued[0]["message"]
        assert message.from_user.username == "alice_x"
        assert message.from_user.full_name == "Alice"  # the display name is untouched
        assert message.chat.username == "room_x"
        assert [i for i, _ in rig.messaging.aliases] == ["chat"]

    @pytest.mark.asyncio
    async def test_without_usernames_nothing_is_learnt_and_the_old_shape_holds(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client)
        await client.call("interfaces.inbound", **inbound_args())
        message = rig.messaging.enqueued[0]["message"]
        assert message.from_user.username == "Alice" and message.chat.username is None
        assert rig.messaging.aliases == []


class TestActivity:
    async def _answering(self, client: Client) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "interface.activity":
                seen.append(dict(params))
                return {"ok": True}
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        return seen

    @pytest.mark.asyncio
    async def test_activity_reaches_an_interface_that_asked_for_it(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client, capabilities=["activity"])
        seen = await self._answering(client)
        await rig.host.interfaces["chat"].send_activity("chat/room1/t", "typing")
        assert seen == [
            {"interface_id": "chat", "interface_path": "chat/room1/t", "kind": "typing"}
        ]

    @pytest.mark.asyncio
    async def test_an_interface_that_did_not_ask_is_never_called(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client)
        seen = await self._answering(client)
        await rig.host.interfaces["chat"].send_activity("chat/room1", "typing")
        assert seen == []

    @pytest.mark.asyncio
    async def test_a_failing_extension_does_not_break_the_caller(
        self, rig: Rig
    ) -> None:
        client = await rig.connect()
        await register(client, capabilities=["activity"])
        original = client.peer._handler  # noqa: SLF001

        async def handler(method: str, params: Any, notification: bool) -> Any:
            if method == "interface.activity":
                raise RpcError(ErrorCode.INTERNAL_ERROR, "boom")
            return await original(method, params, notification)

        client.peer._handler = handler  # noqa: SLF001
        await rig.host.interfaces["chat"].send_activity(
            "chat/room1", "typing"
        )  # no raise


class TestVoiceInput:
    @pytest.mark.asyncio
    async def test_a_spoken_message_is_marked_for_the_queue(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        await client.call("interfaces.inbound", **inbound_args(is_voice_input=True))
        await client.call("interfaces.inbound", **inbound_args(text="typed"))
        spoken, typed = (m["message"] for m in rig.messaging.enqueued)
        assert spoken.is_voice_input is True
        assert not hasattr(typed, "is_voice_input")


class TestChatAndThreadAreNotGuessed:
    """A path's levels mean whatever the interface says; the Core reads ``{chat}`` and
    ``{thread}`` placeholders or the explicit ``chat_id`` / ``thread_id``."""

    GUILDS = (("hall", ("hall/{guild}/{channel}", "hall/{channel}")),)

    @pytest.mark.asyncio
    async def test_a_second_level_that_is_not_a_thread_is_not_made_one(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(iface_manifest(interfaces=self.GUILDS))
        await register(client, "hall")
        await client.call(
            "interfaces.inbound",
            **inbound_args(interface_id="hall", path="hall/100/500", chat_id="500"),
        )
        message = rig.messaging.enqueued[0]["message"]
        assert (message.chat_id, message.thread_id) == ("500", None)

    @pytest.mark.asyncio
    async def test_without_a_chat_placeholder_the_chat_must_be_passed(
        self, rig: Rig
    ) -> None:
        client = await rig.connect(iface_manifest(interfaces=self.GUILDS))
        await register(client, "hall")
        error = await client.expect(
            ErrorCode.INVALID_PARAMS,
            "interfaces.inbound",
            **inbound_args(interface_id="hall", path="hall/100/500"),
        )
        assert "name the chat" in error.message
        assert rig.messaging.enqueued == [] and rig.messaging.recorded == []

    @pytest.mark.asyncio
    async def test_placeholders_named_chat_and_thread_are_read(self, rig: Rig) -> None:
        client = await rig.connect()
        await register(client)
        await client.call("interfaces.inbound", **inbound_args(path="chat/room9/t3"))
        message = rig.messaging.enqueued[0]["message"]
        assert (message.chat_id, message.thread_id) == ("room9", "t3")
