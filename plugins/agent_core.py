"""Basic Agent Core plugin (minimal PoC)

- Registers config/exposed vars
- Provides attach/detach helpers for engines
- No action types exposed (PoC)
"""
from __future__ import annotations

from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_debug, log_info
import json
import asyncio

try:
    from core.variables_engine import register_exposed_var
    register_exposed_var(
        "AGENT_ENABLED",
        label="Agent Enabled",
        default=False,
        value_type=bool,
        ui_type="boolean",
        description="Enable the Agent plugin (PoC)",
        scope="agent",
        component="agent_core",
    )
    register_exposed_var(
        "AGENT_APPROVAL_MODE",
        label="Agent Approval Mode",
        default="whitelist",
        value_type=str,
        ui_type="string",
        description="Approval mode for agent proposals (always_approve|whitelist|always_ask|disabled)",
        scope="agent",
        component="agent_core",
    )
    register_exposed_var(
        "AGENT_SHELL_WHITELIST",
        label="Agent Shell Whitelist",
        default="",
        value_type=str,
        ui_type="string",
        description="Comma-separated list of approved shell commands for the agent",
        scope="agent",
        component="agent_core",
    )
    register_exposed_var(
        "AGENT_CONTAINER_REQUIRED",
        label="Agent Container Required",
        default=True,
        value_type=bool,
        ui_type="boolean",
        description="Require containerized environment to run risky actions",
        scope="agent",
        component="agent_core",
    )
except Exception:
    # tests may run before variables engine is available
    pass


