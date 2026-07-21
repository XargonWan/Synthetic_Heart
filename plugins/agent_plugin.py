# plugins/agent_plugin.py

import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
from collections.abc import Callable
from typing import Optional, Dict, Any

from core.ai_plugin_base import AIPluginBase
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.core_initializer import register_plugin
from core.config_manager import config_registry

# Expose config variables (registration best-effort)
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "AGENT_ENABLED",
        label="Enable Agent",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable the Agent plugin (default: enabled only in container)",
        scope="agent",
        component="agent",
        needs_component_reload=True,
    )
    register_exposed_var(
        "AGENT_APPROVAL_MODE",
        label="Agent approval mode",
        default="whitelist",
        value_type=str,
        ui_type="select",
        options=["always_approve", "whitelist", "always_ask", "disabled"],
        description="Approval policy for agent-executed commands",
        scope="agent",
        component="agent",
        needs_component_reload=True,
    )
    register_exposed_var(
        "AGENT_SHELL_WHITELIST",
        label="Agent shell whitelist",
        default="ls,cat,df -h,free -m,uptime,whoami,id",
        value_type=str,
        ui_type="string",
        description="Comma-separated list of allowed shell commands when in whitelist mode",
        scope="agent",
        component="agent",
        needs_component_reload=True,
    )
    register_exposed_var(
        "AGENT_CONTAINER_REQUIRED",
        label="Require container to enable shell execution",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="If true, shell execution will be disabled when not detected inside a container",
        scope="agent",
        component="agent",
        needs_component_reload=True,
    )
    # Engine override for the agentic loop. "Default" == use the active Cortex
    # engine (BASE_CORTEX); any other value must be a Cortex engine registered
    # in the Cortex registry. The WebUI populates the option list with the
    # registered Cortex engines (see core/webui.py cortex selector block).
    register_exposed_var(
        "AGENT_CORTEX",
        label="Agent engine (Cortex override)",
        default="Default",
        value_type=str,
        ui_type="select",
        options=["Default"],
        description=(
            "Which Cortex engine the agentic loop uses. 'Default' reuses the "
            "active Cortex engine; pick another registered Cortex engine to run "
            "the agent on an LLM better suited for tool-calling/agent work."
        ),
        scope="agent",
        component="agent",
        needs_component_reload=True,
    )
except Exception:
    # tests / import-time safety
    pass


def _in_container() -> bool:
    try:
        if os.path.exists("/.dockerenv"):
            return True
        cgroup = "/proc/self/cgroup"
        if os.path.isfile(cgroup):
            with open(cgroup, "r") as fh:
                data = fh.read()
            if "docker" in data or "kubepods" in data or "containerd" in data:
                return True
    except Exception as e:
        log_debug(f"[agent] Container detection failed: {e}")
    return False


