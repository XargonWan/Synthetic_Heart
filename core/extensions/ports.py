"""Ports: what the extension API needs from the Core (platform milestone M2).

The API methods in :mod:`core.extensions.api` are written against these small
interfaces instead of against ``core.db``, ``config_registry`` and friends. That
keeps the security-relevant code (namespacing, validation, quotas) testable
without a database, and lets the Core plug in its real services in one place
(:mod:`core.extensions.core_ports`).

An extension never sees a port: it only ever sees RPC methods. The gateway fills
in ``extension_id`` itself, so an extension cannot address another's data.

Like :mod:`core.extensions.manifest` this module imports nothing from ``core``
other than the manifest model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.extensions.manifest import ConfigField


class QuotaExceeded(Exception):
    """A storage write would exceed the extension's quota."""


@dataclass(frozen=True)
class StorageLimits:
    """Per-extension storage limits. A real adapter must enforce the same ones."""

    max_key_length: int = 256
    max_value_bytes: int = 256 * 1024
    max_keys: int = 10_000
    max_total_bytes: int = 5 * 1024 * 1024
    max_list_results: int = 1000


class LogPort(Protocol):
    def write(self, extension_id: str, level: str, message: str) -> None: ...


class ConfigPort(Protocol):
    def get(self, extension_id: str, field: ConfigField) -> Any:
        """Current value of one declared key, or the field's default."""
        ...


class StoragePort(Protocol):
    """Namespaced key/value storage. Values are JSON **text**.

    Every method is scoped by ``extension_id``: there is no way to name another
    extension's data. ``set`` raises :class:`QuotaExceeded` when a limit from
    :class:`StorageLimits` would be exceeded; replacing an existing key counts
    only the difference.
    """

    async def get(self, extension_id: str, key: str) -> str | None: ...

    async def set(self, extension_id: str, key: str, value_json: str) -> None: ...

    async def delete(self, extension_id: str, key: str) -> bool: ...

    async def keys(self, extension_id: str, prefix: str, limit: int) -> list[str]: ...


class StatsPort(Protocol):
    """Statistics sink. ``series`` already carries the extension prefix."""

    def incr(self, series: str, n: int, labels: dict[str, str]) -> None: ...

    def observe(self, series: str, seconds: float, labels: dict[str, str]) -> None: ...

    def gauge(self, series: str, value: float) -> None: ...


@dataclass
class ApiPorts:
    """Everything the extension API needs from the Core."""

    log: LogPort
    config: ConfigPort
    storage: StoragePort
    stats: StatsPort
