# plugins/agent_plugin.py

import asyncio
import json
import os
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


class AgentPlugin(AIPluginBase):
    display_name = "Agent Plugin"

    def __init__(self, notify_fn: Optional[callable] = None):
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

        log_info(
            f"[agent] Initialized. in_container={self._in_container} enabled={self._enabled} approval_mode={self._approval_mode}"
        )

    @staticmethod
    def get_supported_action_types() -> list[str]:
        return ["agent_execute", "propose_action", "approve_action", "start_task"]

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
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        return {
            "description": "Use the agent actions to propose or execute actions. Ensure to return ONLY valid JSON when asked to produce actions.",
        }

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
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
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
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
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
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
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

            # Check global enabled flag
            if not self._enabled:
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
                        await self._update_activity_log(activity_id, status="executed")
                    await self._insert_action_exec(
                        activity_id, command, status="executed", result={"output": res}
                    )
                except Exception as e:
                    log_warning(f"[agent] Failed to persist execution record: {e}")
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
                            f"Agent proposes command #{activity_id} (awaiting approval): {cmd}\nReply with '/agent approve {activity_id}' to approve."
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
                        f"Agent proposes command #{activity_id} (awaiting approval): {cmd}\nReply with '/agent approve {activity_id}' to approve."
                    )
                except Exception:
                    pass
                return f"Command proposal sent for approval: proposal #{activity_id}"

            return "Unknown approval mode"

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
                    f"Agent proposed action #{activity_id}: {cmd}\nReply with '/agent approve {activity_id}' to approve."
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
                    from core.db import get_conn_ctx

                    async with get_conn_ctx() as conn:
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
                    await self._update_activity_log(
                        proposal_id, status="approved", trainer_id=trainer_id
                    )
            except Exception as e:
                log_warning(f"[agent] Failed to update proposal status: {e}")

            # Execute
            res = await self._run_command(cmd)

            # Persist execution details and mark executed
            try:
                await self._insert_action_exec(
                    proposal_id, cmd, status="executed", result={"output": res}
                )
                await self._update_activity_log(
                    proposal_id, status="executed", result=res
                )
            except Exception as e:
                log_warning(f"[agent] Failed to persist approval execution: {e}")

            return {"status": "executed", "proposal_id": proposal_id, "output": res}

        log_warning(f"[agent] Unknown action type: {action_type}")
        return None


PLUGIN_CLASS = AgentPlugin