def _safe_int(value: Any, default: int, *, min_value: int, max_value: int) -> int:
    """Parse an int safely and clamp to bounds."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


class AgentPlugin(AIPluginBase):
    display_name = "Agent Plugin"

    def __init__(self, notify_fn: Optional[Callable[[str], None]] = None):
        # Prefer core.notifier.notify_trainer when available
        if notify_fn:
            self._notify_fn = notify_fn
        else:
            try:
                from core.notifier import notify_trainer

                self._notify_fn = notify_trainer
            except Exception:
                self._notify_fn = lambda msg: log_info(f"[NOTIFY fallback] {msg}")
        register_plugin("agent", self)

        # Config-derived state
        self._in_container = _in_container()
        self._enabled = (
            bool(config_registry.get_var("AGENT_ENABLED", True)) and self._in_container
        )
        self._approval_mode = str(
            config_registry.get_var("AGENT_APPROVAL_MODE", "whitelist")
        )
        self._whitelist_raw = str(
            config_registry.get_var(
                "AGENT_SHELL_WHITELIST", "ls,cat,df -h,free -m,uptime,whoami,id"
            )
        )
        self._whitelist = [
            c.strip() for c in self._whitelist_raw.split(",") if c.strip()
        ]
        self._container_required = bool(
            config_registry.get_var("AGENT_CONTAINER_REQUIRED", True)
        )

        # If not in container and container_required => disable shell execution by default
        if not self._in_container and self._container_required:
            self._enabled = False

        self._engine: Any | None = None
        self._attached_engine: str | None = None
        self._attach_task: asyncio.Task[Any] | None = None

        log_info(
            f"[agent] Initialized. in_container={self._in_container} enabled={self._enabled} approval_mode={self._approval_mode}"
        )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self._attach_task = loop.create_task(self._attach_to_active_engine())

    def _refresh_runtime_settings(self) -> None:
        """Refresh policy/config values so WebUI toggles apply at runtime."""
        try:
            self._approval_mode = str(
                config_registry.get_var("AGENT_APPROVAL_MODE", "whitelist")
            )
            self._whitelist_raw = str(
                config_registry.get_var(
                    "AGENT_SHELL_WHITELIST", "ls,cat,df -h,free -m,uptime,whoami,id"
                )
            )
            self._whitelist = [
                c.strip() for c in self._whitelist_raw.split(",") if c.strip()
            ]
            self._container_required = bool(
                config_registry.get_var("AGENT_CONTAINER_REQUIRED", True)
            )
            self._enabled = bool(config_registry.get_var("AGENT_ENABLED", True))
        except Exception:
            # Keep the last known settings if config reads fail transiently.
            pass

    def is_enabled(self) -> bool:
        """Only expose agent actions when the agent is toggled on.

        Without this, ``core_initializer`` defaults the plugin to enabled and
        injects ``agent_execute`` / ``propose_action`` / ``approve_action`` into
        every prompt even when ``AGENT_ENABLED`` is off or we are not running in
        a container — bloating the tool block for small local LLMs.
        """
        self._refresh_runtime_settings()
        if self._container_required and not self._in_container:
            return False
        return bool(self._enabled)

    async def _attach_to_active_engine(self) -> None:
        try:
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            engine_name = await get_active_cortex_engine()
            if not engine_name:
                return

            registry = get_cortex_registry()
            engine = None
            if hasattr(registry, "get_engine"):
                engine = registry.get_engine(engine_name)
            elif hasattr(registry, "load_engine"):
                engine = registry.load_engine(engine_name)

            if engine is None:
                log_debug(f"[agent] Active engine '{engine_name}' not loaded")
                return

            attach_fn = getattr(engine, "attach_agent", None)
            if callable(attach_fn):
                attach_fn(self)
            else:
                setattr(engine, "_agent_plugin", self)

            self._engine = engine
            self._attached_engine = str(engine_name)
            log_debug(f"[agent] Attached to engine '{self._attached_engine}'")
        except Exception as e:
            log_warning(f"[agent] Failed to attach to active engine: {e}")

    async def _get_conn_ctx(self) -> Any:
        try:
            import core as core_package

            db_module = getattr(core_package, "db", None)
        except Exception:
            db_module = None

        if db_module is None:
            db_module = sys.modules.get("core.db")
        if db_module is None:
            db_module = importlib.import_module("core.db")

        get_conn_ctx = getattr(db_module, "get_conn_ctx")
        conn_ctx = get_conn_ctx()
        if asyncio.iscoroutine(conn_ctx):
            conn_ctx = await conn_ctx
        return conn_ctx

    def get_supported_action_types(self) -> list[str]:
        return [
            "agent_execute",
            "propose_action",
            "approve_action",
            "start_task",
            "agent_list_files",
            "agent_read_file",
        ]

    def get_supported_actions(self) -> Dict[str, Any]:
        return {
            "agent_execute": {
                "required_fields": ["command"],
                "optional_fields": ["description", "requires_approval"],
                "description": "Execute a shell command (subject to policy).",
            },
            "propose_action": {
                "required_fields": ["command"],
                "optional_fields": ["description"],
                "description": "Propose an action that requires approval before execution.",
            },
            "approve_action": {
                "required_fields": ["proposal_id"],
                "optional_fields": [],
                "description": "Approve a previously proposed action.",
            },
            "reject_action": {
                "required_fields": ["proposal_id"],
                "optional_fields": [],
                "description": "Reject a previously proposed action without executing it.",
            },
            "agent_list_files": {
                "required_fields": [],
                "optional_fields": ["path", "recursive", "max_depth", "limit"],
                "description": "List files/directories within the allowed agent filesystem roots.",
            },
            "agent_read_file": {
                "required_fields": ["path"],
                "optional_fields": ["start_line", "end_line", "max_chars"],
                "description": "Read a text file within the allowed agent filesystem roots.",
            },
            "spawn_drone": {
                "required_fields": ["goal"],
                "optional_fields": ["engine", "max_iterations"],
                "security_level": "medium",
                "external_effects": ["drone"],
                "description": (
                    "Delegate a focused sub-task to an ephemeral sub-agent (a 'Drone'). "
                    "The Drone runs its own bounded agentic loop with the available tools "
                    "and returns a concise result. Use this to isolate a self-contained "
                    "piece of work (research, a multi-step lookup, a scoped file inspection) "
                    "so the main task stays clean. A Drone CANNOT spawn further Drones. "
                    "Provide a clear, self-contained 'goal'."
                ),
            },
        }

    def _allowed_roots(self) -> list[Path]:
        """Return the filesystem roots the agent is allowed to read."""
        roots_raw = os.getenv("AGENT_FS_ROOTS")
        if roots_raw:
            roots = [p.strip() for p in roots_raw.split(":") if p.strip()]
        else:
            roots = [
                os.getenv("AGENT_FS_ROOT", "/app"),
                os.getenv("SYNTH_LOG_DIR", "/app/logs"),
            ]

        out: list[Path] = []
        for root in roots:
            try:
                out.append(Path(root).resolve())
            except Exception:
                continue
        return out

    def _resolve_safe_path(self, raw_path: str) -> tuple[Path | None, str | None]:
        """Resolve a user path and ensure it stays inside allowed roots."""
        if not raw_path or not str(raw_path).strip():
            return None, "Missing path"

        p = Path(str(raw_path).strip())
        if not p.is_absolute():
            # Relative paths are resolved against first allowed root.
            roots = self._allowed_roots()
            if not roots:
                return None, "No allowed roots configured"
            p = roots[0] / p

        try:
            resolved = p.resolve()
        except Exception as exc:
            return None, f"Invalid path: {exc}"

        for root in self._allowed_roots():
            try:
                resolved.relative_to(root)
                return resolved, None
            except ValueError:
                continue

        return None, "Path is outside allowed roots"

    def _list_files(
        self, base: Path, *, recursive: bool, max_depth: int, limit: int
    ) -> list[str]:
        """Return a bounded directory listing rooted at ``base``."""
        if not base.exists():
            return []
        if base.is_file():
            return [str(base)]

        results: list[str] = []

        def _walk(path: Path, depth: int) -> None:
            if len(results) >= limit:
                return
            if depth > max_depth:
                return
            try:
                entries = sorted(path.iterdir(), key=lambda x: x.name.lower())
            except Exception:
                return

            for entry in entries:
                if len(results) >= limit:
                    return
                marker = "/" if entry.is_dir() else ""
                results.append(str(entry) + marker)
                if recursive and entry.is_dir():
                    _walk(entry, depth + 1)

        _walk(base, 0)
        return results

    def get_prompt_instructions(self, action_name: str) -> dict:
        enabled = self.is_enabled()
        if enabled:
            description = (
                "You have agentic capabilities. Use the agent actions to propose or execute actions. "
                "Ensure to return ONLY valid JSON when asked to produce actions."
            )
        else:
            description = (
                "You would have agentic capabilities, but the agent system is currently disabled. "
                "To enable it, set AGENT_ENABLED to true or enable it via WebUI. "
                "When disabled, you can still use regular actions but cannot execute agentic commands."
            )
        return {"description": description}

    async def handle_incoming_message(self, bot, message, prompt):
        """Send a structured prompt to the active LLM (via plugin_instance) and parse JSON actions."""
        try:
            from core.plugin_instance import handle_incoming_message as llm_handle

            # Assume prompt is dict or string; normalize
            data = (
                prompt
                if isinstance(prompt, dict)
                else {"input": {"payload": {"description": str(prompt)}}}
            )

            res = await llm_handle(
                bot=None, message=None, context_memory_or_prompt=data
            )

            # Try to parse as JSON actions
            try:
                parsed = json.loads(res)
                actions = parsed.get("actions", []) if isinstance(parsed, dict) else []
                log_debug(f"[agent] LLM returned actions: {actions}")
                return actions
            except Exception:
                log_warning("[agent] LLM did not return parseable JSON actions")
                return res

        except Exception as e:
            log_error(f"[agent] handle_incoming_message error: {e}")
            raise

    async def _run_command(self, cmd: str, timeout: float = 30.0) -> str:
        """Execute a single shell command and return output (async)."""
        try:
            process = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return stdout.decode(errors="ignore").strip()
        except asyncio.TimeoutError:
            return "⚠️ Command timeout"
        except Exception as e:
            log_error(f"[agent] _run_command failed: {e}")
            return f"⚠️ Command failed: {e}"

    def _is_whitelisted(self, cmd: str) -> bool:
        # Simple heuristic: command startswith any whitelist entry
        for w in self._whitelist:
            if cmd.strip().startswith(w):
                return True
        return False

    # --- DB persistence helpers for audit logging ---
    async def _create_activity_log(
        self,
        command: str,
        proposer: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[int]:
        try:
            import json

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO agent_activity_log (command, proposer, status, trainer_id, request_ts, response_ts, result, metadata)
                        VALUES (%s, %s, %s, NULL, CURRENT_TIMESTAMP, NULL, NULL, %s)
                        """,
                        (
                            command,
                            proposer,
                            "proposed",
                            json.dumps(metadata) if metadata else None,
                        ),
                    )
                    await conn.commit()
                    return getattr(cur, "lastrowid", None)
        except Exception as e:
            log_error(f"[agent] _create_activity_log failed: {e}")
            return None

    async def _update_activity_log(
        self,
        activity_id: int,
        *,
        status: Optional[str] = None,
        trainer_id: Optional[str] = None,
        result: Optional[str] = None,
    ) -> None:
        if not activity_id:
            return
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    updates = []
                    params = []
                    if status is not None:
                        updates.append("status=%s")
                        params.append(status)
                    if trainer_id is not None:
                        updates.append("trainer_id=%s")
                        params.append(trainer_id)
                    if result is not None:
                        updates.append("result=%s")
                        params.append(result)
                        updates.append("response_ts=CURRENT_TIMESTAMP")
                    if not updates:
                        return
                    sql = (
                        "UPDATE agent_activity_log SET "
                        + ", ".join(updates)
                        + " WHERE id=%s"
                    )
                    params.append(activity_id)
                    await cur.execute(sql, tuple(params))
                    await conn.commit()
        except Exception as e:
            log_error(f"[agent] _update_activity_log failed: {e}")

    async def _insert_action_exec(
        self,
        activity_log_id: int,
        command: str,
        *,
        status: str = "pending",
        error_text: Optional[str] = None,
        result: Optional[dict] = None,
    ) -> Optional[int]:
        try:
            import json

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO agent_action_execs (activity_log_id, command, status, error_text, result)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            activity_log_id,
                            command,
                            status,
                            error_text,
                            json.dumps(result) if result is not None else None,
                        ),
                    )
                    await conn.commit()
                    return getattr(cur, "lastrowid", None)
        except Exception as e:
            log_error(f"[agent] _insert_action_exec failed: {e}")
            return None

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {})

        if action_type == "agent_execute":
            cmd = payload.get("command", "").strip()
            if not cmd:
                log_warning("[agent] No command provided")
                return "No command provided"

            # Check global enabled flag (live value from config registry)
            if not self.is_enabled():
                log_warning(
                    "[agent] Agent execution disabled by configuration or not running in container"
                )
                return "Agent execution is disabled by configuration or runtime environment"

            mode = self._approval_mode or "whitelist"

            if mode == "disabled":
                return "Agent shell execution disabled (policy)"

            # Helper to persist a direct execution result
            async def _execute_and_record(command, activity_id=None):
                try:
                    res = await self._run_command(command)
                except Exception as e:
                    res = f"⚠️ Execution failed: {e}"
                # Record action exec and update activity log
                try:
                    if activity_id is None:
                        activity_id = await self._create_activity_log(
                            command, proposer="system", metadata=None
                        )
                        # Mark it approved/executed immediately
                        if activity_id is not None:
                            await self._update_activity_log(
                                activity_id, status="executed"
                            )
                    if activity_id is not None:
                        await self._insert_action_exec(
                            activity_id,
                            command,
                            status="executed",
                            result={"output": res},
                        )
                except Exception as e:
                    log_warning(f"[agent] Failed to persist execution record: {e}")

                formatted_result = (
                    "Agent executed command:\n"
                    "```\n"
                    f"{command}\n"
                    "```\n"
                    "Output:\n"
                    "```\n"
                    f"{res}\n"
                    "```"
                )

                try:
                    self._notify_fn(formatted_result)
                except Exception:
                    pass

                interface_name = context.get("interface") or context.get(
                    "interface_name"
                )
                if interface_name:
                    if isinstance(original_message, dict):
                        chat_id = original_message.get("chat_id")
                        message_id = original_message.get("message_id")
                        interface_path = original_message.get("interface_path")
                    else:
                        chat_id = getattr(original_message, "chat_id", None)
                        message_id = getattr(original_message, "message_id", None)
                        interface_path = getattr(
                            original_message, "interface_path", None
                        )

                    try:
                        from core.auto_response import request_llm_delivery

                        await request_llm_delivery(
                            action_outputs=[
                                {
                                    "type": "agent_execute",
                                    "command": command,
                                    "output": str(res),
                                    "formatted_output": formatted_result,
                                }
                            ],
                            original_context={
                                "chat_id": chat_id,
                                "message_id": message_id,
                                "interface_name": interface_name,
                                "interface_path": interface_path,
                            },
                            action_type="agent_execute",
                        )
                    except Exception as e:
                        log_warning(
                            f"[agent] Failed to deliver execution output to interface: {e}"
                        )

                return res

            if mode == "always_approve":
                log_info(f"[agent] executing (always_approve): {cmd}")
                return await _execute_and_record(cmd)

            if mode == "whitelist":
                if self._is_whitelisted(cmd):
                    log_info(f"[agent] executing (whitelisted): {cmd}")
                    return await _execute_and_record(cmd)
                else:
                    log_warning(f"[agent] Command not in whitelist: {cmd}")
                    # Create a proposal and notify trainer
                    activity_id = await self._create_activity_log(
                        cmd, proposer="system"
                    )
                    try:
                        self._notify_fn(
                            f"Agent proposes command #{activity_id} (awaiting approval): {cmd}\n"
                            f"Reply with '/agent approve {activity_id}' to approve or '/agent reject {activity_id}' to reject."
                        )
                    except Exception:
                        pass
                    return (
                        f"Command not allowed without approval: proposal #{activity_id}"
                    )

            if mode == "always_ask":
                # Create a proposal and notify trainer
                activity_id = await self._create_activity_log(cmd, proposer="system")
                try:
                    self._notify_fn(
                        f"Agent proposes command #{activity_id} (awaiting approval): {cmd}\n"
                        f"Reply with '/agent approve {activity_id}' to approve or '/agent reject {activity_id}' to reject."
                    )
                except Exception:
                    pass
                return f"Command proposal sent for approval: proposal #{activity_id}"

            return "Unknown approval mode"

        if action_type == "agent_list_files":
            raw_path = str(payload.get("path") or ".")
            recursive = bool(payload.get("recursive", False))
            max_depth = _safe_int(payload.get("max_depth"), 2, min_value=0, max_value=8)
            limit = _safe_int(payload.get("limit"), 120, min_value=1, max_value=1000)

            safe_path, err = self._resolve_safe_path(raw_path)
            if err or safe_path is None:
                return {"status": "error", "reason": err or "invalid path"}

            items = self._list_files(
                safe_path,
                recursive=recursive,
                max_depth=max_depth,
                limit=limit,
            )
            return {
                "status": "ok",
                "path": str(safe_path),
                "count": len(items),
                "items": items,
            }

        if action_type == "agent_read_file":
            raw_path = str(payload.get("path") or "").strip()
            safe_path, err = self._resolve_safe_path(raw_path)
            if err or safe_path is None:
                return {"status": "error", "reason": err or "invalid path"}
            if not safe_path.exists():
                return {"status": "error", "reason": "file not found"}
            if safe_path.is_dir():
                return {"status": "error", "reason": "path is a directory"}

            start_line = _safe_int(
                payload.get("start_line"), 1, min_value=1, max_value=1_000_000
            )
            end_line = _safe_int(
                payload.get("end_line"),
                start_line + 199,
                min_value=start_line,
                max_value=1_000_000,
            )
            max_chars = _safe_int(
                payload.get("max_chars"), 40_000, min_value=500, max_value=200_000
            )

            try:
                with safe_path.open("r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
                slice_lines = lines[start_line - 1 : end_line]
                content = "".join(slice_lines)
                if len(content) > max_chars:
                    content = content[:max_chars] + "\n... (truncated)"
                return {
                    "status": "ok",
                    "path": str(safe_path),
                    "start_line": start_line,
                    "end_line": min(end_line, len(lines)),
                    "total_lines": len(lines),
                    "content": content,
                }
            except Exception as exc:
                return {"status": "error", "reason": f"read failed: {exc}"}

        # Placeholder: other action types may propose or approve
        if action_type == "propose_action":
            cmd = payload.get("command")
            proposer = payload.get("proposer") or "system"
            if not cmd:
                return {"status": "error", "reason": "no command provided"}
            activity_id = await self._create_activity_log(
                cmd, proposer=proposer, metadata={"origin": "propose_action"}
            )
            try:
                self._notify_fn(
                    f"Agent proposed action #{activity_id}: {cmd}\n"
                    f"Reply with '/agent approve {activity_id}' to approve or '/agent reject {activity_id}' to reject."
                )
            except Exception:
                pass
            return {"status": "proposed", "command": cmd, "proposal_id": activity_id}

        if action_type == "approve_action":
            # Approve previously proposed action identified by proposal_id OR accept direct command
            proposal_id = payload.get("proposal_id") or payload.get("proposal")
            cmd = payload.get("command")
            trainer_id = None
            # Try to extract trainer id from original_message if available
            try:
                if original_message and isinstance(original_message, dict):
                    # Accept multiple potential fields
                    trainer_id = (
                        original_message.get("sender_id")
                        or original_message.get("user_id")
                        or None
                    )
                    if trainer_id is not None:
                        trainer_id = str(trainer_id)
            except Exception:
                trainer_id = None

            if proposal_id and not cmd:
                # Lookup the proposal in DB to find the command
                try:
                    conn_ctx = await self._get_conn_ctx()
                    async with conn_ctx as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT command, status FROM agent_activity_log WHERE id=%s",
                                (int(proposal_id),),
                            )
                            row = await cur.fetchone()
                            if not row:
                                return {
                                    "status": "error",
                                    "reason": "proposal not found",
                                }
                            cmd = row[0]
                            current_status = row[1]
                            if current_status != "proposed":
                                return {
                                    "status": "error",
                                    "reason": f"proposal not in proposed state: {current_status}",
                                }
                except Exception as e:
                    log_error(f"[agent] approve_action lookup failed: {e}")
                    return {"status": "error", "reason": "db lookup failed"}

            if not cmd:
                return {"status": "error", "reason": "no command to approve"}

            # Mark as approved
            try:
                if proposal_id:
                    await self._update_activity_log(
                        int(proposal_id), status="approved", trainer_id=trainer_id
                    )
                else:
                    # Create a new activity row marked as approved
                    proposal_id = await self._create_activity_log(
                        cmd, proposer="trainer"
                    )
                    if proposal_id is not None:
                        await self._update_activity_log(
                            proposal_id, status="approved", trainer_id=trainer_id
                        )
            except Exception as e:
                log_warning(f"[agent] Failed to update proposal status: {e}")

            # Execute
            res = await self._run_command(cmd)

            # Persist execution details and mark executed
            try:
                if proposal_id is not None:
                    await self._insert_action_exec(
                        proposal_id, cmd, status="executed", result={"output": res}
                    )
                    await self._update_activity_log(
                        proposal_id, status="executed", result=res
                    )
            except Exception as e:
                log_warning(f"[agent] Failed to persist approval execution: {e}")

            return {"status": "executed", "proposal_id": proposal_id, "output": res}

        if action_type == "reject_action":
            # Reject a previously proposed action without executing it.
            proposal_id = payload.get("proposal_id") or payload.get("proposal")
            trainer_id = None
            try:
                if original_message and isinstance(original_message, dict):
                    trainer_id = (
                        original_message.get("sender_id")
                        or original_message.get("user_id")
                        or None
                    )
                    if trainer_id is not None:
                        trainer_id = str(trainer_id)
            except Exception:
                trainer_id = None

            if not proposal_id:
                return {"status": "error", "reason": "no proposal_id to reject"}

            # Verify the proposal exists and is still pending.
            try:
                conn_ctx = await self._get_conn_ctx()
                async with conn_ctx as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT status FROM agent_activity_log WHERE id=%s",
                            (int(proposal_id),),
                        )
                        row = await cur.fetchone()
                        if not row:
                            return {
                                "status": "error",
                                "reason": "proposal not found",
                            }
                        current_status = row[0]
                        if current_status != "proposed":
                            return {
                                "status": "error",
                                "reason": f"proposal not in proposed state: {current_status}",
                            }
            except Exception as e:
                log_error(f"[agent] reject_action lookup failed: {e}")
                return {"status": "error", "reason": "db lookup failed"}

            try:
                await self._update_activity_log(
                    int(proposal_id),
                    status="rejected",
                    trainer_id=trainer_id,
                    result="rejected",
                )
            except Exception as e:
                log_warning(f"[agent] Failed to update proposal status: {e}")
                return {"status": "error", "reason": "db update failed"}

            return {"status": "rejected", "proposal_id": proposal_id}

        if action_type == "start_task":
            from core.agent_core import get_agent_loop_manager

            engine_name = payload.get("engine") or "manual"
            input_payload = payload.get("input") or payload.get("input_payload") or {}
            max_iterations = payload.get("max_iterations")

            manager = get_agent_loop_manager()
            task_id = await manager.run_loop(
                engine=engine_name,
                input_payload=input_payload,
                context=context or {},
                max_iterations=max_iterations,
            )
            return {"status": "started", "task_id": task_id}

        if action_type == "spawn_drone":
            goal = str(payload.get("goal") or "").strip()
            if not goal:
                return {"status": "error", "reason": "no goal provided"}

            # Recursion guard: Drones cannot spawn Drones (single-level
            # delegation). The prompt filter already hides spawn_drone from a
            # Drone's tool list; this is the defensive backstop.
            ctx = context or {}
            drone_ctx = ctx.get("drone")
            if isinstance(drone_ctx, dict) and drone_ctx.get("is_drone"):
                log_warning("[agent] A Drone attempted to spawn another Drone; blocked")
                return {"ok": False, "error": "drones_cannot_spawn_drones"}

            engine = payload.get("engine") or None
            max_iterations = payload.get("max_iterations")
            parent_task_id = ctx.get("agent_task_id") or ctx.get("task_id")

            from core.agent_core import get_agent_loop_manager

            manager = get_agent_loop_manager()
            try:
                result = await manager.run_drone(
                    goal=goal,
                    engine=engine,
                    context=ctx,
                    parent_task_id=parent_task_id,
                    max_iterations=max_iterations,
                    original_message=original_message,
                )
            except Exception as exc:
                log_error(f"[agent] spawn_drone failed: {exc}")
                return {"ok": False, "error": f"drone_failed: {exc}"}

            return {
                "ok": True,
                "final_text": result.get("final_text", ""),
                "iterations": result.get("iterations"),
                "stop_reason": result.get("stop_reason"),
                "task_id": result.get("task_id"),
            }

        log_warning(f"[agent] Unknown action type: {action_type}")
        return None


PLUGIN_CLASS = AgentPlugin
