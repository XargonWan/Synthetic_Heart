# core/external_endpoints/registry.py
"""CRUD registry for external AI service endpoints.

Handles persistence (MariaDB via ``core.db``) and synchronises additions /
removals into the four SyntH subsystem registries (Cortex, Vox, Auris, Live)
at runtime.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from core.external_endpoints.crypto import decrypt_api_key, encrypt_api_key
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.logging_utils import log_debug, log_error, log_info, log_warning

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS external_endpoints (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    display_label VARCHAR(255) NOT NULL DEFAULT '',
    protocol VARCHAR(50) NOT NULL DEFAULT 'openai',
    base_url VARCHAR(1024) NOT NULL DEFAULT '',
    api_key_enc TEXT,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    capabilities JSON,
    subsystem_map JSON,
    available_models JSON,
    default_model VARCHAR(255),
    probe_status VARCHAR(50) NOT NULL DEFAULT 'never',
    last_probe_at DATETIME,
    extra_config JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_enabled (enabled),
    INDEX idx_protocol (protocol)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


async def _ensure_table() -> None:
    from core.db import get_conn_ctx

    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_CREATE_TABLE_SQL)
        try:
            await conn.commit()
        except Exception:
            pass


def _row_to_endpoint(row: tuple, description: Any) -> ExternalEndpoint:
    """Convert a DB row tuple + cursor description to ExternalEndpoint."""
    columns = [d[0] for d in description]
    row_dict = dict(zip(columns, row))
    return ExternalEndpoint.from_row(row_dict)


class ExternalEndpointRegistry:
    """Singleton registry for external endpoints.

    All mutation methods also update the live SyntH subsystem registries
    (Cortex, Vox, Auris, Live) so engine changes take effect immediately
    without a restart.
    """

    async def _ensure(self) -> None:
        await _ensure_table()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add_endpoint(
        self,
        name: str,
        base_url: str,
        protocol: str = "openai",
        api_key: str = "",
        display_label: str = "",
        extra_config: dict[str, Any] | None = None,
        subsystem_map: dict[str, bool] | None = None,
    ) -> ExternalEndpoint:
        """Create a new external endpoint and persist it to the DB."""
        from core.db import get_conn_ctx

        await self._ensure()

        try:
            proto = EndpointProtocol(protocol)
        except ValueError:
            proto = EndpointProtocol.CUSTOM

        api_key_enc = encrypt_api_key(api_key) if api_key else None
        label = display_label or name
        extra = json.dumps(extra_config or {})
        smap = json.dumps({k: bool(v) for k, v in (subsystem_map or {}).items()})

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO external_endpoints
                      (name, display_label, protocol, base_url, api_key_enc,
                       enabled, capabilities, subsystem_map, available_models,
                       probe_status, extra_config)
                    VALUES (%s, %s, %s, %s, %s, 1,
                            '{}', %s, '[]', 'never', %s)
                    """,
                    (name, label, proto.value, base_url, api_key_enc, smap, extra),
                )
                row_id = cur.lastrowid
            try:
                await conn.commit()
            except Exception:
                pass

        ep = await self.get_endpoint(row_id)
        if ep is None:
            raise RuntimeError(
                f"[ext_endpoints] Failed to retrieve inserted endpoint '{name}'"
            )

        log_info(f"[ext_endpoints] Added endpoint '{name}' (id={row_id})")
        return ep

    async def update_endpoint(
        self,
        endpoint_id: int,
        **fields: Any,
    ) -> ExternalEndpoint | None:
        """Update an existing endpoint.  Only supplied fields are changed."""
        from core.db import get_conn_ctx

        await self._ensure()

        allowed = {
            "name",
            "display_label",
            "protocol",
            "base_url",
            "api_key",
            "enabled",
            "extra_config",
            "subsystem_map",
        }
        set_clauses: list[str] = []
        params: list[Any] = []

        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "api_key":
                set_clauses.append("api_key_enc = %s")
                params.append(encrypt_api_key(value) if value else None)
            elif key in ("extra_config", "subsystem_map"):
                set_clauses.append(f"{key} = %s")
                if isinstance(value, dict):
                    params.append(json.dumps(value))
                else:
                    params.append(json.dumps({}))
            elif key == "protocol":
                try:
                    proto = EndpointProtocol(value).value
                except ValueError:
                    proto = EndpointProtocol.CUSTOM.value
                set_clauses.append("protocol = %s")
                params.append(proto)
            else:
                set_clauses.append(f"{key} = %s")
                params.append(value)

        if not set_clauses:
            return await self.get_endpoint(endpoint_id)

        set_clauses.append("updated_at = %s")
        params.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        params.append(endpoint_id)

        sql = f"UPDATE external_endpoints SET {', '.join(set_clauses)} WHERE id = %s"
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, params)
            try:
                await conn.commit()
            except Exception:
                pass

        ep = await self.get_endpoint(endpoint_id)
        if ep is not None:
            await self._sync_registries(ep)
        log_debug(f"[ext_endpoints] Updated endpoint id={endpoint_id}")
        return ep

    async def remove_endpoint(self, endpoint_id: int) -> bool:
        """Delete an endpoint from the DB and unregister it from all subsystems."""
        from core.db import get_conn_ctx

        await self._ensure()

        ep = await self.get_endpoint(endpoint_id)
        if ep is None:
            return False

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM external_endpoints WHERE id = %s", (endpoint_id,)
                )
            try:
                await conn.commit()
            except Exception:
                pass

        self._unregister_from_all(ep.engine_name())
        log_info(f"[ext_endpoints] Removed endpoint '{ep.name}' (id={endpoint_id})")
        return True

    async def get_endpoint(self, id_or_name: int | str) -> ExternalEndpoint | None:
        """Fetch a single endpoint by id or name."""
        from core.db import get_conn_ctx

        await self._ensure()

        if isinstance(id_or_name, int):
            clause = "id = %s"
        else:
            clause = "name = %s"

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT * FROM external_endpoints WHERE {clause}",
                    (id_or_name,),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                return _row_to_endpoint(row, cur.description)

    async def get_endpoint_by_name(self, name: str) -> ExternalEndpoint | None:
        """Fetch a single endpoint by its unique name string."""
        return await self.get_endpoint(name)

    async def list_endpoints(
        self, enabled_only: bool = False
    ) -> list[ExternalEndpoint]:
        """Return all endpoints, optionally filtered to enabled ones."""
        from core.db import get_conn_ctx

        await self._ensure()

        where = "WHERE enabled = 1" if enabled_only else ""
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT * FROM external_endpoints {where} ORDER BY id"
                )
                rows = await cur.fetchall()
                if not rows:
                    return []
                desc = cur.description
                return [_row_to_endpoint(r, desc) for r in rows]

    async def set_subsystem_map(
        self, endpoint_id: int, mapping: dict[str, bool]
    ) -> None:
        """Persist a user-defined subsystem mapping and re-sync registries."""
        from core.db import get_conn_ctx

        await self._ensure()

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE external_endpoints SET subsystem_map = %s, "
                    "updated_at = %s WHERE id = %s",
                    (
                        json.dumps(mapping),
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        endpoint_id,
                    ),
                )
            try:
                await conn.commit()
            except Exception:
                pass

        ep = await self.get_endpoint(endpoint_id)
        if ep is not None:
            await self._sync_registries(ep)
        log_debug(f"[ext_endpoints] Updated subsystem_map for id={endpoint_id}")

    async def set_probe_result(
        self,
        endpoint_id: int,
        status: str,
        capabilities: dict[str, bool],
        models: list[str],
    ) -> None:
        """Persist probe results and sync registries."""
        from core.db import get_conn_ctx

        await self._ensure()

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE external_endpoints
                    SET probe_status = %s, capabilities = %s, available_models = %s,
                        last_probe_at = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (
                        status,
                        json.dumps(capabilities),
                        json.dumps(models),
                        now,
                        now,
                        endpoint_id,
                    ),
                )
            try:
                await conn.commit()
            except Exception:
                pass

        ep = await self.get_endpoint(endpoint_id)
        if ep is not None:
            # Auto-select the first available model when none has been set yet
            if status == "success" and models and ep.default_model is None:
                await self._auto_set_default_model(endpoint_id, models[0])
                ep = await self.get_endpoint(endpoint_id)
            await self._sync_registries(ep)

    async def _auto_set_default_model(self, endpoint_id: int, model: str) -> None:
        """Persist an automatically selected default model (probe post-processing)."""
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE external_endpoints SET default_model = %s, "
                    "updated_at = %s WHERE id = %s AND default_model IS NULL",
                    (
                        model,
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        endpoint_id,
                    ),
                )
            try:
                await conn.commit()
            except Exception:
                pass
        log_info(
            f"[ext_endpoints] Auto-selected default_model='{model}' for id={endpoint_id}"
        )

    async def set_default_model(self, endpoint_id: int, model: str) -> None:
        """Set the active model for an endpoint."""
        from core.db import get_conn_ctx

        await self._ensure()

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE external_endpoints SET default_model = %s, "
                    "updated_at = %s WHERE id = %s",
                    (
                        model or None,
                        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                        endpoint_id,
                    ),
                )
            try:
                await conn.commit()
            except Exception:
                pass
        log_debug(f"[ext_endpoints] Set default_model='{model}' for id={endpoint_id}")

    # ------------------------------------------------------------------
    # Registry synchronisation
    # ------------------------------------------------------------------

    async def register_all_enabled(self) -> None:
        """Load all enabled endpoints and register them in SyntH subsystems.

        Called once at startup by core_initializer.
        """
        try:
            endpoints = await self.list_endpoints(enabled_only=True)
        except Exception as exc:
            log_warning(f"[ext_endpoints] Could not load endpoints from DB: {exc}")
            return

        for ep in endpoints:
            try:
                await self._sync_registries(ep)
            except Exception as exc:
                log_warning(
                    f"[ext_endpoints] Failed to register endpoint '{ep.name}': {exc}"
                )

        log_info(
            f"[ext_endpoints] Registered {len(endpoints)} external endpoint(s) "
            "into SyntH subsystems"
        )

    async def _sync_registries(self, ep: ExternalEndpoint) -> None:
        """Register or re-register the endpoint in the appropriate subsystems.

        The mapping is derived from ``ep.effective_subsystem_map()``.
        Old registrations are always removed first to ensure a clean state.
        """
        engine_name = ep.engine_name()
        self._unregister_from_all(engine_name)

        if not ep.enabled:
            log_debug(
                f"[ext_endpoints] Endpoint '{ep.name}' is disabled – not registering"
            )
            return

        effective = ep.effective_subsystem_map()
        api_key = decrypt_api_key(ep.api_key_enc or "")

        from core.external_endpoints.adapters.base import BaseProtocolAdapter
        from core.external_endpoints.probe import get_adapter_for_endpoint

        try:
            adapter: BaseProtocolAdapter = get_adapter_for_endpoint(ep, api_key)
        except Exception as exc:
            log_error(f"[ext_endpoints] Cannot build adapter for '{ep.name}': {exc}")
            return

        label = ep.display_label or ep.name

        if effective.get("cortex"):
            try:
                from core.cortex_registry import get_cortex_registry
                from core.external_endpoints.bridges.cortex_bridge import (
                    ExternalCortexEngine,
                )

                bridge = ExternalCortexEngine(ep, adapter)
                get_cortex_registry().register_instance(
                    engine_name, bridge, cortex="llm_provider", label=label
                )
                log_info(f"[ext_endpoints] '{ep.name}' registered as Cortex engine")
            except Exception as exc:
                log_warning(
                    f"[ext_endpoints] Cortex registration failed for '{ep.name}': {exc}"
                )

        if effective.get("vox"):
            try:
                from core.external_endpoints.bridges.vox_bridge import ExternalVoxEngine
                from core.vox_registry import VOX_REGISTRY

                vox_bridge = ExternalVoxEngine(ep, adapter)
                VOX_REGISTRY.register_instance(engine_name, vox_bridge, label=label)
                log_info(f"[ext_endpoints] '{ep.name}' registered as Vox engine")
            except Exception as exc:
                log_warning(
                    f"[ext_endpoints] Vox registration failed for '{ep.name}': {exc}"
                )

        if effective.get("auris"):
            try:
                from core.auris_registry import AURIS_REGISTRY
                from core.external_endpoints.bridges.auris_bridge import (
                    ExternalAurisEngine,
                )

                auris_bridge = ExternalAurisEngine(ep, adapter)
                AURIS_REGISTRY.register_instance(engine_name, auris_bridge, label=label)
                log_info(f"[ext_endpoints] '{ep.name}' registered as Auris engine")
            except Exception as exc:
                log_warning(
                    f"[ext_endpoints] Auris registration failed for '{ep.name}': {exc}"
                )

        if effective.get("live"):
            try:
                from core.external_endpoints.bridges.live_bridge import (
                    ExternalLiveEngine,
                )
                from core.live_registry import LIVE_REGISTRY

                live_bridge = ExternalLiveEngine(ep, adapter)
                LIVE_REGISTRY.register_instance(engine_name, live_bridge, label=label)
                log_info(f"[ext_endpoints] '{ep.name}' registered as Live engine")
            except Exception as exc:
                log_warning(
                    f"[ext_endpoints] Live registration failed for '{ep.name}': {exc}"
                )

    def _unregister_from_all(self, engine_name: str) -> None:
        """Remove an engine from all subsystem registries."""
        _remove_from_cortex(engine_name)
        _remove_from_vox(engine_name)
        _remove_from_auris(engine_name)
        _remove_from_live(engine_name)


def _remove_from_cortex(name: str) -> None:
    try:
        from core.cortex_registry import get_cortex_registry

        get_cortex_registry().unregister_engine(name)
    except Exception:
        pass


def _remove_from_vox(name: str) -> None:
    try:
        from core.vox_registry import VOX_REGISTRY

        VOX_REGISTRY.unregister_engine(name)
    except Exception:
        pass


def _remove_from_auris(name: str) -> None:
    try:
        from core.auris_registry import AURIS_REGISTRY

        AURIS_REGISTRY.unregister_engine(name)
    except Exception:
        pass


def _remove_from_live(name: str) -> None:
    try:
        from core.live_registry import LIVE_REGISTRY

        LIVE_REGISTRY.unregister_engine(name)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: ExternalEndpointRegistry | None = None


def get_external_endpoint_registry() -> ExternalEndpointRegistry:
    global _registry
    if _registry is None:
        _registry = ExternalEndpointRegistry()
    return _registry
