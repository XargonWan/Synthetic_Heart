# plugins/agent_plugin.py

import json
import os
from pathlib import Path
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
        self._enabled = bool(config_registry.get_var("AGENT_ENABLED", True))

        log_info(f"[agent] Initialized. enabled={self._enabled}")

    def _refresh_runtime_settings(self) -> None:
        """Refresh config values so WebUI toggles apply at runtime."""
        try:
            self._enabled = bool(config_registry.get_var("AGENT_ENABLED", True))
        except Exception:
            # Keep the last known settings if config reads fail transiently.
            pass

    def is_enabled(self) -> bool:
        """Only expose agent actions when the agent is toggled on.

        Without this, ``core_initializer`` defaults the plugin to enabled and
        injects the agent tools (``spawn_drone`` / ``agent_read_file`` /
        ``agent_list_files``) into every prompt even when ``AGENT_ENABLED`` is
        off — bloating the tool block for small local LLMs.
        """
        self._refresh_runtime_settings()
        return bool(self._enabled)

    def get_supported_action_types(self) -> list[str]:
        return [
            "agent_list_files",
            "agent_read_file",
            "spawn_drone",
            "resume_agent_task",
        ]

    def get_supported_actions(self) -> Dict[str, Any]:
        return {
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
            "resume_agent_task": {
                "required_fields": ["task_id"],
                "optional_fields": [],
                "security_level": "medium",
                "external_effects": ["agent_task"],
                "description": (
                    "Resume a previously paused agent task by its numeric id, continuing "
                    "it where it left off (with a fresh iteration budget) instead of "
                    "starting a brand-new task. Use this whenever the user asks to continue, "
                    "resume, or keep working on a specific existing task and refers to it by "
                    "its number (e.g. 'continue task 37'). Provide the numeric 'task_id'. The "
                    "task must currently be paused/pending; you can only resume a task that "
                    "is waiting to be continued."
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
                "You have agentic capabilities. Use the agent actions to inspect files "
                "or delegate a focused sub-task to a Drone. "
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

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {})

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

        if action_type == "resume_agent_task":
            raw_id = payload.get("task_id")
            try:
                task_id = int(raw_id)
            except (TypeError, ValueError):
                return {"status": "error", "reason": "invalid or missing task_id"}
            if task_id <= 0:
                return {"status": "error", "reason": "invalid task_id"}

            from core.agent_core import get_agent_loop_manager

            manager = get_agent_loop_manager()
            resumable = await manager.find_task_by_id(task_id)
            if not resumable:
                return {
                    "status": "error",
                    "reason": f"task {task_id} not found or not pending",
                }

            ctx = context or {}
            prior_observations = list(resumable.get("prior_observations") or [])
            user_goal = str(ctx.get("original_text") or ctx.get("goal") or "").strip()
            if user_goal:
                prior_observations.append(
                    {"iteration": None, "role": "user", "content": user_goal}
                )
            try:
                result = await manager.run_agentic_turn(
                    goal=resumable.get("goal") or "",
                    engine=resumable.get("engine"),
                    context=ctx,
                    original_message=original_message,
                    task_id=resumable.get("task_id"),
                    prior_observations=prior_observations,
                )
            except Exception as exc:
                log_error(f"[agent] resume_agent_task failed: {exc}")
                return {"ok": False, "error": f"resume_failed: {exc}"}

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
