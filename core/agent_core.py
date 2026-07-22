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
import importlib
import json
import sys
from typing import Any, Dict, Optional

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry
from core.agent_router import _is_pure_message

# Expose agent configuration variable for max iterations
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "AGENT_MAX_ITERATIONS",
        label="Agent max iterations",
        default=30,
        value_type=int,
        ui_type="number",
        description="Maximum iterations allowed for Agent loops before automatic stop.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Drones are ephemeral, task-scoped sub-agents spawned by the Agent via the
    # `spawn_drone` tool. They run through the same bounded agent loop but with a
    # tighter budget and cannot spawn further Drones (single-level delegation).
    register_exposed_var(
        "DRONE_MAX_ITERATIONS",
        label="Drone max iterations",
        default=3,
        value_type=int,
        ui_type="number",
        description="Maximum iterations allowed for a Drone (sub-agent) loop before automatic stop.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "DRONE_TURN_TIMEOUT_SEC",
        label="Drone turn timeout (seconds)",
        default=90,
        value_type=int,
        ui_type="number",
        description="Wall-clock budget in seconds for a single Drone (sub-agent) turn.",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    # Explicit-completion contract: when enabled (default), the agentic loop does
    # NOT treat a bare natural-language response (no tool calls) as "task done".
    # The model must either send a real user message or call the dedicated
    # ``attempt_completion`` tool to end the turn; otherwise the loop re-injects
    # a nudge and keeps working until the goal is finished or iterations run out.
    # This prevents premature final answers (the model announcing intent — e.g.
    # "I'll check the codebase now..." — and stopping without doing the work).
    register_exposed_var(
        "AGENT_REQUIRE_EXPLICIT_COMPLETION",
        label="Agent requires explicit completion",
        default=True,
        value_type=bool,
        ui_type="toggle",
        description=(
            "When on, an agentic turn ends only via a user message or the "
            "attempt_completion tool — plain text with no tool calls does not "
            "stop the loop, preventing premature final answers."
        ),
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
except Exception:
    pass

# Sentinel tool the model calls to explicitly declare the task finished. It is
# not a real executable action — the loop intercepts it, extracts its summary as
# the final answer, and stops. Recognised in the prompt's AVAILABLE TOOLS block.
_COMPLETION_TOOL = "attempt_completion"

# DB helper is imported lazily inside methods for testability/mocking


class AgentLoopManager:
    """Manage bounded agentic turns (Agentic Runtime 2.0).

    Orchestrates ``run_agentic_turn`` and ``run_drone``: assembling the tool
    manifest, running the bounded reasoning loop, and persisting each turn to
    the ``agent_tasks`` table via ``_persist_agentic_turn``.
    """

    def __init__(self) -> None:
        pass

    # --- DB helpers ---
    async def _maybe_commit(self, conn) -> None:
        """Safely call commit on connection (if available) and await if coroutine."""
        try:
            commit_fn = getattr(conn, "commit", None)
            if commit_fn and callable(commit_fn):
                res = commit_fn()
                if asyncio.iscoroutine(res):
                    await res
        except Exception:
            # Best-effort; ignore commit failures
            pass

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

    async def _begin_agentic_turn(
        self,
        *,
        engine: str | None,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        original_message: Any = None,
        preplanned_calls: Optional[list[Dict[str, Any]]] = None,
    ) -> Optional[int]:
        """Insert a ``running`` row for an in-flight agentic turn.

        Called BEFORE the reasoning loop starts so the turn is durable from the
        moment it begins. This is what makes a detached turn survivable across a
        container restart: an interrupted turn is left as a ``running`` row in
        ``agent_tasks`` that the startup recovery sweep can detect and mark as
        interrupted (rather than the turn silently vanishing with the process).

        Returns the new row id, or ``None`` on any DB failure (best-effort: a
        persistence problem must never prevent the turn from running).
        """
        try:
            trainer_id: str | None = None
            if isinstance(original_message, dict):
                raw_id = original_message.get("sender_id") or original_message.get(
                    "user_id"
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)
            elif original_message is not None:
                raw_id = getattr(original_message, "sender_id", None) or getattr(
                    original_message, "user_id", None
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)

            source = "agentic_turn"
            interface_path = None
            if isinstance(context, dict):
                source = str(
                    context.get("interface_name") or context.get("interface") or source
                )
                interface_path = context.get("interface_path")

            task_name: str | None = None
            if isinstance(context, dict):
                raw_title = context.get("agent_task_title")
                if isinstance(raw_title, str) and raw_title.strip():
                    task_name = raw_title.strip()[:120]
            if not task_name and isinstance(goal, str) and goal.strip():
                task_name = goal.strip()[:120]

            input_payload = {
                "goal": goal,
                "planned_actions": preplanned_calls
                if isinstance(preplanned_calls, list)
                else None,
            }
            metadata = {
                "source": source,
                "interface_path": interface_path,
                "has_preplanned_calls": bool(isinstance(preplanned_calls, list)),
                "name": task_name,
            }
            if isinstance(context, dict) and isinstance(context.get("drone"), dict):
                drone_meta = context["drone"]
                if drone_meta.get("is_drone"):
                    metadata["source"] = "drone"
                    metadata["drone"] = {
                        "parent_task_id": drone_meta.get("parent_task_id"),
                    }

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    params = (
                        str(engine or "default"),
                        "running",
                        json.dumps(input_payload),
                        trainer_id,
                        json.dumps(metadata),
                    )
                    new_id: Optional[int] = None
                    try:
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            params,
                        )
                        row = await cur.fetchone()
                        if row is not None:
                            new_id = int(row[0])
                    except Exception:
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            params,
                        )
                        last = getattr(cur, "lastrowid", None)
                        new_id = int(last) if last else None
                    await self._maybe_commit(conn)
                    return new_id
        except Exception as e:
            log_warning(f"[agent_core] _begin_agentic_turn failed: {e}")
            return None

    async def find_resumable_task_for_interface(
        self, interface_path: str | None
    ) -> Optional[Dict[str, Any]]:
        """Find a paused (``pending``) agentic task for the given interface.

        A single interface (a Telegram chat, a Discord channel, a WebUI
        session) must never own two parallel pending agentic tasks. When a
        turn exhausts its budget without an explicit completion it is parked as
        ``pending``; the very next message from that same ``interface_path``
        should RESUME that task, not spawn a brand-new one. This is what lets a
        user reply "yes, keep going" (in any language) in chat and have Synth
        continue the same task — without any keyword/language detection, purely
        by matching the originating interface.

        Returns a dict ``{"task_id", "goal", "engine", "prior_observations"}``
        for the most recent pending task on that interface, or ``None`` when
        there is none (or on any DB error — best-effort, never blocks a turn).
        """
        if not interface_path or not isinstance(interface_path, str):
            return None
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, engine, input, iterations_meta "
                        "FROM agent_tasks "
                        "WHERE status='pending' "
                        "AND metadata::json->>'interface_path' = %s "
                        "ORDER BY id DESC LIMIT 1",
                        (interface_path,),
                    )
                    row = await cur.fetchone()
            if not row:
                return None
            task_id = int(row[0])
            engine = row[1]
            input_raw = row[2]
            iterations_raw = row[3]

            input_payload = json.loads(input_raw) if input_raw else {}
            if not isinstance(input_payload, dict):
                input_payload = {}
            goal = str(input_payload.get("goal") or "").strip()
            if not goal:
                return None

            prior_observations: list[Dict[str, Any]] = []
            iterations_meta = json.loads(iterations_raw) if iterations_raw else []
            if isinstance(iterations_meta, list):
                for entry in iterations_meta:
                    if not isinstance(entry, dict):
                        continue
                    prior_observations.append(
                        {
                            "iteration": entry.get("iteration"),
                            "role": entry.get("role") or "observation",
                            "content": entry.get("result"),
                        }
                    )
            return {
                "task_id": task_id,
                "goal": goal,
                "engine": engine if engine and engine != "default" else None,
                "prior_observations": prior_observations,
            }
        except Exception as e:
            log_warning(f"[agent_core] find_resumable_task_for_interface failed: {e}")
            return None

    async def find_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Load a specific paused (``pending``) agentic task by its id.

        Unlike :meth:`find_resumable_task_for_interface` this does NOT match on
        the originating interface: the user may refer to a task created on a
        different interface (e.g. a Grillo-originated task referenced from a
        Telegram chat). The task id is chosen by the model — this method only
        loads and validates it, it never parses user text.

        Returns a dict ``{"task_id", "goal", "engine", "prior_observations"}``
        when the task exists and is ``pending``; otherwise ``None`` (unknown id,
        wrong status, or any DB error — best-effort, never blocks a turn).
        """
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            return None
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, engine, input, iterations_meta, status "
                        "FROM agent_tasks WHERE id=%s LIMIT 1",
                        (tid,),
                    )
                    row = await cur.fetchone()
            if not row:
                return None
            status = row[4]
            if status != "pending":
                log_info(
                    f"[agent_core] find_task_by_id: task {tid} is '{status}', "
                    "not resumable"
                )
                return None
            engine = row[1]
            input_raw = row[2]
            iterations_raw = row[3]

            input_payload = json.loads(input_raw) if input_raw else {}
            if not isinstance(input_payload, dict):
                input_payload = {}
            goal = str(input_payload.get("goal") or "").strip()
            if not goal:
                return None

            prior_observations: list[Dict[str, Any]] = []
            iterations_meta = json.loads(iterations_raw) if iterations_raw else []
            if isinstance(iterations_meta, list):
                for entry in iterations_meta:
                    if not isinstance(entry, dict):
                        continue
                    prior_observations.append(
                        {
                            "iteration": entry.get("iteration"),
                            "role": entry.get("role") or "observation",
                            "content": entry.get("result"),
                        }
                    )
            return {
                "task_id": int(row[0]),
                "goal": goal,
                "engine": engine if engine and engine != "default" else None,
                "prior_observations": prior_observations,
            }
        except Exception as e:
            log_warning(f"[agent_core] find_task_by_id failed: {e}")
            return None

    async def list_recent_tasks(self, limit: int = 15) -> list[Dict[str, Any]]:
        """Return the most recent agent tasks for display (newest first).

        Each entry is ``{"task_id", "status", "engine", "goal", "resumable"}``.
        ``resumable`` mirrors :meth:`find_task_by_id` semantics — only
        ``pending`` tasks can be resumed. Best-effort: returns ``[]`` on any DB
        error so a display command never raises.
        """
        try:
            lim = int(limit)
        except (TypeError, ValueError):
            lim = 15
        if lim <= 0:
            lim = 15
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, status, engine, input "
                        "FROM agent_tasks ORDER BY id DESC LIMIT %s",
                        (lim,),
                    )
                    rows = await cur.fetchall()
        except Exception as e:
            log_warning(f"[agent_core] list_recent_tasks failed: {e}")
            return []

        tasks: list[Dict[str, Any]] = []
        for row in rows or []:
            try:
                input_payload = json.loads(row[3]) if row[3] else {}
            except Exception:
                input_payload = {}
            goal = ""
            if isinstance(input_payload, dict):
                goal = str(input_payload.get("goal") or "").strip()
            status = row[1]
            tasks.append(
                {
                    "task_id": int(row[0]),
                    "status": status,
                    "engine": row[2],
                    "goal": goal,
                    "resumable": status == "pending",
                }
            )
        return tasks

    async def _mark_task_running(self, task_id: int) -> None:
        """Flip an existing ``agent_tasks`` row back to ``running``.

        Used when resuming a paused task so the UI/state reflects the in-flight
        resume immediately. Best-effort: a failure here must never abort the
        turn (the finalising persist will set the real terminal status anyway).
        """
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status='running', "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                        (int(task_id),),
                    )
                await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _mark_task_running failed: {e}")

    async def _persist_agentic_turn(
        self,
        *,
        engine: str | None,
        goal: str,
        result: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        original_message: Any = None,
        preplanned_calls: Optional[list[Dict[str, Any]]] = None,
        task_id: Optional[int] = None,
    ) -> Optional[int]:
        """Persist a completed agentic turn into ``agent_tasks``.

        This is the single, source-agnostic persistence point for
        :meth:`run_agentic_turn`. Every caller — the message-chain Agent Lane
        (Telegram / Discord / API), the WebUI ``/api/agent/run`` route, or any
        future entry point — produces a row visible in the WebUI Agent panel,
        regardless of which interface originated the turn.

        Best-effort: DB failures are logged and swallowed so an audit-write
        problem never breaks the agent turn itself.
        """
        try:
            observations = result.get("observations") or []
            iterations_meta: list[Dict[str, Any]] = []
            if isinstance(observations, list):
                for idx, obs in enumerate(observations, start=1):
                    if not isinstance(obs, dict):
                        continue
                    iterations_meta.append(
                        {
                            "iteration": obs.get("iteration") or idx,
                            "role": obs.get("role") or "observation",
                            "result": obs.get("content"),
                        }
                    )

            stop_reason = str(result.get("stop_reason") or "")
            if stop_reason in {"timeout", "engine_error", "empty_response"}:
                status = "failed"
            elif stop_reason == "paused_max_iterations":
                # Iteration budget exhausted without an explicit completion: the
                # goal is not finished. Park the task as ``pending`` so the user
                # can grant more iterations via "Continue" (WebUI) instead of it
                # being falsely reported as ``completed``.
                status = "pending"
            else:
                status = "completed"

            # Count the tool actions actually executed across the turn so the
            # proactive "I've done X actions, continue?" message and the WebUI
            # can show a concrete number.
            actions_executed = 0
            if isinstance(observations, list):
                for obs in observations:
                    if isinstance(obs, dict) and obs.get("role") == "tool_results":
                        content = obs.get("content")
                        if isinstance(content, list):
                            actions_executed += len(content)

            # Derive a trainer/originator id and a source label from the
            # originating context/message so the WebUI can attribute the task.
            trainer_id: str | None = None
            if isinstance(original_message, dict):
                raw_id = original_message.get("sender_id") or original_message.get(
                    "user_id"
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)
            elif original_message is not None:
                raw_id = getattr(original_message, "sender_id", None) or getattr(
                    original_message, "user_id", None
                )
                if raw_id is not None:
                    trainer_id = str(raw_id)

            source = "agentic_turn"
            interface_path = None
            if isinstance(context, dict):
                source = str(
                    context.get("interface_name") or context.get("interface") or source
                )
                interface_path = context.get("interface_path")

            input_payload = {
                "goal": goal,
                "planned_actions": preplanned_calls
                if isinstance(preplanned_calls, list)
                else None,
            }
            output_payload = {
                "iterations": int(result.get("iterations") or len(iterations_meta)),
                "final_text": result.get("final_text") or "",
                "stop_reason": stop_reason,
                "actions_executed": actions_executed,
                "paused": status == "pending",
            }
            # Task name shown in the WebUI Agents panel. Prefer the recon-derived
            # title (set on the shared context by the agent-intent recon hook);
            # fall back to a truncated goal so the task is never nameless.
            task_name: str | None = None
            if isinstance(context, dict):
                raw_title = context.get("agent_task_title")
                if isinstance(raw_title, str) and raw_title.strip():
                    task_name = raw_title.strip()[:120]
            if not task_name and isinstance(goal, str) and goal.strip():
                task_name = goal.strip()[:120]

            metadata = {
                "source": source,
                "interface_path": interface_path,
                "has_preplanned_calls": bool(isinstance(preplanned_calls, list)),
                "name": task_name,
            }
            # Tag Drone (sub-agent) turns so the WebUI/audit can distinguish them
            # from top-level Agent turns and link them to their parent task.
            if isinstance(context, dict) and isinstance(context.get("drone"), dict):
                drone_meta = context["drone"]
                if drone_meta.get("is_drone"):
                    metadata["source"] = "drone"
                    metadata["drone"] = {
                        "parent_task_id": drone_meta.get("parent_task_id"),
                    }

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    # When ``task_id`` is provided, a ``running`` row already
                    # exists (opened by ``_begin_agentic_turn`` so the turn is
                    # durable from the start). Finalise it in place with the
                    # loop results instead of inserting a duplicate row.
                    if task_id is not None:
                        await cur.execute(
                            """
                            UPDATE agent_tasks
                            SET engine = %s,
                                status = %s,
                                iterations_meta = %s,
                                output = %s,
                                trainer_id = %s,
                                metadata = %s
                            WHERE id = %s
                            """,
                            (
                                str(engine or "default"),
                                status,
                                json.dumps(iterations_meta),
                                json.dumps(output_payload),
                                trainer_id,
                                json.dumps(metadata),
                                int(task_id),
                            ),
                        )
                        await self._maybe_commit(conn)
                        return int(task_id)

                    params = (
                        str(engine or "default"),
                        status,
                        json.dumps(input_payload),
                        json.dumps(iterations_meta),
                        json.dumps(output_payload),
                        trainer_id,
                        json.dumps(metadata),
                    )
                    new_id: Optional[int] = None
                    try:
                        # Postgres path: RETURNING id yields the new row id.
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, iterations_meta, output, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                            """,
                            params,
                        )
                        row = await cur.fetchone()
                        if row is not None:
                            new_id = int(row[0])
                    except Exception:
                        # MariaDB / drivers without RETURNING support.
                        await cur.execute(
                            """
                            INSERT INTO agent_tasks (engine, status, input, iterations_meta, output, trainer_id, metadata)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            params,
                        )
                        last = getattr(cur, "lastrowid", None)
                        new_id = int(last) if last else None
                    await self._maybe_commit(conn)
                    return new_id
        except Exception as e:
            log_warning(f"[agent_core] _persist_agentic_turn failed: {e}")
            return None

    async def run_agentic_turn(
        self,
        *,
        goal: str,
        engine: str | None = None,
        context: Dict[str, Any] | None = None,
        max_iterations: int | None = None,
        timeout_seconds: float | None = None,
        original_message: Any = None,
        preplanned_calls: list[Dict[str, Any]] | None = None,
        task_id: int | None = None,
        prior_observations: list[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        """Run a bounded agentic turn that re-injects tool results into the model.

        Each iteration:

        1. Asks the active engine for a response, including the accumulated
           observation history from previous tool calls.
        2. Parses any tool calls out of the response (via the standard
           message-chain normalization).
        3. Executes them through :class:`core.agent_tool_executor.AgentToolExecutor`
           — which funnels internal actions AND external MCP tools through the
           same safety/audit gate.
        4. Appends the tool results as observations and loops, until the model
           emits no more tool calls, hits ``max_iterations``, or ``timeout``.

        Args:
            goal: The user/agent objective for this turn.
            engine: Optional cortex engine name (defaults to active cortex).
            context: Optional extra context dict forwarded to the engine.
            max_iterations: Hard cap on iterations (defaults to AGENT_MAX_ITERATIONS).
            timeout_seconds: Optional wall-clock budget for the whole turn.
            original_message: Optional originating message (for audit/safety).
            task_id: Optional existing ``agent_tasks`` row id to resume/finalise
                in place (used by "Continue" to re-run a paused task on the same
                record instead of creating a new one).
            prior_observations: Optional observation history from a previous
                (paused) turn, re-injected so the model continues with the
                context it already built instead of starting from scratch.

        Returns:
            A dict with ``iterations``, ``observations``, ``final_text`` and
            ``stop_reason``.
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("AGENT_MAX_ITERATIONS", 30))
        if timeout_seconds is None:
            timeout_seconds = float(
                config_registry.get_var("AGENT_TURN_TIMEOUT_SEC", 120)
            )

        # Resolve the Cortex engine for the agentic loop. When the caller did
        # not pin an explicit engine, honour the AGENT_CORTEX override (scope
        # "agent"): "Default" reuses the active Base Cortex, otherwise the
        # agent runs on a dedicated LLM better suited for tool-calling work.
        if not engine:
            try:
                from core.config import get_active_cortex_engine

                engine = await get_active_cortex_engine(scope="agent")
                log_debug(
                    f"[agent_core] Agentic loop engine resolved (scope=agent): {engine}"
                )
            except Exception as exc:
                log_warning(
                    f"[agent_core] Could not resolve agent-scope engine, "
                    f"falling back to active cortex: {exc}"
                )
                engine = None

        from core.agent_tool_executor import agent_tool_executor
        from core.transport_layer import extract_json_from_text

        # Derive the interface name from the originating interface_path when the
        # caller did not already supply it. The Agent Lane router only sets
        # ``interface_path`` on the context, but internal actions run by the tool
        # executor read ``context["interface"]`` (e.g. create_personal_diary_entry
        # would otherwise persist the entry as interface="unknown"). Enriching the
        # shared context once here means both the prompt and every executed tool
        # see the correct interface. Best-effort and purely structural.
        if isinstance(context, dict) and not context.get("interface"):
            src_path = context.get("interface_path")
            if isinstance(src_path, str) and src_path:
                try:
                    from core.interface_path_utils import get_interface_from_path

                    derived_interface = get_interface_from_path(src_path)
                    if derived_interface:
                        context["interface"] = derived_interface
                except Exception as exc:
                    log_debug(
                        f"[agent_core] Could not derive interface from "
                        f"interface_path {src_path!r}: {exc}"
                    )

        # Open a durable ``running`` row BEFORE the loop starts. A detached turn
        # (message-chain Agent Lane) can be interrupted by a container restart
        # mid-flight; persisting up-front means the startup recovery sweep can
        # detect the orphaned ``running`` row and surface it instead of the turn
        # vanishing silently. Best-effort: a None id just means the finalising
        # persist will INSERT a fresh row as before.
        #
        # When ``task_id`` is supplied (resume/"Continue" of a paused task) we
        # reuse that existing row instead of opening a new one, so the whole
        # multi-batch effort stays a single ``agent_tasks`` record.
        if task_id is None:
            task_id = await self._begin_agentic_turn(
                engine=engine,
                goal=goal,
                context=context,
                original_message=original_message,
                preplanned_calls=preplanned_calls,
            )
        else:
            # Resuming an existing (paused) row: flip it back to ``running`` so
            # the UI reflects the in-flight resume instead of showing it as
            # ``pending`` for the whole batch. Best-effort — never block the
            # turn on a status update failure.
            await self._mark_task_running(task_id)

        observations: list[Dict[str, Any]] = list(prior_observations or [])
        final_text = ""
        stop_reason = "max_iterations"

        # Diary discipline: a single agentic turn is ONE moment, not many. The
        # model must not write a diary entry on every iteration — at most one at
        # the start and one at the end. We enforce this deterministically (the
        # prompt guidance alone is not trusted with weak cortex engines) by
        # allowing diary tool calls only on the first iteration (start) and the
        # last allowed iteration (end), and suppressing them in between.
        _DIARY_TOOLS = {"create_personal_diary_entry", "update_diary_entry"}

        import time

        start = time.monotonic()

        # Deterministic path: execute a user-provided tool plan without relying
        # on model tool-call generation. Still uses the same tool executor/safety
        # gate and returns observations in the standard agent format.
        if preplanned_calls:
            from core.agent_tool_executor import agent_tool_executor

            capped_calls = preplanned_calls[: max(1, max_iterations)]
            for i, call in enumerate(capped_calls, start=1):
                if (time.monotonic() - start) > timeout_seconds:
                    stop_reason = "timeout"
                    break

                if not isinstance(call, dict):
                    observations.append(
                        {
                            "iteration": i,
                            "role": "tool_results",
                            "content": [
                                {
                                    "tool": "",
                                    "ok": False,
                                    "result": "",
                                    "error": "invalid_planned_call",
                                }
                            ],
                        }
                    )
                    continue

                name = str(call.get("name") or call.get("type") or "").strip()
                args = call.get("arguments", call.get("payload", {}))
                if not isinstance(args, dict):
                    args = {}

                exec_result = await agent_tool_executor.execute(
                    name,
                    args,
                    context=context or {"from_cortex": True, "agent_tool": True},
                    original_message=original_message,
                )
                observations.append(
                    {
                        "iteration": i,
                        "role": "tool_results",
                        "content": [
                            {
                                "tool": name,
                                "ok": exec_result.get("ok", False),
                                "result": exec_result.get("result", ""),
                                "error": exec_result.get("error"),
                            }
                        ],
                    }
                )

            if not stop_reason or stop_reason == "max_iterations":
                stop_reason = "planned_calls_done"
            result: Dict[str, Any] = {
                "iterations": len(observations),
                "observations": observations,
                "final_text": "",
                "stop_reason": stop_reason,
            }
            result["task_id"] = await self._persist_agentic_turn(
                engine=engine,
                goal=goal,
                result=result,
                context=context,
                original_message=original_message,
                preplanned_calls=preplanned_calls,
                task_id=task_id,
            )
            return result

        for i in range(1, max_iterations + 1):
            elapsed = time.monotonic() - start
            if elapsed > timeout_seconds:
                stop_reason = "timeout"
                break

            remaining_budget = max(1.0, timeout_seconds - elapsed)
            # Per-call timeout must respect the real engine budget, not an
            # arbitrary hardcoded cap. Browser-backed cortex engines (e.g.
            # selenium-llm-engine) routinely need far longer than a few seconds
            # to produce a response; capping each call at 8s made every
            # iteration time out with an empty reply, so the whole agentic turn
            # returned nothing. Bound the per-call wait by the engine's own
            # response timeout (AWAIT_RESPONSE_TIMEOUT) and the remaining turn
            # budget, whichever is smaller.
            engine_timeout = float(
                config_registry.get_var("AWAIT_RESPONSE_TIMEOUT", 600)
            )
            per_call_timeout = max(2.0, min(engine_timeout, remaining_budget))

            # Build the iteration prompt: goal + prior observations.
            prompt = self._build_agent_prompt(goal, observations, engine, context)

            try:
                if engine:
                    # An engine is pinned (via caller or the AGENT_CORTEX
                    # override) — call it directly through the Cortex registry so
                    # the agentic loop actually runs on the selected engine
                    # instead of the generic active plugin.
                    raw_response = await asyncio.wait_for(
                        self._call_engine_direct(prompt, engine),
                        timeout=per_call_timeout,
                    )
                else:
                    from core import plugin_instance

                    raw_response = await asyncio.wait_for(
                        plugin_instance.handle_incoming_message(
                            bot=None, message=None, context_memory_or_prompt=prompt
                        ),
                        timeout=per_call_timeout,
                    )
            except asyncio.TimeoutError:
                log_warning(
                    f"[agent_core] Engine call timed out at iteration {i} "
                    f"after {per_call_timeout:.1f}s"
                )
                raw_response = ""
            except Exception as exc:
                log_error(f"[agent_core] Engine call failed at iteration {i}: {exc}")
                observations.append(
                    {"iteration": i, "role": "error", "content": str(exc)}
                )
                stop_reason = "engine_error"
                break

            raw_text = (
                raw_response
                if isinstance(raw_response, str)
                else (str(raw_response) if raw_response is not None else "")
            )

            if not raw_text.strip():
                try:
                    remaining_after_primary = max(
                        1.0, timeout_seconds - (time.monotonic() - start)
                    )
                    fallback_text = await asyncio.wait_for(
                        self._call_engine_direct(prompt, engine),
                        timeout=max(2.0, min(engine_timeout, remaining_after_primary)),
                    )
                except asyncio.TimeoutError:
                    fallback_text = ""
                if fallback_text:
                    raw_text = fallback_text

            parsed, _meta = extract_json_from_text(raw_text, return_metadata=True)
            tool_calls = self._extract_tool_calls(parsed)

            # Explicit completion signal. The model ends the turn by calling the
            # dedicated ``attempt_completion`` tool (a sentinel, not a real
            # action). Its ``summary``/``text`` becomes the final answer. This is
            # the language-agnostic, structural end-of-turn marker used by robust
            # agents — absence of tool calls alone concludes nothing.
            completion_calls = [
                c
                for c in tool_calls
                if str(c.get("name") or c.get("type") or "").strip() == _COMPLETION_TOOL
            ]
            if completion_calls:
                tool_calls = [c for c in tool_calls if c not in completion_calls]
                summary_parts: list[str] = []
                for cc in completion_calls:
                    args = cc.get("arguments") or cc.get("payload") or {}
                    if isinstance(args, dict):
                        text = str(
                            args.get("summary")
                            or args.get("text")
                            or args.get("content")
                            or ""
                        ).strip()
                        if text:
                            summary_parts.append(text)
                if summary_parts:
                    final_text = "\n\n".join(summary_parts)
                observations.append(
                    {
                        "iteration": i,
                        "role": "assistant",
                        "content": final_text or "(completed)",
                    }
                )
                stop_reason = "completed"
                break

            # Synth actions vs tools. A plain outbound message action (e.g.
            # ``message_telegram_bot``) is Synth talking to the user, NOT a tool
            # the agent should feed back into its loop. Recognise those synth
            # actions and split them out: the tool executor keeps handling real
            # tools, while the message text becomes the turn's final reply. This
            # stops message actions from interfering with the agent loop (being
            # executed as "tools" and driving further iterations).
            message_calls = [
                c
                for c in tool_calls
                if _is_pure_message(str(c.get("name") or c.get("type") or ""))
            ]
            if message_calls:
                tool_calls = [c for c in tool_calls if c not in message_calls]
                collected: list[str] = []
                for mc in message_calls:
                    args = mc.get("arguments") or mc.get("payload") or {}
                    if isinstance(args, dict):
                        text = str(
                            args.get("text") or args.get("content") or ""
                        ).strip()
                        if text:
                            collected.append(text)
                if collected:
                    final_text = "\n\n".join(collected)

            # Under the explicit-completion contract, the ONLY structural
            # end-of-turn signal is ``attempt_completion`` (handled above) or
            # exhausting the iteration budget. A plain outbound message — even on
            # a synchronous interface like Ollama — is NOT proof the goal is
            # done: weak models routinely emit an intent statement ("I'll check
            # the codebase now...") as a message and would otherwise stop there.
            require_explicit_completion = bool(
                config_registry.get_var("AGENT_REQUIRE_EXPLICIT_COMPLETION", True)
            )

            if not tool_calls:
                # Only synth message actions this iteration (no real tools left).
                if message_calls and final_text.strip():
                    if require_explicit_completion and i < max_iterations:
                        # Deliver the message as an intermediate reply but keep
                        # working: the model must still call attempt_completion
                        # (or run out of iterations) to genuinely end the turn.
                        observations.append(
                            {
                                "iteration": i,
                                "role": "assistant",
                                "content": final_text,
                            }
                        )
                        observations.append(
                            {
                                "iteration": i,
                                "role": "system",
                                "content": (
                                    "You sent a message but the goal is NOT "
                                    "finished yet. A message is not a completion "
                                    "signal. Do not stop at an intent statement — "
                                    "take the next tool action to make real "
                                    "progress. When (and only when) the goal is "
                                    "genuinely accomplished, call the "
                                    f"{_COMPLETION_TOOL} tool with a short summary."
                                ),
                            }
                        )
                        stop_reason = "max_iterations"
                        continue

                    # Explicit-completion ON but iterations exhausted: the goal
                    # was never explicitly completed. Do NOT declare the task
                    # done — pause it so the user can grant more iterations via
                    # "Continue". The message is kept as the latest reply.
                    if require_explicit_completion:
                        observations.append(
                            {"iteration": i, "role": "assistant", "content": final_text}
                        )
                        stop_reason = "paused_max_iterations"
                        break

                    # Explicit-completion disabled: the message is the final
                    # reply; end the turn.
                    observations.append(
                        {"iteration": i, "role": "assistant", "content": final_text}
                    )
                    stop_reason = "model_done"
                    break

                if not raw_text.strip():
                    observations.append(
                        {
                            "iteration": i,
                            "role": "error",
                            "content": "empty_model_response",
                        }
                    )
                    stop_reason = "empty_response"
                    continue

                # Bare text, no tool calls, no user-facing message. Under the
                # explicit-completion contract this is NOT "done": weak models
                # often stop here with an intent statement ("I'll check the
                # codebase now...") instead of actually finishing the goal. Keep
                # the text as an intermediate observation, re-inject a structural
                # nudge, and continue the loop. The turn only ends via a user
                # message, ``attempt_completion``, or exhausting iterations.
                if require_explicit_completion and i < max_iterations:
                    observations.append(
                        {"iteration": i, "role": "assistant", "content": raw_text}
                    )
                    observations.append(
                        {
                            "iteration": i,
                            "role": "system",
                            "content": (
                                "You responded with text but no tool calls and no "
                                "user message. The goal is NOT finished yet. Do not "
                                "stop at an intent statement — take the next tool "
                                "action to make real progress. When (and only when) "
                                "the goal is genuinely accomplished, call the "
                                f"{_COMPLETION_TOOL} tool with a short summary, or "
                                "send the final answer as a user message."
                            ),
                        }
                    )
                    final_text = raw_text
                    stop_reason = "max_iterations"
                    continue

                # Explicit-completion ON but iterations exhausted with only a
                # bare intent statement: the goal was never explicitly finished.
                # Pause the task instead of faking completion, so the user can
                # grant more iterations via "Continue".
                if require_explicit_completion:
                    final_text = raw_text
                    observations.append(
                        {"iteration": i, "role": "assistant", "content": raw_text}
                    )
                    stop_reason = "paused_max_iterations"
                    break

                # Explicit-completion disabled: fall back to the legacy
                # behaviour and keep the text as final.
                final_text = raw_text
                observations.append(
                    {"iteration": i, "role": "assistant", "content": raw_text}
                )
                stop_reason = "model_done"
                break

            # Execute each tool call and collect observations.
            # Diary entries are allowed only on the first (start) and last (end)
            # iteration; suppress them on the intermediate working iterations so
            # a single task produces at most one opening and one closing entry.
            diary_allowed_this_iteration = i == 1 or i == max_iterations
            iteration_results: list[Dict[str, Any]] = []
            for call in tool_calls:
                name = call.get("name") or call.get("type") or ""
                args = call.get("arguments") or call.get("payload") or {}
                if not isinstance(args, dict):
                    args = {}
                if name in _DIARY_TOOLS and not diary_allowed_this_iteration:
                    log_info(
                        f"[agent_core] Iteration {i}: suppressing mid-task diary "
                        f"tool '{name}' (diary allowed only at start/end of turn)"
                    )
                    iteration_results.append(
                        {
                            "tool": name,
                            "ok": False,
                            "result": "",
                            "error": (
                                "diary_suppressed_mid_task: write a diary entry "
                                "only at the start or the end of the task, not on "
                                "intermediate iterations"
                            ),
                        }
                    )
                    continue
                exec_result = await agent_tool_executor.execute(
                    name,
                    args,
                    context=context or {"from_cortex": True, "agent_tool": True},
                    original_message=original_message,
                )
                iteration_results.append(
                    {
                        "tool": name,
                        "ok": exec_result.get("ok", False),
                        "result": exec_result.get("result", ""),
                        "error": exec_result.get("error"),
                    }
                )
                log_info(
                    f"[agent_core] Iteration {i}: tool '{name}' "
                    f"ok={exec_result.get('ok')}"
                )

            observations.append(
                {
                    "iteration": i,
                    "role": "tool_results",
                    "content": iteration_results,
                }
            )

        # Loop fell through the iteration budget while still executing tool
        # calls (never called attempt_completion). Under the explicit-completion
        # contract this is NOT a completion — pause the task so the user can
        # grant more iterations via "Continue" instead of marking it done.
        if stop_reason == "max_iterations" and bool(
            config_registry.get_var("AGENT_REQUIRE_EXPLICIT_COMPLETION", True)
        ):
            stop_reason = "paused_max_iterations"

        # Paused (budget exhausted without explicit completion): let Synth
        # author the "I'm not done, shall I continue?" message itself, in the
        # conversation's own language/tone, instead of shipping a hardcoded
        # English string. Overwrite ``final_text`` with the composed message so
        # every delivery path (Telegram/Discord/API) shows Synth's own words.
        if stop_reason == "paused_max_iterations":
            actions_executed = 0
            for obs in observations:
                if isinstance(obs, dict) and obs.get("role") == "tool_results":
                    content = obs.get("content")
                    if isinstance(content, list):
                        actions_executed += len(content)
            composed = await self._compose_pause_message(
                goal=goal,
                observations=observations,
                actions_executed=actions_executed,
                engine=engine,
                context=context,
            )
            if composed:
                final_text = composed

        result: Dict[str, Any] = {
            "iterations": len(observations),
            "observations": observations,
            "final_text": final_text,
            "stop_reason": stop_reason,
        }
        result["task_id"] = await self._persist_agentic_turn(
            engine=engine,
            goal=goal,
            result=result,
            context=context,
            original_message=original_message,
            preplanned_calls=preplanned_calls,
            task_id=task_id,
        )
        return result

    async def run_drone(
        self,
        *,
        goal: str,
        engine: str | None = None,
        context: Dict[str, Any] | None = None,
        parent_task_id: int | None = None,
        max_iterations: int | None = None,
        timeout_seconds: float | None = None,
        original_message: Any = None,
    ) -> Dict[str, Any]:
        """Run an ephemeral, task-scoped sub-agent ("Drone").

        A Drone is a single-level delegation: it runs through the same bounded
        :meth:`run_agentic_turn` loop but with a tighter budget
        (``DRONE_MAX_ITERATIONS`` / ``DRONE_TURN_TIMEOUT_SEC``) and is flagged so
        it cannot spawn further Drones. The flag is enforced both by the
        ``spawn_drone`` handler (recursion guard) and by
        :meth:`_build_agent_prompt`, which hides the ``spawn_drone`` tool from a
        Drone's tool list.

        Args:
            goal: The focused sub-task objective for the Drone.
            engine: Optional cortex engine name. When ``None`` the Drone inherits
                the agent-scope engine (same resolution as the parent Agent).
            context: Optional context dict; a ``drone`` marker is injected.
            parent_task_id: DB id of the Agent task that spawned this Drone.
            max_iterations: Hard cap (defaults to ``DRONE_MAX_ITERATIONS``).
            timeout_seconds: Wall-clock budget (defaults to ``DRONE_TURN_TIMEOUT_SEC``).
            original_message: Optional originating message (for audit/safety).

        Returns:
            The standard :meth:`run_agentic_turn` result dict (``iterations``,
            ``observations``, ``final_text``, ``stop_reason``, ``task_id``).
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("DRONE_MAX_ITERATIONS", 3))
        if timeout_seconds is None:
            timeout_seconds = float(
                config_registry.get_var("DRONE_TURN_TIMEOUT_SEC", 90)
            )

        drone_context: Dict[str, Any] = dict(context or {})
        drone_context["drone"] = {
            "is_drone": True,
            "parent_task_id": parent_task_id,
        }

        log_info(
            f"[agent_core] Spawning Drone (parent_task_id={parent_task_id}, "
            f"max_iterations={max_iterations}, timeout={timeout_seconds}s)"
        )

        return await self.run_agentic_turn(
            goal=goal,
            engine=engine,
            context=drone_context,
            max_iterations=max_iterations,
            timeout_seconds=timeout_seconds,
            original_message=original_message,
        )

    async def _call_engine_direct(
        self,
        prompt: Dict[str, Any],
        engine_name: str | None,
    ) -> str:
        """Fallback direct call to the active cortex engine.

        Some runtime paths can return an empty string through ``plugin_instance``
        even when the model produced output. This fallback talks to the engine
        directly and returns its raw text response.
        """
        try:
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            resolved_engine = engine_name or await get_active_cortex_engine()
            if not resolved_engine:
                return ""

            registry = get_cortex_registry()
            engine = registry.get_engine(resolved_engine) or registry.load_engine(
                resolved_engine
            )
            if engine is None:
                return ""

            # Agentic turns MUST go through the role-separated message path.
            # The agent prompt carries an ``agent_turn`` block under the
            # ``system_message`` key; ``handle_incoming_message`` -> _build_messages
            # would mistake that for a corrector payload (it keys off
            # ``system_message``), discarding the real GOAL/TOOLS/system text and
            # emitting an almost-empty prompt. External web-driven engines (e.g.
            # selenium-llm-engine) then pad that empty prompt with their own
            # canvas/JSON boilerplate. Passing explicit role-separated messages to
            # ``generate_response`` bypasses _build_messages entirely and delivers
            # the actual agentic prompt.
            payload = prompt.get("input", {}).get("payload", {})
            text = str(payload.get("text", ""))
            system = str(payload.get("system", ""))
            if prompt.get("agent_mode") and hasattr(engine, "generate_response"):
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ]
                res = await engine.generate_response(messages)
                return res if isinstance(res, str) else (str(res) if res else "")

            if hasattr(engine, "handle_incoming_message"):
                try:
                    # Common signature used by many Cortex engines
                    res = await engine.handle_incoming_message(
                        bot=None,
                        message=None,
                        context_memory_or_prompt=prompt,
                    )
                except TypeError:
                    # Fallback for engines expecting positional prompt arg.
                    res = await engine.handle_incoming_message(None, None, prompt)
                return res if isinstance(res, str) else (str(res) if res else "")

            if hasattr(engine, "generate_response"):
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ]
                res = await engine.generate_response(messages)
                return res if isinstance(res, str) else (str(res) if res else "")
        except Exception as exc:
            log_debug(f"[agent_core] Direct engine fallback failed: {exc}")
        return ""

    async def _compose_pause_message(
        self,
        *,
        goal: str,
        observations: list[Dict[str, Any]],
        actions_executed: int,
        engine: str | None,
        context: Dict[str, Any] | None,
    ) -> str:
        """Have Synth write the "I need more turns, shall I continue?" message.

        When an agentic turn exhausts its iteration budget without an explicit
        completion, the user must be told — but the text must be authored BY the
        model in the language and tone of the ongoing conversation, never a
        hardcoded English string (which would also wrongly reference a WebUI-only
        "Continue" button on chat interfaces where none exists).

        This asks the active cortex to produce a short, natural, plain-text
        message summarising what was done so far and asking whether to keep
        going. It returns the generated text, or an empty string on any failure
        (the caller then falls back to the model's last real reply).
        """
        try:
            instruction = (
                "You are Synth. You have been working on a task for a user but "
                "have reached your action budget for this turn WITHOUT finishing "
                f"it. So far you have carried out {actions_executed} action(s). "
                "Write a SHORT, natural message to the user, in the SAME language "
                "and tone as the ongoing conversation, that: (1) briefly says what "
                "you have been doing, (2) makes clear the task is not finished "
                "yet, and (3) asks whether they want you to continue. Do NOT "
                "mention any button, UI element or technical detail — the user "
                "may just reply in chat to tell you to keep going. Reply with the "
                "message text ONLY, no JSON, no tool call, no quotes."
            )
            history_lines: list[str] = []
            for obs in observations[-12:]:
                role = obs.get("role", "system")
                content = obs.get("content", "")
                if isinstance(content, list):
                    for item in content:
                        status = (
                            "OK" if item.get("ok") else f"ERROR: {item.get('error')}"
                        )
                        history_lines.append(f"[tool:{item.get('tool')}] {status}")
                else:
                    history_lines.append(f"[{role}] {content}")
            history_block = "\n".join(history_lines)
            prompt: Dict[str, Any] = {
                "input": {
                    "payload": {
                        "text": (
                            f"GOAL: {goal}\n\nWHAT YOU DID SO FAR:\n{history_block}\n"
                        ),
                        "system": instruction,
                    }
                },
                "system_message": {
                    "type": "agent_turn",
                    "engine": engine,
                    "goal": goal,
                },
                "agent_mode": True,
                "observation_history": observations,
            }
            if context:
                prompt["context"] = context
            text = await self._call_engine_direct(prompt, engine)
            return text.strip() if isinstance(text, str) else ""
        except Exception as exc:
            log_debug(f"[agent_core] _compose_pause_message failed: {exc}")
            return ""

    @staticmethod
    def _build_agent_prompt(
        goal: str,
        observations: list[Dict[str, Any]],
        engine: str | None,
        context: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        """Assemble the per-iteration prompt including observation history."""
        from core.tool_registry import tool_registry

        history_lines: list[str] = []
        for obs in observations:
            role = obs.get("role", "system")
            content = obs.get("content", "")
            if isinstance(content, list):
                # tool_results list
                for item in content:
                    status = "OK" if item.get("ok") else f"ERROR: {item.get('error')}"
                    history_lines.append(
                        f"[tool:{item.get('tool')}] {status}\n{item.get('result', '')}"
                    )
            else:
                history_lines.append(f"[{role}] {content}")

        observation_block = "\n".join(history_lines)

        # Expose the originating conversation to the model. Message-delivery
        # actions (e.g. message_telegram_bot / audio_telegram_bot) declare
        # ``interface_path`` as a REQUIRED field, but the agentic prompt only
        # carried GOAL + TOOLS + OBSERVATIONS — the model never saw the source
        # interface_path and therefore either omitted it (payload validation
        # failed with "interface_path or chat_name is required") or invented a
        # wrong one, so the message and the diary entry silently failed to land.
        # Surfacing it here lets the model reply in the same conversation and
        # gives diary actions the context they need. No keyword/language logic —
        # purely structural, driven by the interface_path already in context.
        source_block = ""
        if isinstance(context, dict):
            source_interface_path = context.get("interface_path")
            source_interface = context.get("interface")
            if source_interface_path:
                lines = [
                    "SOURCE CONVERSATION (use this to talk back to the user):",
                    f"- interface_path: {source_interface_path}",
                ]
                if source_interface:
                    lines.append(f"- interface: {source_interface}")
                lines.append(
                    "When you call a message/delivery action (e.g. "
                    "message_telegram_bot), you MUST set its 'interface_path' "
                    "field to EXACTLY the interface_path above so the reply "
                    "reaches this same conversation."
                )
                source_block = "\n".join(lines)

        # Drones cannot spawn Drones: hide the spawn_drone tool from a Drone's
        # available tool list (single-level delegation). The handler enforces the
        # same rule defensively, this just keeps the model from ever proposing it.
        is_drone = bool(
            isinstance(context, dict)
            and isinstance(context.get("drone"), dict)
            and context["drone"].get("is_drone")
        )

        tool_lines: list[str] = []
        for tool in tool_registry.all_tools():
            if is_drone and tool.name == "spawn_drone":
                continue
            params = []
            for p in tool.parameters:
                ptype = p.type or "string"
                req = "required" if p.required else "optional"
                params.append(f"{p.name}:{ptype}({req})")
            params_block = ", ".join(params) if params else "no-params"
            tool_lines.append(
                f"- {tool.name} | source={tool.source} | security={tool.security_level} | params=[{params_block}]"
            )
        # The completion sentinel is always available and is how the model ends
        # the turn. It is intercepted by the loop, not executed as a real action.
        tool_lines.append(
            f"- {_COMPLETION_TOOL} | source=agent | security=safe | "
            "params=[summary:string(required)]"
        )
        tools_block = "\n".join(tool_lines) if tool_lines else "- (no tools registered)"

        system_text = (
            "You are Synth operating in agentic mode. Achieve the goal using "
            "the available tools. When you need a tool, respond ONLY with the "
            "tool-call JSON actions. Use only tool names from the AVAILABLE TOOLS "
            "block.\n"
            "Work like a careful engineer: break the goal into steps and keep "
            "calling tools to gather information, verify assumptions and make "
            "progress until the goal is genuinely achieved. Do NOT stop and give "
            "a final answer while the goal is only partially done or still "
            "unverified — inspect results, and take the next tool action if more "
            "work remains.\n"
            "ENDING THE TURN: neither plain text NOR a message action ends the "
            "task on its own — the loop keeps going. Announcing what you are "
            'about to do ("I\'ll now check...") is never a completion, even if '
            f"phrased as a message. The ONLY way to finish is to call the "
            f"{_COMPLETION_TOOL} tool with a short summary of what you "
            "accomplished, once the goal is genuinely done (or you can clearly "
            "explain, based on tool results, why it cannot be). Send message "
            "actions to talk to the user while you work, but keep emitting the "
            f"next tool call until you call {_COMPLETION_TOOL}.\n"
            "Each tool observation is already in PRIOR OBSERVATIONS — build on it "
            "rather than repeating an identical call.\n"
            "Diary discipline: this is a single agentic task, not many separate "
            "moments. Do NOT write a diary entry on every iteration. At most, "
            "record one diary entry when you begin the task and one when it is "
            "finished. During the intermediate working iterations do NOT call any "
            "diary tool (create_personal_diary_entry / update_diary_entry) — just "
            "use the tools needed to make progress. If the prior observations "
            "already show a diary entry was written for this task, do not write "
            "another one until the task is complete."
        )
        source_prefix = f"{source_block}\n\n" if source_block else ""
        prompt = {
            "input": {
                "payload": {
                    "text": (
                        f"{source_prefix}"
                        f"GOAL: {goal}\n\n"
                        f"AVAILABLE TOOLS:\n{tools_block}\n\n"
                        f"PRIOR OBSERVATIONS:\n{observation_block}\n"
                        if observation_block
                        else (
                            f"{source_prefix}"
                            f"GOAL: {goal}\n\nAVAILABLE TOOLS:\n{tools_block}\n"
                        )
                    ),
                }
            },
            "system_message": {
                "type": "agent_turn",
                "engine": engine,
                "goal": goal,
            },
            "agent_mode": True,
            "observation_history": observations,
        }
        if context:
            prompt["context"] = context
        # Attach the system instruction so engines that honor it will use it.
        prompt["input"]["payload"]["system"] = system_text
        return prompt

    @staticmethod
    def _extract_tool_calls(parsed: Any) -> list[Dict[str, Any]]:
        """Normalize parsed LLM JSON into a list of tool-call dicts."""

        def _normalize_args(value: Any) -> dict[str, Any]:
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                try:
                    parsed_value = json.loads(value)
                    return parsed_value if isinstance(parsed_value, dict) else {}
                except Exception:
                    return {}
            return {}

        if not isinstance(parsed, dict):
            return []
        # Standard SyntH shape: {"actions": [{"type":..., "payload":...}]}
        actions = parsed.get("actions")
        if isinstance(actions, list):
            calls: list[Dict[str, Any]] = []
            for a in actions:
                if isinstance(a, dict) and a.get("type"):
                    calls.append(
                        {
                            "name": a["type"],
                            "arguments": _normalize_args(a.get("payload", {})),
                        }
                    )
            return calls

        # OpenAI-ish shape: {"tool_calls": [{"function": {"name":..., "arguments": ...}}]}
        tool_calls = parsed.get("tool_calls")
        if isinstance(tool_calls, list):
            calls: list[Dict[str, Any]] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if isinstance(fn, dict) and fn.get("name"):
                    calls.append(
                        {
                            "name": str(fn.get("name")),
                            "arguments": _normalize_args(fn.get("arguments", {})),
                        }
                    )
            if calls:
                return calls

        # Generic shape: {"calls": [{"name":..., "arguments":...}]}
        generic_calls = parsed.get("calls")
        if isinstance(generic_calls, list):
            calls: list[Dict[str, Any]] = []
            for c in generic_calls:
                if isinstance(c, dict) and c.get("name"):
                    calls.append(
                        {
                            "name": str(c.get("name")),
                            "arguments": _normalize_args(c.get("arguments", {})),
                        }
                    )
            if calls:
                return calls

        # Single action object. The tool/action name may be carried under any of
        # ``type`` / ``name`` / ``tool`` — different engines pick different keys
        # (e.g. logfare-claude emits ``{"tool": "attempt_completion", "payload":
        # {...}}``). Normalize all three the same way so the completion sentinel
        # and single tool calls are never silently dropped.
        name_key = parsed.get("type") or parsed.get("name") or parsed.get("tool")
        if name_key and (
            "arguments" in parsed or "payload" in parsed or "args" in parsed
        ):
            return [
                {
                    "name": str(name_key),
                    "arguments": _normalize_args(
                        parsed.get(
                            "arguments", parsed.get("payload", parsed.get("args", {}))
                        )
                    ),
                }
            ]
        if parsed.get("type"):
            return [
                {
                    "name": parsed["type"],
                    "arguments": _normalize_args(parsed.get("payload", {})),
                }
            ]
        return []


# Expose a convenient singleton manager
_agent_loop_manager = AgentLoopManager()


def get_agent_loop_manager() -> AgentLoopManager:
    return _agent_loop_manager
