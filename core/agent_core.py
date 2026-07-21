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

    async def _create_agent_task(
        self,
        engine: str,
        input_payload: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        try:
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO agent_tasks (engine, status, input, iterations_meta, output, trainer_id, metadata)
                        VALUES (%s, %s, %s, %s, %s, NULL, %s)
                        """,
                        (
                            engine,
                            "pending",
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
            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status=%s WHERE id=%s",
                        (status, int(task_id)),
                    )
                    await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _update_agent_task_status failed: {e}")

    async def _append_iteration_meta(
        self, task_id: int, iteration_meta: Dict[str, Any]
    ) -> None:
        if not task_id:
            return
        try:
            import json

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    # Fetch existing iterations_meta
                    await cur.execute(
                        "SELECT iterations_meta FROM agent_tasks WHERE id=%s",
                        (int(task_id),),
                    )
                    row = await cur.fetchone()
                    if row:
                        existing = row[0] or "[]"
                    else:
                        existing = "[]"
                    try:
                        arr = json.loads(existing)
                    except Exception:
                        arr = []
                    arr.append(iteration_meta)
                    await cur.execute(
                        "UPDATE agent_tasks SET iterations_meta=%s WHERE id=%s",
                        (json.dumps(arr), int(task_id)),
                    )
                    await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _append_iteration_meta failed: {e}")

    async def _finalize_task(
        self, task_id: int, status: str, output: Optional[Dict[str, Any]] = None
    ) -> None:
        try:
            import json

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE agent_tasks SET status=%s, output=%s WHERE id=%s",
                        (status, json.dumps(output) if output else None, int(task_id)),
                    )
                    await self._maybe_commit(conn)
        except Exception as e:
            log_warning(f"[agent_core] _finalize_task failed: {e}")

    async def _persist_agentic_turn(
        self,
        *,
        engine: str | None,
        goal: str,
        result: Dict[str, Any],
        context: Optional[Dict[str, Any]] = None,
        original_message: Any = None,
        preplanned_calls: Optional[list[Dict[str, Any]]] = None,
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
            status = (
                "failed"
                if stop_reason in {"timeout", "engine_error", "empty_response"}
                else "completed"
            )

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
            }
            metadata = {
                "source": source,
                "interface_path": interface_path,
                "has_preplanned_calls": bool(isinstance(preplanned_calls, list)),
            }

            conn_ctx = await self._get_conn_ctx()
            async with conn_ctx as conn:
                async with conn.cursor() as cur:
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
    async def run_loop(
        self,
        *,
        engine: str,
        input_payload: Dict[str, Any],
        context: Dict[str, Any] | None = None,
        max_iterations: int | None = None,
    ) -> Optional[int]:
        """Run an agent task loop as a background-friendly coroutine.

        Returns the agent task id (db) if created, else None.
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("AGENT_MAX_ITERATIONS", 5))

        task_id = await self._create_agent_task(
            engine, input_payload, metadata=context or {}
        )
        if not task_id:
            log_error("[agent_core] Failed to create agent task in DB")
            return None

        # Launch the loop in background and store the task
        loop_task = asyncio.create_task(
            self._run_loop_background(
                task_id, engine, input_payload, context or {}, max_iterations
            )
        )
        self._running_tasks[task_id] = loop_task
        return task_id

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
    ) -> Dict[str, Any]:
        """Run a bounded agentic turn that re-injects tool results into the model.

        This is the real agent loop (the previous ``_run_loop_background`` was a
        conservative scaffold).  Each iteration:

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

        Returns:
            A dict with ``iterations``, ``observations``, ``final_text`` and
            ``stop_reason``.
        """
        if max_iterations is None:
            max_iterations = int(config_registry.get_var("AGENT_MAX_ITERATIONS", 5))
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

        observations: list[Dict[str, Any]] = []
        final_text = ""
        stop_reason = "max_iterations"

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
            )
            return result

        for i in range(1, max_iterations + 1):
            elapsed = time.monotonic() - start
            if elapsed > timeout_seconds:
                stop_reason = "timeout"
                break

            remaining_budget = max(1.0, timeout_seconds - elapsed)
            per_call_timeout = min(8.0, max(2.0, remaining_budget / 2.0))

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
                        timeout=max(1.0, min(15.0, remaining_after_primary)),
                    )
                except asyncio.TimeoutError:
                    fallback_text = ""
                if fallback_text:
                    raw_text = fallback_text

            parsed, _meta = extract_json_from_text(raw_text, return_metadata=True)
            tool_calls = self._extract_tool_calls(parsed)

            if not tool_calls:
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

                # No more tool calls -> model is done; keep its text as final.
                final_text = raw_text
                observations.append(
                    {"iteration": i, "role": "assistant", "content": raw_text}
                )
                stop_reason = "model_done"
                break

            # Execute each tool call and collect observations.
            iteration_results: list[Dict[str, Any]] = []
            for call in tool_calls:
                name = call.get("name") or call.get("type") or ""
                args = call.get("arguments") or call.get("payload") or {}
                if not isinstance(args, dict):
                    args = {}
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
        )
        return result

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
                payload = prompt.get("input", {}).get("payload", {})
                text = str(payload.get("text", ""))
                system = str(payload.get("system", ""))
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ]
                res = await engine.generate_response(messages)
                return res if isinstance(res, str) else (str(res) if res else "")
        except Exception as exc:
            log_debug(f"[agent_core] Direct engine fallback failed: {exc}")
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

        tool_lines: list[str] = []
        for tool in tool_registry.all_tools():
            params = []
            for p in tool.parameters:
                ptype = p.type or "string"
                req = "required" if p.required else "optional"
                params.append(f"{p.name}:{ptype}({req})")
            params_block = ", ".join(params) if params else "no-params"
            tool_lines.append(
                f"- {tool.name} | source={tool.source} | security={tool.security_level} | params=[{params_block}]"
            )
        tools_block = "\n".join(tool_lines) if tool_lines else "- (no tools registered)"

        system_text = (
            "You are Synth operating in agentic mode. Achieve the goal using "
            "the available tools. When you need a tool, respond ONLY with the "
            "tool-call JSON actions. When the goal is complete, respond with a "
            "final natural-language answer and no tool calls. Use only tool names "
            "from the AVAILABLE TOOLS block."
        )
        prompt = {
            "input": {
                "payload": {
                    "text": (
                        f"GOAL: {goal}\n\n"
                        f"AVAILABLE TOOLS:\n{tools_block}\n\n"
                        f"PRIOR OBSERVATIONS:\n{observation_block}\n"
                        if observation_block
                        else f"GOAL: {goal}\n\nAVAILABLE TOOLS:\n{tools_block}\n"
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

        # Single action object.
        if parsed.get("type"):
            return [
                {
                    "name": parsed["type"],
                    "arguments": _normalize_args(parsed.get("payload", {})),
                }
            ]
        if parsed.get("name") and (
            "arguments" in parsed or "payload" in parsed or "args" in parsed
        ):
            return [
                {
                    "name": str(parsed.get("name")),
                    "arguments": _normalize_args(
                        parsed.get(
                            "arguments", parsed.get("payload", parsed.get("args", {}))
                        )
                    ),
                }
            ]
        return []

    async def _run_loop_background(
        self,
        task_id: int,
        engine: str,
        input_payload: Dict[str, Any],
        context: Dict[str, Any],
        max_iterations: int,
    ) -> None:
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
                    import time
                except Exception as e:
                    log_error(f"[agent_core] Failed to import runtime helpers: {e}")
                    result = {"ok": False, "error": "internal import failure"}
                    iteration_meta = {
                        "iteration": i,
                        "status": "completed",
                        "result": result,
                    }
                    await self._append_iteration_meta(task_id, iteration_meta)
                    break

                # Build a structured prompt for the agent iteration
                prompt = {
                    "input": {
                        "payload": {
                            "text": input_payload
                            if isinstance(input_payload, str)
                            else input_payload,
                            "iteration": i,
                            "task_id": task_id,
                        }
                    },
                    "system_message": {
                        "type": "agent_iteration",
                        "task_id": task_id,
                        "iteration": i,
                        "engine": engine,
                    },
                }

                log_debug(
                    f"[agent_core] Running iteration {i} for task {task_id} using engine={engine}"
                )

                # Gather Recon contributions (preflight) and attach to the prompt
                try:
                    from core.recon import gather_recon_contributions

                    recon = await gather_recon_contributions(
                        message=None,
                        context_memory=None,
                        text=None,
                        tags=None,
                        keywords=None,
                    )
                    if recon:
                        prompt["recon"] = recon
                except Exception as e:
                    log_debug(f"[agent_core] Recon gather failed: {e}")

                try:
                    # Ask the active engine (via plugin_instance) to produce JSON actions or a response
                    raw_response = await plugin_instance.handle_incoming_message(
                        bot=None, message=None, context_memory_or_prompt=prompt
                    )
                    raw_text = (
                        raw_response
                        if isinstance(raw_response, str)
                        else (str(raw_response) if raw_response is not None else "")
                    )
                except Exception as e:
                    raw_text = f"⚠️ LLM invocation failed: {e}"

                # Try to extract JSON from the LLM output
                parsed_json, meta = extract_json_from_text(
                    raw_text, return_metadata=True
                )

                if not parsed_json:
                    # Record raw text when no JSON actions found
                    result = {"ok": False, "raw_text": raw_text, "metadata": meta}
                    iteration_meta = {
                        "iteration": i,
                        "status": "completed",
                        "result": result,
                    }
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
                orig_msg = SimpleNamespace(
                    from_cortex=True,
                    chat_id=f"agent_task_{task_id}",
                    message_id=int(time.time() * 1000) % 1_000_000,
                    text=raw_text,
                    interface_path="agent",
                )

                # Run actions via action parser
                try:
                    context = {"from_cortex": True, "task_id": task_id, "iteration": i}
                    run_result = await run_actions(actions, context, None, orig_msg)
                except Exception as e:
                    run_result = {
                        "processed": [],
                        "errors": [str(e)],
                        "failed_actions": [],
                    }

                iteration_meta = {
                    "iteration": i,
                    "status": "completed",
                    "llm_raw": raw_text,
                    "actions_result": run_result,
                }
                await self._append_iteration_meta(task_id, iteration_meta)

                # If any plugin created a proposal (agent_activity_log.status='proposed') recently, pause and wait for approval
                try:
                    conn_ctx = await self._get_conn_ctx()
                    async with conn_ctx as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT id, command, request_ts FROM agent_activity_log WHERE status=%s ORDER BY request_ts DESC LIMIT 1",
                                ("proposed",),
                            )
                            row = await cur.fetchone()
                            if row:
                                prop_id, prop_cmd, prop_ts = row[0], row[1], row[2]
                                # best-effort: if proposal is recent (last 60s) then consider it part of this task
                                import datetime

                                if prop_ts and isinstance(prop_ts, datetime.datetime):
                                    age = (
                                        datetime.datetime.utcnow() - prop_ts
                                    ).total_seconds()
                                else:
                                    age = 0
                                if age < 120:
                                    await self._update_agent_task_status(
                                        task_id, "waiting_for_approval"
                                    )
                                    self.pause_task(task_id)
                                    notify_intelligent(
                                        f"Agent task #{task_id} paused: awaiting approval for proposal #{prop_id}: {prop_cmd}"
                                    )
                                    # Don't continue iterations until approval/resume
                                    break
                except Exception:
                    # Non-fatal - proceed
                    pass

                # Honor explicit stop condition in returned JSON meta
                if (
                    isinstance(parsed_json, dict)
                    and parsed_json.get("meta")
                    and isinstance(parsed_json.get("meta"), dict)
                ):
                    if parsed_json.get("meta", {}).get("agent_continue") is False:
                        break

            output = {"final": "done", "iterations": max_iterations}
            await self._finalize_task(task_id, "completed", output)

            # Run Debrief hooks with processed iterations
            try:
                from core.debrief import run_debrief

                # Load iterations_meta for context
                await run_debrief(
                    processed_actions=[],
                    failed_actions=[],
                    results=output,
                    context={"task_id": task_id},
                    original_message=None,
                )
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
            if hasattr(reg, "get_engine"):
                engine = reg.get_engine(name)
            elif hasattr(reg, "load_engine"):
                engine = reg.load_engine(name)
            # Attach if the engine exposes an attach_agent() hook (do not require supports_agent)
            if (
                engine
                and hasattr(engine, "attach_agent")
                and callable(engine.attach_agent)
            ):
                try:
                    engine.attach_agent(self)
                    self._engine = engine
                except Exception as e:
                    log_warning(
                        f"[agent_core] attach_to_active_engine attach failed: {e}"
                    )
        except Exception as e:
            log_warning(f"[agent_core] attach_to_active_engine failed: {e}")

    async def detach_from_engine(self) -> None:
        try:
            if (
                self._engine
                and hasattr(self._engine, "detach_agent")
                and callable(self._engine.detach_agent)
            ):
                try:
                    self._engine.detach_agent(self)
                finally:
                    self._engine = None
        except Exception as e:
            log_warning(f"[agent_core] detach_from_engine failed: {e}")

    async def execute_action(
        self, action: Dict[str, Any], context: Dict[str, Any], interface, message
    ) -> Dict[str, Any]:
        typ = action.get("type")
        payload = action.get("payload") or {}

        if typ == "propose_action":
            cmd = payload.get("command") or payload.get("cmd") or ""
            proposer = payload.get("proposer")
            # create activity log
            try:
                if callable(self._create_activity_log):
                    aid = await self._create_activity_log(
                        cmd, proposer=proposer, metadata=payload.get("metadata")
                    )
                else:
                    aid = None
            except Exception as e:
                aid = None
                log_warning(
                    f"[agent_core] propose_action create_activity_log failed: {e}"
                )

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

            return {"status": "proposed", "proposal_id": aid}

        elif typ == "approve_action":
            proposal_id = payload.get("proposal_id")
            command = payload.get("command") or ""
            approver = (
                (message or {}).get("sender_id") if isinstance(message, dict) else None
            )

            # mark approved
            try:
                if callable(self._update_activity_log):
                    await self._update_activity_log(
                        proposal_id, status="approved", approver=approver
                    )
            except Exception as e:
                log_warning(
                    f"[agent_core] approve_action update_activity_log failed: {e}"
                )

            # execute command
            output = None
            try:
                if callable(self._run_command):
                    output = await self._run_command(command)
                else:
                    import asyncio as _asyncio

                    proc = await _asyncio.create_subprocess_shell(
                        command,
                        stdout=_asyncio.subprocess.PIPE,
                        stderr=_asyncio.subprocess.STDOUT,
                    )
                    out, _ = await proc.communicate()
                    output = out.decode("utf-8", errors="replace") if out else ""
            except Exception as e:
                output = f"Error: {e}"

            # record exec
            try:
                if callable(self._insert_action_exec):
                    await self._insert_action_exec(
                        proposal_id, command, output=output, executor=approver
                    )
            except Exception as e:
                log_warning(
                    f"[agent_core] approve_action insert_action_exec failed: {e}"
                )

            # finalize
            try:
                if callable(self._update_activity_log):
                    await self._update_activity_log(
                        proposal_id, status="executed", output=output
                    )
            except Exception:
                pass

            return {"status": "executed", "proposal_id": proposal_id, "output": output}

        else:
            return {"status": "unknown_action"}


# Expose a convenient singleton manager
_agent_loop_manager = AgentLoopManager()


def get_agent_loop_manager() -> AgentLoopManager:
    return _agent_loop_manager
