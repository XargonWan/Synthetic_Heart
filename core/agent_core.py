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
        default=5,
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
except Exception:
    pass

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

            if not tool_calls:
                # Only synth message actions this iteration (no real tools left):
                # Synth has produced its reply, so end the turn and let the
                # normal delivery path send ``final_text`` to the interface.
                if message_calls and final_text.strip():
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

                # No more tool calls -> model is done; keep its text as final.
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
        tools_block = "\n".join(tool_lines) if tool_lines else "- (no tools registered)"

        system_text = (
            "You are Synth operating in agentic mode. Achieve the goal using "
            "the available tools. When you need a tool, respond ONLY with the "
            "tool-call JSON actions. When the goal is complete, respond with a "
            "final natural-language answer and no tool calls. Use only tool names "
            "from the AVAILABLE TOOLS block.\n"
            "Diary discipline: this is a single agentic task, not many separate "
            "moments. Do NOT write a diary entry on every iteration. At most, "
            "record one diary entry when you begin the task and one when it is "
            "finished. During the intermediate working iterations do NOT call any "
            "diary tool (create_personal_diary_entry / update_diary_entry) — just "
            "use the tools needed to make progress. If the prior observations "
            "already show a diary entry was written for this task, do not write "
            "another one until the task is complete."
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


# Expose a convenient singleton manager
_agent_loop_manager = AgentLoopManager()


def get_agent_loop_manager() -> AgentLoopManager:
    return _agent_loop_manager
