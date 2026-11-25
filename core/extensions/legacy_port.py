"""The Core side of the legacy settings migration (see :mod:`core.extensions.legacy`)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from core.extensions.core_ports import CoreConfig, config_key
from core.extensions.legacy import FALSE_WORDS
from core.extensions.manifest import ConfigField, load_manifest
from core.extensions.repo.installer import InstallError, Installer


async def read_rows(keys: Iterable[str]) -> dict[str, str]:
    """The stored ``config`` rows for ``keys`` (missing keys are simply absent)."""
    from core.db import get_conn_ctx

    wanted = sorted(set(keys))
    if not wanted:
        return {}
    marks = ", ".join(["%s"] * len(wanted))
    async with get_conn_ctx() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT config_key, value FROM config WHERE config_key IN ({marks})",
            tuple(wanted),
        )
        rows = await cur.fetchall()
    found: dict[str, str] = {}
    for row in rows or []:
        key = row["config_key"] if isinstance(row, dict) else row[0]
        value = row["value"] if isinstance(row, dict) else row[1]
        if value is not None:
            found[str(key)] = str(value)
    return found


class CoreLegacyPort:
    """Applies the migration to the real installer, config registry and storage."""

    def __init__(
        self,
        installer: Installer,
        registry: Any = None,
        storage: Any = None,
    ) -> None:
        self.installer = installer
        if registry is None:
            from core.config_manager import config_registry

            registry = config_registry
        if storage is None:
            from core.extensions.db_storage import SqlStorage

            storage = SqlStorage()
        self.registry = registry
        self.storage = storage

    def installed(self, extension_id: str) -> bool:
        return extension_id in self.installer.state.load().extensions

    def install(self, extension_id: str) -> str | None:
        """From a **trusted** repository only (an untrusted one needs the operator)."""
        try:
            self.installer.install(extension_id)
            self.installer.grant(extension_id)
        except InstallError as exc:
            return str(exc)[:200]
        return None

    def set_enabled(self, extension_id: str, enabled: bool) -> None:
        self.installer.set_enabled(extension_id, enabled)

    def _field(self, extension_id: str, key: str) -> ConfigField | None:
        directory: Path = self.installer.install_dir(extension_id)
        try:
            manifest = load_manifest(directory / "synth-extension.toml")
        except Exception:  # noqa: BLE001 - an unreadable manifest means nothing to set
            return None
        return next((f for f in manifest.config if f.key == key), None)

    async def copy_setting(self, extension_id: str, key: str, value: str) -> bool:
        declared = self._field(extension_id, key)
        if declared is None:
            return False
        current = CoreConfig().get(extension_id, declared)  # also registers the key
        default = declared.default if declared.default is not None else ""
        if current not in (default, "", None):
            return False  # the operator already set it: theirs is newer
        text = value.strip()
        if declared.type == "bool":
            text = "false" if text.lower() in FALSE_WORDS else "true"
        await self.registry.set_value(config_key(extension_id, key), text)
        return True

    async def copy_session(self, extension_id: str, key: str, value: str) -> bool:
        if await self.storage.get(extension_id, key) is not None:
            return False
        await self.storage.set(extension_id, key, json.dumps(value))
        return True
