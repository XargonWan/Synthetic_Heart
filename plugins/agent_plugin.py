# plugins/agent_plugin.py

import asyncio
import json
import os
import re
import shutil
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
    register_exposed_var(
        "AGENT_SHELL_ALLOW_HOST",
        label="Allow agent shell on host",
        default=False,
        value_type=bool,
        ui_type="bool",
        description=(
            "Allow the agent_run_shell action to run when Synth is NOT inside a "
            "container. Off by default: a shell on the host is a real "
            "machine-compromise risk. Only enable in trusted local dev."
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


def _is_in_container() -> bool:
    """Best-effort detection of whether Synth runs inside a container.

    Order of precedence:
    1. Explicit ``SYNTH_IN_CONTAINER`` env override (``1``/``true`` or ``0``/``false``).
    2. Presence of ``/.dockerenv`` (created by Docker) or ``/run/.containerenv`` (Podman).
    3. A ``docker``/``kubepods``/``containerd`` marker in ``/proc/1/cgroup``.

    Defaults to ``False`` (host) when it cannot tell, which is the safer choice:
    shell execution is only auto-permitted inside the disposable container.
    """
    override = os.getenv("SYNTH_IN_CONTAINER")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")

    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True

    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            cgroup = fh.read()
        if any(m in cgroup for m in ("docker", "kubepods", "containerd", "libpod")):
            return True
    except Exception:
        pass

    return False


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
            "agent_write_file",
            "agent_edit_file",
            "agent_search_files",
            "agent_run_shell",
            "spawn_drone",
            "resume_agent_task",
            "note_to_self",
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
            "agent_write_file": {
                "required_fields": ["path", "content"],
                "optional_fields": ["mode"],
                "security_level": "medium",
                "external_effects": ["filesystem"],
                "description": (
                    "Write a text file within the allowed agent filesystem roots. "
                    "Creates parent directories as needed. 'mode' may be 'overwrite' "
                    "(default) or 'append'. Use this to create/update text files "
                    "(e.g. notes, logs, generated documents) inside the sandbox."
                ),
            },
            "agent_edit_file": {
                "required_fields": ["path", "old_string", "new_string"],
                "optional_fields": ["expected_replacements"],
                "security_level": "medium",
                "external_effects": ["filesystem"],
                "description": (
                    "Edit an existing text file in place by replacing an exact literal "
                    "substring ('old_string') with 'new_string'. 'old_string' MUST match "
                    "the file content verbatim (including whitespace/indentation) and, by "
                    "default, must occur exactly once — include enough surrounding context "
                    "to make it unique. Set 'expected_replacements' to replace a known "
                    "number of occurrences. Use this for surgical edits to an existing file "
                    "instead of rewriting the whole file with agent_write_file."
                ),
            },
            "agent_search_files": {
                "required_fields": ["pattern"],
                "optional_fields": [
                    "path",
                    "regex",
                    "case_sensitive",
                    "glob",
                    "max_results",
                    "max_file_bytes",
                ],
                "description": (
                    "Search file contents within the allowed sandbox roots (a native grep). "
                    "'pattern' is plain text by default; set 'regex' true for a Python regex. "
                    "'path' scopes the search (default: first allowed root), 'glob' filters "
                    "filenames (e.g. '*.py'), 'case_sensitive' defaults to false. Returns "
                    "matching lines with file path and line number, bounded by 'max_results'."
                ),
            },
            "agent_run_shell": {
                "required_fields": ["command"],
                "optional_fields": ["cwd", "timeout"],
                "security_level": "high",
                "external_effects": ["shell"],
                "description": (
                    "Run a shell command and capture its stdout/stderr/exit code. "
                    "The working directory ('cwd') defaults to the first allowed "
                    "filesystem root and MUST stay inside the allowed roots. "
                    "For safety this action ONLY runs when Synth is executing inside "
                    "a container (the disposable runtime image); on a bare host it is "
                    "refused unless AGENT_SHELL_ALLOW_HOST is explicitly enabled. "
                    "'timeout' is capped in seconds. Use for build/test/git/system "
                    "commands you cannot express with the file actions."
                ),
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
            "note_to_self": {
                "required_fields": ["note"],
                "optional_fields": [],
                "description": (
                    "Record a PRIVATE internal thought/reasoning note for yourself "
                    "DURING an agent task. This is NEVER shown to the user — it is "
                    "your own scratchpad for planning, tracking progress, or noting "
                    "an intermediate observation. Use this instead of putting "
                    "internal monologue, task-log text, or 'thinking out loud' into "
                    "a user-facing message. To actually talk to the user, use a "
                    "message_* action instead."
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

    def _edit_file(self, payload: dict) -> dict:
        """Replace an exact literal substring in a sandboxed text file.

        Mirrors an editor's search/replace semantics: ``old_string`` must match
        the file content verbatim and, by default, occur exactly once. Set
        ``expected_replacements`` to replace a known number of occurrences.
        """
        raw_path = str(payload.get("path") or "").strip()
        old_string = payload.get("old_string")
        new_string = payload.get("new_string")

        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return {
                "status": "error",
                "reason": "old_string and new_string must be strings",
            }
        if old_string == "":
            return {"status": "error", "reason": "old_string must not be empty"}
        if old_string == new_string:
            return {
                "status": "error",
                "reason": "old_string and new_string are identical",
            }

        expected = _safe_int(
            payload.get("expected_replacements"), 1, min_value=1, max_value=10_000
        )

        safe_path, err = self._resolve_safe_path(raw_path)
        if err or safe_path is None:
            return {"status": "error", "reason": err or "invalid path"}
        if not safe_path.exists():
            return {"status": "error", "reason": "file not found"}
        if safe_path.is_dir():
            return {"status": "error", "reason": "path is a directory"}

        try:
            original = safe_path.read_text(encoding="utf-8")
        except Exception as exc:
            return {"status": "error", "reason": f"read failed: {exc}"}

        occurrences = original.count(old_string)
        if occurrences == 0:
            return {"status": "error", "reason": "old_string not found in file"}
        if occurrences != expected:
            return {
                "status": "error",
                "reason": (
                    f"old_string occurs {occurrences} time(s) but "
                    f"expected_replacements={expected}; add more context to make "
                    "the match unique or set expected_replacements accordingly"
                ),
            }

        updated = original.replace(old_string, new_string)
        if len(updated.encode("utf-8")) > 2_000_000:
            return {"status": "error", "reason": "resulting file too large (>2MB)"}

        try:
            safe_path.write_text(updated, encoding="utf-8")
        except Exception as exc:
            return {"status": "error", "reason": f"write failed: {exc}"}

        log_info(
            f"[agent] agent_edit_file replaced {occurrences} occurrence(s) in {safe_path}"
        )
        return {
            "status": "ok",
            "path": str(safe_path),
            "replacements": occurrences,
            "bytes_written": len(updated.encode("utf-8")),
        }

    def _search_files(self, payload: dict) -> dict:
        """Search file contents within the sandbox (a bounded native grep)."""
        pattern = payload.get("pattern")
        if not isinstance(pattern, str) or pattern == "":
            return {"status": "error", "reason": "pattern must be a non-empty string"}

        use_regex = bool(payload.get("regex", False))
        case_sensitive = bool(payload.get("case_sensitive", False))
        glob = payload.get("glob")
        glob_pat = str(glob).strip() if isinstance(glob, str) and glob.strip() else "*"
        max_results = _safe_int(
            payload.get("max_results"), 200, min_value=1, max_value=2000
        )
        max_file_bytes = _safe_int(
            payload.get("max_file_bytes"),
            2_000_000,
            min_value=1_000,
            max_value=20_000_000,
        )

        raw_path = str(payload.get("path") or ".")
        safe_path, err = self._resolve_safe_path(raw_path)
        if err or safe_path is None:
            return {"status": "error", "reason": err or "invalid path"}
        if not safe_path.exists():
            return {"status": "error", "reason": "path not found"}

        flags = 0 if case_sensitive else re.IGNORECASE
        if use_regex:
            try:
                matcher = re.compile(pattern, flags)
            except re.error as exc:
                return {"status": "error", "reason": f"invalid regex: {exc}"}

            def _matches(line: str) -> bool:
                return matcher.search(line) is not None
        else:
            needle = pattern if case_sensitive else pattern.lower()

            def _matches(line: str) -> bool:
                haystack = line if case_sensitive else line.lower()
                return needle in haystack

        if safe_path.is_file():
            candidates = [safe_path]
        else:
            candidates = sorted(p for p in safe_path.rglob(glob_pat) if p.is_file())

        matches: list[dict] = []
        files_scanned = 0
        truncated = False
        for fpath in candidates:
            if len(matches) >= max_results:
                truncated = True
                break
            try:
                if fpath.stat().st_size > max_file_bytes:
                    continue
            except Exception:
                continue
            files_scanned += 1
            try:
                with fpath.open("r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if _matches(line):
                            matches.append(
                                {
                                    "path": str(fpath),
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:1000],
                                }
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
            except Exception:
                continue

        return {
            "status": "ok",
            "path": str(safe_path),
            "files_scanned": files_scanned,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }

    async def _run_shell(self, payload: dict) -> dict:
        """Execute a shell command inside the sandbox.

        Security model: the command only runs when Synth executes inside a
        container (the disposable runtime image). On a bare host it is refused
        unless ``AGENT_SHELL_ALLOW_HOST`` is explicitly enabled, because a shell
        on the host is a real machine-compromise risk for a public persona.
        """
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"status": "error", "reason": "command must be a non-empty string"}

        in_container = _is_in_container()
        allow_host = bool(config_registry.get_var("AGENT_SHELL_ALLOW_HOST", False))
        if not in_container and not allow_host:
            log_warning(
                "[agent] agent_run_shell refused: not running in a container and "
                "AGENT_SHELL_ALLOW_HOST is disabled"
            )
            return {
                "status": "error",
                "reason": (
                    "shell execution is only allowed inside a container; "
                    "set AGENT_SHELL_ALLOW_HOST=true to override on a host"
                ),
            }

        # Resolve and confine the working directory to the allowed roots.
        raw_cwd = payload.get("cwd")
        if raw_cwd:
            safe_cwd, err = self._resolve_safe_path(str(raw_cwd))
            if err or safe_cwd is None:
                return {"status": "error", "reason": err or "invalid cwd"}
        else:
            roots = self._allowed_roots()
            if not roots:
                return {"status": "error", "reason": "no allowed roots configured"}
            safe_cwd = roots[0]
        if not safe_cwd.exists() or not safe_cwd.is_dir():
            return {
                "status": "error",
                "reason": "cwd does not exist or is not a directory",
            }

        timeout = _safe_int(payload.get("timeout"), 60, min_value=1, max_value=600)
        shell_exe = shutil.which("bash") or shutil.which("sh") or "/bin/sh"

        try:
            proc = await asyncio.create_subprocess_exec(
                shell_exe,
                "-c",
                command,
                cwd=str(safe_cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            return {"status": "error", "reason": f"failed to start shell: {exc}"}

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return {
                "status": "error",
                "reason": f"command timed out after {timeout}s",
                "cwd": str(safe_cwd),
            }

        max_out = 40_000
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        truncated = False
        if len(stdout) > max_out:
            stdout = stdout[:max_out] + "\n... (truncated)"
            truncated = True
        if len(stderr) > max_out:
            stderr = stderr[:max_out] + "\n... (truncated)"
            truncated = True

        exit_code = proc.returncode
        log_info(
            f"[agent] agent_run_shell exit={exit_code} cwd={safe_cwd} "
            f"in_container={in_container}"
        )
        return {
            "status": "ok" if exit_code == 0 else "error",
            "exit_code": exit_code,
            "cwd": str(safe_cwd),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        enabled = self.is_enabled()
        if enabled:
            description = (
                "You have agentic capabilities. Use the agent actions to inspect files, "
                "search file contents inside the sandbox (agent_search_files), "
                "write text files inside the allowed sandbox (agent_write_file), "
                "make surgical edits to an existing file (agent_edit_file), "
                "run shell commands inside the sandbox (agent_run_shell), "
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

        if action_type == "agent_write_file":
            raw_path = str(payload.get("path") or "").strip()
            content = payload.get("content")
            if not isinstance(content, str):
                return {"status": "error", "reason": "content must be a string"}
            if len(content) > 2_000_000:
                return {"status": "error", "reason": "content too large (>2MB)"}

            mode = str(payload.get("mode") or "overwrite").strip().lower()
            if mode not in ("overwrite", "append"):
                return {
                    "status": "error",
                    "reason": "mode must be 'overwrite' or 'append'",
                }

            safe_path, err = self._resolve_safe_path(raw_path)
            if err or safe_path is None:
                return {"status": "error", "reason": err or "invalid path"}
            if safe_path.is_dir():
                return {"status": "error", "reason": "path is a directory"}

            try:
                safe_path.parent.mkdir(parents=True, exist_ok=True)
                open_mode = "a" if mode == "append" else "w"
                with safe_path.open(open_mode, encoding="utf-8") as fh:
                    fh.write(content)
                written = len(content.encode("utf-8"))
                log_info(
                    f"[agent] agent_write_file wrote {written} bytes to {safe_path} (mode={mode})"
                )
                return {
                    "status": "ok",
                    "path": str(safe_path),
                    "bytes_written": written,
                    "mode": mode,
                }
            except Exception as exc:
                return {"status": "error", "reason": f"write failed: {exc}"}

        if action_type == "agent_edit_file":
            return self._edit_file(payload)

        if action_type == "agent_search_files":
            return self._search_files(payload)

        if action_type == "agent_run_shell":
            return await self._run_shell(payload)

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

        if action_type == "note_to_self":
            note = str(payload.get("note") or "").strip()
            if not note:
                return {"status": "error", "reason": "empty note"}
            preview = note if len(note) <= 300 else note[:300] + " ..."
            log_info(f"[agent] note_to_self: {preview}")
            return {"status": "ok", "recorded": True}

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
