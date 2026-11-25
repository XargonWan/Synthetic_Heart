"""Tests for ``core.extensions.core_interfaces``: the real Core behind the ports (M3).

``CoreInterfaceHost`` is exercised against the Core's real registries (restored
after each test); the prompt rebuild, which needs the whole application, is
replaced by a counter. ``CoreMessaging`` only forwards, so it is checked against
the Core functions it must call, with those functions replaced.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

import core.core_initializer as ci
from core.extensions.core_interfaces import (
    CoreInterfaceHost,
    CoreMessaging,
    core_interface_ports,
)
from core.extensions.interfaces import InterfaceSpec, RemoteInterface
from core.interfaces_registry import get_interface_registry


class FakeSession:
    """The part of a gateway session a ``RemoteInterface`` touches."""

    def __init__(self) -> None:
        self.peer = SimpleNamespace(closed=False)
        self.calls: list[tuple[str, Any]] = []

    async def call(
        self, method: str, params: Any = None, *, timeout: float | None = None
    ) -> Any:
        self.calls.append((method, params))
        return {"ok": True}


def make_remote(name: str = "chat") -> RemoteInterface:
    spec = InterfaceSpec(id=name, display_name="Chat", patterns=(f"{name}/{{chat}}",))
    return RemoteInterface(FakeSession(), spec)  # type: ignore[arg-type]


@pytest.fixture
def core_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """Snapshot the Core registries, count prompt rebuilds, restore everything."""
    registry = get_interface_registry()
    snapshot = {
        "interfaces": dict(ci.INTERFACE_REGISTRY),
        "actions": dict(ci.ACTION_REGISTRY),
        "active": list(ci.core_initializer.active_interfaces),
        "components": dict(ci.core_initializer.components),
        "caps": dict(registry._interface_capabilities),  # noqa: SLF001
        "trainers": dict(registry._trainer_ids),  # noqa: SLF001
    }
    counters = {"rebuilds": 0, "invalidations": 0}

    async def fake_rebuild() -> None:
        counters["rebuilds"] += 1

    monkeypatch.setattr(ci.core_initializer, "_build_actions_block", fake_rebuild)
    monkeypatch.setattr(
        ci.core_initializer,
        "_invalidate_action_caches",
        lambda: counters.__setitem__("invalidations", counters["invalidations"] + 1),
    )
    try:
        yield counters
    finally:
        ci.INTERFACE_REGISTRY.clear()
        ci.INTERFACE_REGISTRY.update(snapshot["interfaces"])
        ci.ACTION_REGISTRY.clear()
        ci.ACTION_REGISTRY.update(snapshot["actions"])
        ci.core_initializer.active_interfaces[:] = snapshot["active"]
        ci.core_initializer.components.clear()
        ci.core_initializer.components.update(snapshot["components"])
        registry._interface_capabilities.clear()  # noqa: SLF001
        registry._interface_capabilities.update(snapshot["caps"])  # noqa: SLF001
        registry._trainer_ids.clear()  # noqa: SLF001
        registry._trainer_ids.update(snapshot["trainers"])  # noqa: SLF001


class TestCoreInterfaceHost:
    @pytest.mark.asyncio
    async def test_register_puts_the_interface_everywhere_an_in_process_one_would_be(
        self, core_state: dict[str, int]
    ) -> None:
        host = CoreInterfaceHost()
        remote = make_remote("zz_remote")
        assert host.exists("zz_remote") is False
        await host.register("zz_remote", remote)

        assert host.exists("zz_remote") is True
        assert ci.INTERFACE_REGISTRY["zz_remote"] is remote
        assert ci.ACTION_REGISTRY["send_message"] is remote
        assert "zz_remote" in ci.core_initializer.active_interfaces
        assert "send_message" in get_interface_registry().get_interface_capabilities(
            "zz_remote"
        )
        assert core_state["rebuilds"] == 1 and core_state["invalidations"] == 1

    @pytest.mark.asyncio
    async def test_unregister_undoes_it_and_keeps_trainer_ids(
        self, core_state: dict[str, int]
    ) -> None:
        host = CoreInterfaceHost()
        remote = make_remote("zz_remote")
        registry = get_interface_registry()
        registry.set_trainer_id(
            "zz_remote", "12345"
        )  # from the TRAINER_IDS configuration
        await host.register("zz_remote", remote)
        previous_action_handler = ci.ACTION_REGISTRY.get("send_message")
        assert previous_action_handler is remote

        await host.unregister("zz_remote", remote)

        assert "zz_remote" not in ci.INTERFACE_REGISTRY
        assert ci.ACTION_REGISTRY.get("send_message") is not remote
        assert "zz_remote" not in ci.core_initializer.active_interfaces
        assert registry.get_interface_capabilities("zz_remote") == frozenset()
        assert registry.get_trainer_id("zz_remote") == "12345"
        info = ci.core_initializer.components.get("zz_remote")
        assert info is not None and info.status == ci.ComponentStatus.SKIPPED
        assert core_state["rebuilds"] == 2

    @pytest.mark.asyncio
    async def test_a_stale_object_cannot_remove_a_newer_registration(
        self, core_state: dict[str, int]
    ) -> None:
        host = CoreInterfaceHost()
        old, new = make_remote("zz_remote"), make_remote("zz_remote")
        await host.register("zz_remote", old)
        await host.register("zz_remote", new)  # e.g. the extension restarted
        await host.unregister("zz_remote", old)  # the dead session's late cleanup
        assert ci.INTERFACE_REGISTRY["zz_remote"] is new
        assert core_state["rebuilds"] == 2  # the stale call did nothing

    @pytest.mark.asyncio
    async def test_unregistering_something_never_registered_is_a_no_op(
        self, core_state: dict[str, int]
    ) -> None:
        await CoreInterfaceHost().unregister("zz_never", make_remote("zz_never"))
        assert core_state["rebuilds"] == 0

    def test_the_name_resolver_is_set_and_cleared(
        self, core_state: dict[str, int]
    ) -> None:
        from core import interface_paths

        host = CoreInterfaceHost()

        async def resolver(*args: Any) -> dict[str, str | None]:
            return {}

        host.set_name_resolver("zz_remote", resolver)
        assert interface_paths.get_name_resolver("zz_remote") is resolver
        host.set_name_resolver("zz_remote", None)
        assert interface_paths.get_name_resolver("zz_remote") is None
        host.set_name_resolver("zz_remote", None)  # clearing twice is fine

    def test_an_in_process_interface_blocks_the_id(
        self, core_state: dict[str, int]
    ) -> None:
        ci.INTERFACE_REGISTRY["zz_legacy"] = object()
        assert CoreInterfaceHost().exists("zz_legacy") is True


class TestCoreMessaging:
    @pytest.mark.asyncio
    async def test_record_calls_the_chat_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import chat_context_manager

        seen: dict[str, Any] = {}

        async def fake(**kwargs: Any) -> None:
            seen.update(kwargs)

        monkeypatch.setattr(chat_context_manager, "add_message_to_context", fake)
        await CoreMessaging().record(
            "chat/r1", "hello", "Alice", "u1", "m1", {"reply_to": {}}
        )
        assert seen == {
            "interface_path": "chat/r1",
            "message_text": "hello",
            "sender_name": "Alice",
            "sender_id": "u1",
            "message_id": "m1",
            "metadata": {"reply_to": {}},
        }

    @pytest.mark.asyncio
    async def test_record_keeps_the_original_time_of_a_late_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timezone

        from core import chat_context_manager

        seen: dict[str, Any] = {}

        async def fake(**kwargs: Any) -> None:
            seen.update(kwargs)

        monkeypatch.setattr(chat_context_manager, "add_message_to_context", fake)
        when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        await CoreMessaging().record("chat/r1", "old", "Alice", "u1", "m1", None, when)
        assert seen["timestamp"] == "2026-01-02T03:04:05+00:00"

    @pytest.mark.asyncio
    async def test_history_and_path_tracking_failures_do_not_block_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import chat_context_manager, interface_paths

        async def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(chat_context_manager, "add_message_to_context", boom)
        monkeypatch.setattr(interface_paths, "resolve_and_touch", boom)
        messaging = CoreMessaging()
        await messaging.record("chat/r1", "x", "A", "1", None, None)  # no exception
        await messaging.touch("chat/r1", "r1", None, object())  # no exception

    @pytest.mark.asyncio
    async def test_touch_calls_path_tracking_with_the_bot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import interface_paths

        seen: list[tuple[Any, ...]] = []

        async def fake(
            path: str, chat: Any, thread: Any, *, bot: Any = None, **kw: Any
        ) -> None:
            seen.append((path, chat, thread, bot))

        monkeypatch.setattr(interface_paths, "resolve_and_touch", fake)
        bot = object()
        await CoreMessaging().touch("chat/r1/t1", "r1", "t1", bot)
        assert seen == [("chat/r1/t1", "r1", "t1", bot)]

    @pytest.mark.asyncio
    async def test_command_goes_to_the_command_handler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import command_registry

        seen: dict[str, Any] = {}

        async def fake(text: str, **kwargs: Any) -> str:
            seen["text"], seen["kwargs"] = text, kwargs
            return "pong"

        monkeypatch.setattr(command_registry, "handle_command_message", fake)
        context = {"bot": object()}
        assert await CoreMessaging().command("/ping", "u1", "chat", context) == "pong"
        assert seen["text"] == "/ping"
        assert seen["kwargs"] == {
            "user_id": "u1",
            "interface_id": "chat",
            "interface_context": context,
        }

    @pytest.mark.asyncio
    async def test_enqueue_goes_to_the_message_queue(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from core import message_queue

        seen: dict[str, Any] = {}

        async def fake(bot: Any, message: Any, **kwargs: Any) -> None:
            seen.update(bot=bot, message=message, **kwargs)

        monkeypatch.setattr(message_queue, "enqueue", fake)
        bot, message = object(), object()
        await CoreMessaging().enqueue(bot, message, "chat", True)
        assert seen == {
            "bot": bot,
            "message": message,
            "interface_id": "chat",
            "skip_mention_check": True,
        }

    def test_the_real_ports_are_built_without_side_effects(self) -> None:
        ports = core_interface_ports()
        assert isinstance(ports.host, CoreInterfaceHost)
        assert isinstance(ports.messaging, CoreMessaging)
