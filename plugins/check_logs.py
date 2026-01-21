"""Check Logs Plugin

Provides two actions:
- get_logs: return the last N lines of a log file (default 30, default file synth.log)
- search_logs: search for keywords or regex across logs and return matching lines

Allowed log filenames (no path traversal allowed):
- synth.log
- prompt_cycle.log
- synth.log.1 (synth.log.2, synth.log.3)
- webui.log
- selkies.log

The plugin sends results back to the invoking interface via the provided bot.
"""

from __future__ import annotations

import os
import re
from typing import List, Dict, Any

from core.core_initializer import register_plugin
from core.logging_utils import log_info, log_error


LOG_DIR = os.getenv("SYNTH_LOG_DIR", "/app/logs")
ALLOWED_FILES = [
    "synth.log",
    "prompt_cycle.log",
    "webui.log",
    "selkies.log",
]
# Allow rotated names synth.log.1 .. synth.log.3
for i in range(1, 4):
    ALLOWED_FILES.append(f"synth.log.{i}")


def _resolve_log_path(filename: str) -> str:
    # Security: only allow filenames in ALLOWED_FILES
    if filename not in ALLOWED_FILES:
        raise ValueError(f"Unsupported log file: {filename}")
    # Join and normalize
    path = os.path.normpath(os.path.join(LOG_DIR, filename))
    # Ensure it resides in LOG_DIR
    if not path.startswith(os.path.normpath(LOG_DIR) + os.sep) and path != os.path.normpath(LOG_DIR):
        raise ValueError("Invalid log path")
    return path


def _tail_lines(path: str, lines: int) -> List[str]:
    # Efficient tail implementation by reading from end
    if lines <= 0:
        return []
    try:
        with open(path, "rb") as f:
            avg_line_length = 200
            to_read = lines * avg_line_length
            try:
                f.seek(-to_read, os.SEEK_END)
            except OSError:
                f.seek(0)
            data = f.read().decode(errors="replace")
    except Exception as e:
        raise
    all_lines = data.splitlines()
    if len(all_lines) <= lines:
        return all_lines
    return all_lines[-lines:]


def _search_in_lines(lines: List[str], queries: List[str], regex: bool, context: int = 0) -> List[str]:
    matches = []
    if regex:
        patterns = [re.compile(q) for q in queries]
        for i, line in enumerate(lines):
            for p in patterns:
                if p.search(line):
                    # include context
                    start = max(0, i - context)
                    end = min(len(lines), i + context + 1)
                    matches.extend(lines[start:end])
                    break
    else:
        lowered = [l.lower() for l in lines]
        qlower = [q.lower() for q in queries]
        for i, l in enumerate(lowered):
            for q in qlower:
                if q in l:
                    start = max(0, i - context)
                    end = min(len(lines), i + context + 1)
                    matches.extend(lines[start:end])
                    break
    # Deduplicate while keeping order
    seen = set()
    out = []
    for l in matches:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out


class CheckLogsPlugin:
    display_name = "Check Logs"

    def __init__(self):
        register_plugin("check_logs", self)
        log_info("[check_logs] plugin initialized and registered")

    def get_supported_action_types(self):
        return ["get_logs", "search_logs"]

    def get_supported_actions(self) -> dict:
        return {
            "get_logs": {
                "description": "Return the last N lines from a log file",
                "required_fields": [],
                "optional_fields": ["file", "lines"],
            },

            "search_logs": {
                "description": "Search logs for keywords or regular expressions",
                "required_fields": ["queries"],
                "optional_fields": ["file", "lines", "regex", "context"],
            },
        }

    def get_prompt_instructions(self, action_name: str) -> dict:
        if action_name == "get_logs":
            return {
                "description": "Return the last N lines from a log file",
                "payload": {"file": "synth.log", "lines": 30},
            }
        if action_name == "search_logs":
            return {
                "description": "Search logs for keywords or regular expressions",
                "payload": {"file": "synth.log", "queries": ["error", "exception"], "regex": False, "lines": 500},
            }
        return {}

    def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload") or {}

        file = payload.get("file", "synth.log")
        # Clamp lines to reasonable bounds to avoid excessive memory use
        lines = min(max(1, int(payload.get("lines", 30))), 5000)

        try:
            path = _resolve_log_path(file)
        except Exception as e:
            log_error(f"[check_logs] invalid file: {e}")
            try:
                bot.send_message(original_message.chat_id, f"Invalid log file: {file}")
            except Exception:
                pass
            return

        if action_type == "get_logs":
            try:
                tail = _tail_lines(path, lines)
                if not tail:
                    body = "(file is empty or not accessible)"
                else:
                    body = "\n".join(tail[-lines:])
                # Truncate if too big
                if len(body) > 19000:
                    body = body[-19000:]
                    body = "... (truncated)\n" + body
                msg = f"Last {lines} lines from {file}:\n```\n{body}\n```"
                bot.send_message(original_message.chat_id, msg)
            except Exception as e:
                log_error(f"[check_logs] failed to read file {path}: {e}")
                try:
                    bot.send_message(original_message.chat_id, f"Failed to read log file: {e}")
                except Exception:
                    pass

        elif action_type == "search_logs":
            queries = payload.get("queries")
            if not queries:
                try:
                    bot.send_message(original_message.chat_id, "No queries provided for search_logs")
                except Exception:
                    pass
                return
            if isinstance(queries, str):
                queries = [queries]
            regex = bool(payload.get("regex", False))
            context_lines = int(payload.get("context", 0))

            try:
                # read up to 'lines' from file tail for searching (to be performant)
                raw = _tail_lines(path, max(lines, 1000))
                matches = _search_in_lines(raw, queries, regex, context_lines)
                if not matches:
                    bot.send_message(original_message.chat_id, "No matches found")
                    return
                body = "\n".join(matches)
                if len(body) > 19000:
                    body = body[:19000]
                    body = body + "\n... (truncated)"
                msg = f"Search results in {file} for {queries}:\n```\n{body}\n```"
                bot.send_message(original_message.chat_id, msg)
            except re.error as e:
                bot.send_message(original_message.chat_id, f"Invalid regular expression: {e}")
            except Exception as e:
                log_error(f"[check_logs] search failed: {e}")
                try:
                    bot.send_message(original_message.chat_id, f"Search failed: {e}")
                except Exception:
                    pass


PLUGIN_CLASS = CheckLogsPlugin
