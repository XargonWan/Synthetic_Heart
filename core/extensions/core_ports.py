"""The Core's real services behind the extension ports (platform milestone M2).

``core_api_ports()`` builds the :class:`ApiPorts` the gateway uses at run time:

* log    -> ``core.logging_utils`` (lines tagged ``[ext:<id>]``)
* stats  -> ``core.stats_registry`` (the existing system, series already prefixed
  by the gateway: no parallel subsystem)
* config -> ``config_registry`` under the key ``EXT__<PUBLISHER>__<NAME>__<KEY>``,
  never overridable from the environment
* storage-> :class:`core.extensions.db_storage.SqlStorage`

Every import of a Core module is inside a function so that importing this module
does not start the database or the configuration system.
"""

from __future__ import annotations

from typing import Any

from core.extensions.db_storage import SqlStorage
from core.extensions.manifest import ConfigField
from core.extensions.ports import ApiPorts, StorageLimits

_LEVELS = {
    "debug": "log_debug",
    "info": "log_info",
    "warning": "log_warning",
    "error": "log_error",
}


# Extension ids are lowercase letters, digits, "_", "-" and one "/". Escaping the
# three separators makes the mapping reversible, so two ids never share a key.
_ESCAPES = {"_": "_U", "-": "_D", "/": "_S"}


def config_key(extension_id: str, key: str) -> str:
    """The ``config_registry`` key holding an extension's setting.

    ``EXT__<escaped id>__<KEY>``. The escaped id never contains ``__`` (every
    ``_`` in it is followed by ``U``, ``D`` or ``S``), so the first ``__`` after
    the prefix always ends the id: the mapping is injective.
    """
    slug = "".join(_ESCAPES.get(char, char) for char in extension_id).upper()
    return f"EXT__{slug}__{key}"


class CoreLog:
    def write(self, extension_id: str, level: str, message: str) -> None:
        from core import logging_utils

        function = getattr(logging_utils, _LEVELS.get(level, "log_info"))
        function(f"[ext:{extension_id}] {message}")


class CoreConfig:
    """Reads an extension's declared keys from the Core configuration registry."""

    def get(self, extension_id: str, field: ConfigField) -> Any:
        from core.config_manager import config_registry

        value_types: dict[str, type] = {
            "string": str,
            "int": int,
            "float": float,
            "bool": bool,
        }
        return config_registry.get_value(
            config_key(extension_id, field.key),
            field.default if field.default is not None else "",
            label=field.label or field.key,
            description=field.description,
            value_type=value_types.get(field.type, str),
            group="extensions",
            component=f"ext:{extension_id}",
            sensitive=field.sensitive,
            allow_env_override=False,
        )


class CoreStats:
    """The Core statistics registry. ``series`` already carries ``ext.<id>.``."""

    def incr(self, series: str, n: int, labels: dict[str, str]) -> None:
        from core import stats_registry

        stats_registry.incr(series, n, **labels)

    def observe(self, series: str, seconds: float, labels: dict[str, str]) -> None:
        from core import stats_registry

        stats_registry.observe(series, seconds, **labels)

    def gauge(self, series: str, value: float) -> None:
        from core import stats_registry

        stats_registry.gauge(series, value)


def core_api_ports(limits: StorageLimits | None = None) -> ApiPorts:
    """The ports backed by the running Core."""
    return ApiPorts(
        log=CoreLog(),
        config=CoreConfig(),
        storage=SqlStorage(limits=limits),
        stats=CoreStats(),
    )
