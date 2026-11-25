"""In-memory ports (platform milestone M2).

Used by the tests and as the reference behaviour that every real adapter must
match (the storage conformance tests run against any :class:`StoragePort`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.extensions.manifest import ConfigField
from core.interface_traits import InterfaceTraits
from core.extensions.ports import ApiPorts, QuotaExceeded, StorageLimits

if TYPE_CHECKING:
    from core.extensions.interfaces import NameResolver, RemoteInterface


class MemoryLog:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, str]] = []

    def write(self, extension_id: str, level: str, message: str) -> None:
        self.records.append((extension_id, level, message))


class MemoryConfig:
    """Config values by extension id; falls back to the declared default."""

    def __init__(self, values: dict[str, dict[str, Any]] | None = None) -> None:
        self.values = values or {}

    def get(self, extension_id: str, field: ConfigField) -> Any:
        return self.values.get(extension_id, {}).get(field.key, field.default)


class MemoryStorage:
    def __init__(self, limits: StorageLimits | None = None) -> None:
        self.limits = limits or StorageLimits()
        self._data: dict[str, dict[str, str]] = {}

    async def get(self, extension_id: str, key: str) -> str | None:
        return self._data.get(extension_id, {}).get(key)

    async def set(self, extension_id: str, key: str, value_json: str) -> None:
        space = self._data.setdefault(extension_id, {})
        size = len(value_json.encode("utf-8"))
        if size > self.limits.max_value_bytes:
            raise QuotaExceeded("value is too large")
        existing = space.get(key)
        if existing is None and len(space) >= self.limits.max_keys:
            raise QuotaExceeded("too many keys")
        used = sum(len(v.encode("utf-8")) for v in space.values())
        used -= len(existing.encode("utf-8")) if existing is not None else 0
        if used + size > self.limits.max_total_bytes:
            raise QuotaExceeded("storage quota exceeded")
        space[key] = value_json

    async def delete(self, extension_id: str, key: str) -> bool:
        return self._data.get(extension_id, {}).pop(key, None) is not None

    async def keys(self, extension_id: str, prefix: str, limit: int) -> list[str]:
        space = self._data.get(extension_id, {})
        return sorted(k for k in space if k.startswith(prefix))[:limit]


class MemoryStats:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float, dict[str, str]]] = []

    def incr(self, series: str, n: int, labels: dict[str, str]) -> None:
        self.calls.append(("incr", series, float(n), dict(labels)))

    def observe(self, series: str, seconds: float, labels: dict[str, str]) -> None:
        self.calls.append(("observe", series, seconds, dict(labels)))

    def gauge(self, series: str, value: float) -> None:
        self.calls.append(("gauge", series, value, {}))


def memory_ports(limits: StorageLimits | None = None) -> ApiPorts:
    """A fresh set of in-memory ports."""
    return ApiPorts(
        log=MemoryLog(),
        config=MemoryConfig(),
        storage=MemoryStorage(limits),
        stats=MemoryStats(),
    )


class MemoryInterfaceHost:
    """A stand-in for the Core's interface registries."""

    def __init__(self, existing: tuple[str, ...] = ()) -> None:
        self.interfaces: dict[str, Any] = {name: object() for name in existing}
        self.resolvers: dict[str, NameResolver] = {}
        self.traits: dict[str, InterfaceTraits] = {}
        self.enabled_events: list[str] = []

    def exists(self, name: str) -> bool:
        return name in self.interfaces

    async def register(self, name: str, interface: RemoteInterface) -> None:
        self.interfaces[name] = interface

    async def unregister(self, name: str, interface: RemoteInterface) -> None:
        if self.interfaces.get(name) is interface:
            del self.interfaces[name]

    def interface_enabled(self, name: str) -> None:
        self.enabled_events.append(name)

    def set_traits(self, name: str, traits: InterfaceTraits | None) -> None:
        if traits is None:
            self.traits.pop(name, None)
        else:
            self.traits[name] = traits

    def set_name_resolver(self, name: str, resolver: NameResolver | None) -> None:
        if resolver is None:
            self.resolvers.pop(name, None)
        else:
            self.resolvers[name] = resolver


class MemoryMessaging:
    """Records what the Core's chat pipeline would have been asked to do."""

    def __init__(self, command_reply: str | None = None) -> None:
        self.command_reply = command_reply
        self.recorded: list[dict[str, Any]] = []
        self.touched: list[tuple[str, Any, Any]] = []
        self.commands: list[tuple[str, str, str]] = []
        self.aliases: list[tuple[str, Any]] = []
        self.seen_keys: set[tuple[str, str, Any]] = set()
        self.trainers: dict[str, str] = {}
        #: What ``enqueue_and_wait`` answers (or raises), and what it was asked.
        self.answer: str | BaseException | None = None
        self.waited: list[dict[str, Any]] = []
        self.in_flight: set[str] = set()
        self.processing: list[tuple[str, bool]] = []
        self.enqueued: list[dict[str, Any]] = []

    async def record(
        self,
        interface_path: str,
        text: str,
        sender_name: str,
        sender_id: str,
        message_id: str | None,
        metadata: dict[str, Any] | None,
        timestamp: Any = None,
    ) -> None:
        self.recorded.append(
            {
                "timestamp": timestamp,
                "interface_path": interface_path,
                "text": text,
                "sender_name": sender_name,
                "sender_id": sender_id,
                "message_id": message_id,
                "metadata": metadata,
            }
        )

    async def touch(
        self, interface_path: str, chat_id: Any, thread_id: Any, bot: Any
    ) -> None:
        self.touched.append((interface_path, chat_id, thread_id))

    async def seen(self, interface_path: str, sender_id: str, timestamp: Any) -> bool:
        return (interface_path, sender_id, timestamp) in self.seen_keys

    async def enqueue_and_wait(
        self,
        bot: Any,
        message: Any,
        interface_id: str,
        *,
        context_memory: Any,
        history_scope: str | None,
        timeout: float | None,
    ) -> str | None:
        self.waited.append(
            {
                "message": message,
                "interface_id": interface_id,
                "context_memory": context_memory,
                "history_scope": history_scope,
                "timeout": timeout,
            }
        )
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def agent_in_flight(self, interface_path: str) -> bool:
        return interface_path in self.in_flight

    async def set_processing(self, interface_path: str, processing: bool) -> None:
        self.processing.append((interface_path, processing))

    def trainer_of(self, interface_id: str) -> str | None:
        return self.trainers.get(interface_id)

    async def learn_alias(self, interface_id: str, message: Any) -> None:
        self.aliases.append((interface_id, message))

    async def command(
        self, text: str, user_id: str, interface_id: str, context: dict[str, Any]
    ) -> str | None:
        self.commands.append((text, user_id, interface_id))
        return self.command_reply

    async def enqueue(
        self, bot: Any, message: Any, interface_id: str, skip_mention_check: bool
    ) -> None:
        self.enqueued.append(
            {
                "bot": bot,
                "message": message,
                "interface_id": interface_id,
                "skip_mention_check": skip_mention_check,
            }
        )