class AgentCorePlugin(AIPluginBase):
    display_name = "Agent Core (PoC)"

    def __init__(self, notify_fn=None):
        super().__init__()
        self._notify_fn = notify_fn
        log_info("[agent_core] Agent Core plugin initialized (PoC)")

    def get_supported_actions(self):
        return {}

    async def _ensure_agent_tables(self) -> bool:
        try:
            from core.db import get_conn_ctx
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS agent_activity_log (
                            id BIGINT AUTO_INCREMENT PRIMARY KEY,
                            proposer VARCHAR(255) DEFAULT NULL,
                            status ENUM('proposed','approved','executed','cancelled') NOT NULL DEFAULT 'proposed',
                            command JSON,
                            response_text LONGTEXT,
                            metadata JSON,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                        )
                        """
                    )
                    await cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS agent_action_execs (
                            id BIGINT AUTO_INCREMENT PRIMARY KEY,
                            activity_log_id BIGINT NOT NULL,
                            action_index INT NOT NULL DEFAULT 0,
                            action_type VARCHAR(150) DEFAULT NULL,
                            payload JSON,
                            status ENUM('pending','processed','failed') NOT NULL DEFAULT 'pending',
                            result JSON,
                            error_text TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
            return True
        except Exception as e:
            log_debug(f"[agent_core] _ensure_agent_tables failed: {e}")
            return False

    async def _create_activity_log(self, proposer: str | None, command: dict, metadata: dict | None = None) -> int | None:
        try:
            from core.db import get_conn_ctx
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO agent_activity_log (proposer, status, command, metadata) VALUES (%s,%s,%s,%s)",
                        (proposer, 'proposed', json.dumps(command), json.dumps(metadata) if metadata else None),
                    )
                    # attempt commit if available
                    try:
                        commit_fn = getattr(conn, 'commit', None)
                        if commit_fn:
                            res = commit_fn()
                            if asyncio.iscoroutine(res):
                                await res
                    except Exception:
                        pass
                    return getattr(cur, 'lastrowid', None)
        except Exception as e:
            log_debug(f"[agent_core] _create_activity_log failed: {e}")
            return None

    async def _insert_action_exec(self, activity_log_id: int, action_index: int, action_type: str | None, payload: dict | None, status: str = 'pending', result: dict | None = None, error_text: str | None = None) -> int | None:
        try:
            from core.db import get_conn_ctx
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO agent_action_execs (activity_log_id, action_index, action_type, payload, status, error_text, result) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (
                            int(activity_log_id),
                            int(action_index),
                            action_type,
                            json.dumps(payload) if payload is not None else None,
                            status,
                            error_text,
                            json.dumps(result) if result is not None else None,
                        ),
                    )
                    try:
                        commit_fn = getattr(conn, 'commit', None)
                        if commit_fn:
                            res = commit_fn()
                            if asyncio.iscoroutine(res):
                                await res
                    except Exception:
                        pass
                    return getattr(cur, 'lastrowid', None)
        except Exception as e:
            log_debug(f"[agent_core] _insert_action_exec failed: {e}")
            return None

    async def propose_action(self, *, proposer: str | None, command: dict, metadata: dict | None = None) -> dict:
        """Create a proposal entry for an external command. Returns proposal dict."""
        ok = await self._ensure_agent_tables()
        if not ok:
            return {'status': 'error', 'reason': 'db_setup_failed'}

        aid = await self._create_activity_log(proposer, command, metadata)
        if not aid:
            return {'status': 'error', 'reason': 'insert_failed'}

        # Notify trainer if configured. Default to True if the variables engine is missing or errors.
        notify = True
        try:
            from core.variables_engine import get_exposed_var
            try:
                notify = bool(get_exposed_var('AGENT_NOTIFY_TRAINER', True))
            except Exception:
                notify = True
        except Exception:
            # variables engine not available in some test contexts - keep notify True
            notify = True

        if notify and self._notify_fn:
            try:
                self._notify_fn(f"Agent proposal created: id={aid}")
            except Exception:
                log_debug("[agent_core] notifier callback raised an exception")

        return {'status': 'proposed', 'proposal_id': aid}

    async def approve_action(self, *, proposal_id: int, approver: str | None = None, command: dict | None = None) -> dict:
        """Approve and optionally execute a proposal. If command provided it will be executed immediately."""
        # Mark approved
        try:
            from core.db import get_conn_ctx
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE agent_activity_log SET status=%s WHERE id=%s", ('approved', int(proposal_id)))
                    try:
                        commit_fn = getattr(conn, 'commit', None)
                        if commit_fn:
                            res = commit_fn()
                            if asyncio.iscoroutine(res):
                                await res
                    except Exception:
                        pass
        except Exception as e:
            log_debug(f"[agent_core] approve_action DB update failed: {e}")
            return {'status': 'error', 'reason': 'db_update_failed'}

        # Execute if a command provided
        exec_result = None
        if command is not None:
            try:
                # run command via a pluggable runner (tests will monkeypatch)
                res = await self._run_command(command)
                exec_result = res
            except Exception as e:
                exec_result = {'ok': False, 'error': str(e)}

        # Insert action exec
        await self._insert_action_exec(proposal_id, 0, 'shell_command', command or {}, 'processed' if exec_result else 'pending', exec_result, None)

        return {'status': 'executed', 'proposal_id': proposal_id, 'result': exec_result}

    async def _run_command(self, command: dict) -> dict:
        """Default runner - tests should monkeypatch this in unit tests."""
        # Very small safe default: echo the command
        return {'ok': True, 'output': str(command)}

    async def attach_to_active_engine(self, get_active_engine_name_fn) -> None:
        """Attach to active engine using provided async getter.

        The test will pass a fake `get_active_engine_name_fn` (async) that returns
        the engine name to attach to. The engine instance is looked up via
        core.cortex_registry.get_cortex_registry().get_engine(name)
        """
        try:
            engine_name = await get_active_engine_name_fn()
            if not engine_name:
                return
            from core.cortex_registry import get_cortex_registry
            registry = get_cortex_registry()
            engine = registry.get_engine(engine_name)
            if engine is None:
                log_debug(f"[agent_core] Engine {engine_name} not loaded; cannot attach")
                return
            # Prefer engine.attach_agent if available
            attach_fn = getattr(engine, "attach_agent", None)
            if attach_fn and callable(attach_fn):
                try:
                    attach_fn(self)
                    log_debug(f"[agent_core] Attached to engine {engine_name} via attach_agent")
                    return
                except Exception:
                    log_debug(f"[agent_core] Engine {engine_name} attach_agent failed, falling back to attribute set")
            # Fallback: set attribute on engine instance
            setattr(engine, "_agent_plugin", self)
            log_debug(f"[agent_core] Attached to engine {engine_name} via attribute set")
        except Exception as e:
            log_debug(f"[agent_core] attach_to_active_engine failed: {e}")

    async def handle_custom_action(self, action_type: str, payload: dict):
        """Handle agent-specific custom actions invoked via execute_action."""
        try:
            if action_type == 'approve_action':
                proposal_id = int(payload.get('proposal_id')) if payload.get('proposal_id') is not None else None
                approver = payload.get('approver')
                command = payload.get('command')
                return await self.approve_action(proposal_id=proposal_id, approver=approver, command=command)
            if action_type == 'propose_action':
                proposer = payload.get('proposer')
                command = payload.get('command')
                metadata = payload.get('metadata')
                return await self.propose_action(proposer=proposer, command=command, metadata=metadata)
            return {'status': 'error', 'reason': 'unsupported_action'}
        except Exception as e:
            log_debug(f"[agent_core] handle_custom_action failed: {e}")
            return {'status': 'error', 'reason': str(e)}
    async def detach_from_active_engine(self, get_active_engine_name_fn) -> None:
        try:
            engine_name = await get_active_engine_name_fn()
            if not engine_name:
                return
            from core.cortex_registry import get_cortex_registry
            registry = get_cortex_registry()
            engine = registry.get_engine(engine_name)
            if engine is None:
                return
            detach_fn = getattr(engine, "detach_agent", None)
            if detach_fn and callable(detach_fn):
                try:
                    detach_fn(self)
                    log_debug(f"[agent_core] Detached from engine {engine_name} via detach_agent")
                    return
                except Exception:
                    log_debug(f"[agent_core] Engine {engine_name} detach_agent failed, falling back to attribute delete")
            # Fallback
            try:
                delattr(engine, "_agent_plugin")
            except Exception:
                setattr(engine, "_agent_plugin", None)
            log_debug(f"[agent_core] Detached from engine {engine_name}")
        except Exception as e:
            log_debug(f"[agent_core] detach_from_active_engine failed: {e}")
