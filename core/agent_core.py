"""core/agent_core.py

Agent loop manager and persistence helpers.
This file provides a lightweight orchestrator used by the Agent plugin to
run multi-iteration tasks, persist them to the DB for WebUI inspection, and
expose pause/resume/cancel control.

Note: this is a conservative, test-friendly scaffold. Concrete LLM/engine
invocation and action parsing are left as TODOs and should call existing
core utilities (plugin_instance, action_parser, etc.).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry

# Expose agent configuration variable for max iterations
try:
    from core.variables_engine import register_exposed_var
    register_exposed_var(
        "AGENT_MAX_ITERATIONS",
        label="Agent max iterations",
        default=5,
        value_type=int,
        ui_type="number",
        description="Maximum iterations allowed for Agent loops before automatic stop.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
except Exception:
    pass

# DB helper is imported lazily inside methods for testability/mocking


class AgentLoopManager:
    """Manage agent tasks: create DB record, run iterations in background,
    and update task status/iteration metadata.

    This implementation is intentionally minimal: it focuses on orchestration,
    persistence hooks and pause/resume semantics. Real LLM calls and action
    dispatching must be integrated in run_loop() where TODO markers are.
    """

    def __init__(self) -> None:
        self._paused_tasks: Dict[int, asyncio.Event] = {}
        self._running_tasks: Dict[int, asyncio.Task] = {}

    # --- DB helpers ---
    async def _maybe_commit(self, conn) -> None:
        """Safely call commit on connection (if available) and await if coroutine."""
        try:
            commit_fn = getattr(conn, 'commit', None)
            if commit_fn and callable(commit_fn):
                res = commit_fn()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            # Best-effort; ignore commit failures
            pass

    async def _create_agent_task(self, engine: str, input_payload: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> Optional[int]:
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO agent_tasks (engine, status, input, iterations_meta, output, trainer_id, metadata)
                        VALUES (%s, %s, %s, %s, %s, NULL, %s)
                        """,
                        (
                            engine,
                            'pending',
                            json.dumps(input_payload),
                            json.dumps([]),
                            None,
                            json.dumps(metadata) if metadata else None,
                        ),
                    )
                    await self._maybe_commit(conn)
                    return getattr(cur, "lastrowid", None)
        except Exception as e:
            log_error(f"[agent_core] _create_agent_task DB error: {e}")
            return None

    async def _update_agent_task_status(self, task_id: int, status: str) -> None:
        if not task_id:
            return
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE agent_tasks SET status=%s WHERE id=%s", (status, int(task_id)))
                    await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _update_agent_task_status failed: {e}")

    async def _append_iteration_meta(self, task_id: int, iteration_meta: Dict[str, Any]) -> None:
        if not task_id:
            return
        try:
            import json
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Fetch existing iterations_meta
                    await cur.execute("SELECT iterations_meta FROM agent_tasks WHERE id=%s", (int(task_id),))
                    row = await cur.fetchone()
                    if row:
                        existing = row[0] or '[]'
                    else:
                        existing = '[]'
                    try:
                        arr = json.loads(existing)
                    except Exception:
                        arr = []
                    arr.append(iteration_meta)
                    await cur.execute("UPDATE agent_tasks SET iterations_meta=%s WHERE id=%s", (json.dumps(arr), int(task_id)))
                    await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _append_iteration_meta failed: {e}")

    async def _finalize_task(self, task_id: int, status: str, output: Optional[Dict[str, Any]] = None) -> None:
        try:
            import json
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("UPDATE agent_tasks SET status=%s, output=%s WHERE id=%s", (status, json.dumps(output) if output else None, int(task_id)))
                    await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _finalize_task failed: {e}")

    # --- Control APIs ---
    def pause_task(self, task_id: int) -> None:
        ev = self._paused_tasks.get(task_id)
        if ev is None:
            ev = asyncio.Event()
            ev.clear()
            self._paused_tasks[task_id] = ev
        else:
            ev.clear()
        log_info(f"[agent_core] Task {task_id} paused")

    def resume_task(self, task_id: int) -> None:
        ev = self._paused_tasks.get(task_id)
        if ev is not None:
            ev.set()
            log_info(f"[agent_core] Task {task_id} resumed")

    def cancel_task(self, task_id: int) -> None:
        t = self._running_tasks.get(task_id)
        if t:
            t.cancel()
            log_info(f"[agent_core] Task {task_id} cancelled")

    # --- Agent loop orchestration (scaffold) ---
    async def run_loop(self, *, engine: str, input_payload: Dict[str, Any], context: Dict[str, Any] | None = None, max_iterations: int | None = None) -> Optional[int]:
        """Run an agent task loop as a background-friendly coroutine.

        Returns the agent task id (db) if created, else None.
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("AGENT_MAX_ITERATIONS", 5))

        task_id = await self._create_agent_task(engine, input_payload, metadata=context or {})
        if not task_id:
            log_error("[agent_core] Failed to create agent task in DB")
            return None

        # Launch the loop in background and store the task
        loop_task = asyncio.create_task(self._run_loop_background(task_id, engine, input_payload, context or {}, max_iterations))
        self._running_tasks[task_id] = loop_task
        return task_id

    async def _run_loop_background(self, task_id: int, engine: str, input_payload: Dict[str, Any], context: Dict[str, Any], max_iterations: int) -> None:
        await self._update_agent_task_status(task_id, "running")
        output = None
        try:
            for i in range(1, max_iterations + 1):
                # Pause support
                ev = self._paused_tasks.get(task_id)
                if ev is not None and not ev.is_set():
                    await self._update_agent_task_status(task_id, "paused")
                    await ev.wait()
                    await self._update_agent_task_status(task_id, "running")

                iteration_meta: Dict[str, Any] = {"iteration": i, "status": "started"}
                await self._append_iteration_meta(task_id, iteration_meta)

                # Integrate with LLM/engine hooks: build a small agent iteration prompt
                try:
                    # Lazy imports to avoid circular dependencies at module load
                    from core import plugin_instance
                    from core.transport_layer import extract_json_from_text
                    from core.action_parser import run_actions
                    from types import SimpleNamespace
                    from core.notifier import notify_intelligent
                    from core.db import get_conn_ctx
                    import time
                except Exception as e:
                    log_error(f"[agent_core] Failed to import runtime helpers: {e}")
                    result = {"ok": False, "error": "internal import failure"}
                    iteration_meta = {"iteration": i, "status": "completed", "result": result}
                    await self._append_iteration_meta(task_id, iteration_meta)
                    break

                # Build a structured prompt for the agent iteration
                prompt = {
                    "input": {
                        "payload": {
                            "text": input_payload if isinstance(input_payload, str) else input_payload,
                            "iteration": i,
                            "task_id": task_id,
                        }
                    },
                    "system_message": {"type": "agent_iteration", "task_id": task_id, "iteration": i, "engine": engine},
                }

                log_debug(f"[agent_core] Running iteration {i} for task {task_id} using engine={engine}")

                # Gather Recon contributions (preflight) and attach to the prompt
                try:
                    from core.recon import gather_recon_contributions
                    recon = await gather_recon_contributions(message=None, context_memory=None, text=None, tags=None, keywords=None)
                    if recon:
                        prompt['recon'] = recon
                except Exception as e:
                    log_debug(f"[agent_core] Recon gather failed: {e}")

                try:
                    # Ask the active engine (via plugin_instance) to produce JSON actions or a response
                    raw_response = await plugin_instance.handle_incoming_message(bot=None, message=None, context_memory_or_prompt=prompt)
                    raw_text = raw_response if isinstance(raw_response, str) else (str(raw_response) if raw_response is not None else "")
                except Exception as e:
                    raw_text = f"⚠️ LLM invocation failed: {e}"

                # Try to extract JSON from the LLM output
                parsed_json, meta = extract_json_from_text(raw_text, return_metadata=True)

                if not parsed_json:
                    # Record raw text when no JSON actions found
                    result = {"ok": False, "raw_text": raw_text, "metadata": meta}
                    iteration_meta = {"iteration": i, "status": "completed", "result": result}
                    await self._append_iteration_meta(task_id, iteration_meta)
                    # If LLM returned nothing actionable, continue to next iteration
                    continue

                # Normalize to a list of actions
                actions = None
                if isinstance(parsed_json, dict) and parsed_json.get("actions"):
                    actions = parsed_json.get("actions")
                elif isinstance(parsed_json, list):
                    actions = parsed_json
                elif isinstance(parsed_json, dict) and parsed_json.get("type"):
                    actions = [parsed_json]
                else:
                    actions = []

                # Create a synthetic original_message to mark origin from LLM
                orig_msg = SimpleNamespace(from_llm=True, chat_id=f"agent_task_{task_id}", message_id=int(time.time()*1000) % 1_000_000, text=raw_text, interface_path="agent")

                # Run actions via action parser
                try:
                    context = {"from_llm": True, "task_id": task_id, "iteration": i}
                    run_result = await run_actions(actions, context, None, orig_msg)
                except Exception as e:
                    run_result = {"processed": [], "errors": [str(e)], "failed_actions": []}

                iteration_meta = {"iteration": i, "status": "completed", "llm_raw": raw_text, "actions_result": run_result}
                await self._append_iteration_meta(task_id, iteration_meta)

                # If any plugin created a proposal (agent_activity_log.status='proposed') recently, pause and wait for approval
                try:
                    async with get_conn_ctx() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("SELECT id, command, request_ts FROM agent_activity_log WHERE status=%s ORDER BY request_ts DESC LIMIT 1", ("proposed",))
                            row = await cur.fetchone()
                            if row:
                                prop_id, prop_cmd, prop_ts = row[0], row[1], row[2]
                                # best-effort: if proposal is recent (last 60s) then consider it part of this task
                                import datetime
                                if prop_ts and isinstance(prop_ts, datetime.datetime):
                                    age = (datetime.datetime.utcnow() - prop_ts).total_seconds()
                                else:
                                    age = 0
                                if age < 120:
                                    await self._update_agent_task_status(task_id, "waiting_for_approval")
                                    self.pause_task(task_id)
                                    notify_intelligent(f"Agent task #{task_id} paused: awaiting approval for proposal #{prop_id}: {prop_cmd}")
                                    # Don't continue iterations until approval/resume
                                    break
                except Exception:
                    # Non-fatal - proceed
                    pass

                # Honor explicit stop condition in returned JSON meta
                if isinstance(parsed_json, dict) and parsed_json.get("meta") and isinstance(parsed_json.get("meta"), dict):
                    if parsed_json.get("meta", {}).get("agent_continue") is False:
                        break

            output = {"final": "done", "iterations": max_iterations}
            await self._finalize_task(task_id, "completed", output)

            # Run Debrief hooks with processed iterations
            try:
                from core.debrief import run_debrief
                # Load iterations_meta for context
                await run_debrief(processed_actions=[], failed_actions=[], results=output, context={'task_id': task_id}, original_message=None)
            except Exception as e:
                log_debug(f"[agent_core] Debrief failed: {e}")
        except asyncio.CancelledError:
            await self._finalize_task(task_id, "cancelled", output)
            log_warning(f"[agent_core] Task {task_id} cancelled")
        except Exception as e:
            await self._finalize_task(task_id, "failed", output)
            log_error(f"[agent_core] _run_loop_background failed: {e}")
        finally:
            # cleanup
            self._running_tasks.pop(task_id, None)
            self._paused_tasks.pop(task_id, None)
            log_info(f"[agent_core] Task {task_id} finished with status in DB")


class AgentCore:
    """Compatibility shim used by tests and plugins.

    Provides attach/detach to the active engine and a small action executor
    used by the Agent plugin's exposed actions (propose/approve/execute).
    """

    def __init__(self) -> None:
        self._enabled: bool = False
        self._engine = None
        # overridable hooks (tests patch these)
        self._notify_fn = None
        self._create_activity_log = None
        self._update_activity_log = None
        self._insert_action_exec = None
        self._run_command = None

    async def attach_to_active_engine(self) -> None:
        try:
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry
            name = await get_active_cortex_engine()
            reg = get_cortex_registry()
            engine = None
            if hasattr(reg, 'get_engine'):
                engine = reg.get_engine(name)
            elif hasattr(reg, 'load_engine'):
                engine = reg.load_engine(name)
            # Attach if the engine exposes an attach_agent() hook (do not require supports_agent)
            if engine and hasattr(engine, 'attach_agent') and callable(engine.attach_agent):
                try:
                    engine.attach_agent(self)
                    self._engine = engine
                except Exception as e:
                    log_warning(f"[agent_core] attach_to_active_engine attach failed: {e}")
        except Exception as e:
            log_warning(f"[agent_core] attach_to_active_engine failed: {e}")

    async def detach_from_engine(self) -> None:
        try:
            if self._engine and hasattr(self._engine, 'detach_agent') and callable(self._engine.detach_agent):
                try:
                    self._engine.detach_agent(self)
                finally:
                    self._engine = None
        except Exception as e:
            log_warning(f"[agent_core] detach_from_engine failed: {e}")

    async def execute_action(self, action: Dict[str, Any], context: Dict[str, Any], interface, message) -> Dict[str, Any]:
        typ = action.get('type')
        payload = action.get('payload') or {}

        if typ == 'propose_action':
            cmd = payload.get('command') or payload.get('cmd') or ''
            proposer = payload.get('proposer')
            # create activity log
            try:
                if callable(self._create_activity_log):
                    aid = await self._create_activity_log(cmd, proposer=proposer, metadata=payload.get('metadata'))
                else:
                    aid = None
            except Exception as e:
                aid = None
                log_warning(f"[agent_core] propose_action create_activity_log failed: {e}")

            # notify trainer / intelligent
            try:
                if callable(self._notify_fn):
                    self._notify_fn(f"Agent proposal created: {cmd}")
                else:
                    try:
                        from core.notifier import notify_intelligent
                        notify_intelligent(f"Agent proposal created: {cmd}")
                    except Exception:
                        pass
            except Exception:
                pass

            return {'status': 'proposed', 'proposal_id': aid}

        elif typ == 'approve_action':
            proposal_id = payload.get('proposal_id')
            command = payload.get('command') or ''
            approver = (message or {}).get('sender_id') if isinstance(message, dict) else None

            # mark approved
            try:
                if callable(self._update_activity_log):
                    await self._update_activity_log(proposal_id, status='approved', approver=approver)
            except Exception as e:
                log_warning(f"[agent_core] approve_action update_activity_log failed: {e}")

            # execute command
            output = None
            try:
                if callable(self._run_command):
                    output = await self._run_command(command)
                else:
                    import asyncio as _asyncio
                    proc = await _asyncio.create_subprocess_shell(command, stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.STDOUT)
                    out, _ = await proc.communicate()
                    output = (out.decode('utf-8', errors='replace') if out else '')
            except Exception as e:
                output = f"Error: {e}"

            # record exec
            try:
                if callable(self._insert_action_exec):
                    await self._insert_action_exec(proposal_id, command, output=output, executor=approver)
            except Exception as e:
                log_warning(f"[agent_core] approve_action insert_action_exec failed: {e}")

            # finalize
            try:
                if callable(self._update_activity_log):
                    await self._update_activity_log(proposal_id, status='executed', output=output)
            except Exception:
                pass

            return {'status': 'executed', 'proposal_id': proposal_id, 'output': output}

        else:
            return {'status': 'unknown_action'}


# Expose a convenient singleton manager
_agent_loop_manager = AgentLoopManager()


def get_agent_loop_manager() -> AgentLoopManager:
    return _agent_loop_manager
