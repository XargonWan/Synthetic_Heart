"""
Synthetic Heart Log Query MCP Server
Exposes log query tools to AI agents via MCP stdio transport.
Run with: uv run synth_log_mcp.py
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

# ── Config ────────────────────────────────────────────────────────────────────

LOGS_DIR = Path(r"D:\dev\10\synthetic_heart\logs")

# Log families: (glob pattern, sort key function)
# Sort key should return something sortable oldest→newest
LOG_FAMILIES = {
    "synth": {
        "current": "synth.log",
        "glob": "synth.*.log",
        "rotation": "datestamp",  # synth.2026-04-11_09-11-27.log
    },
    "cortex_api": {
        "current": "cortex_api.log",
        "glob": "cortex_api.log.*",
        "rotation": "numbered",
    },
    "webui": {
        "current": "webui.log",
        "glob": "webui.*.log",
        "rotation": "datestamp",
    },
    "live_api": {
        "current": "live_api.log",
        "glob": "live_api.log.*",
        "rotation": "numbered",
    },
    "memoria": {
        "current": "memoria.log",
        "glob": None,
        "rotation": None,
    },
    "gemini_extract": {
        "current": "gemini_extract.log",
        "glob": "gemini*.log*",
        "rotation": "datestamp",
    },
}

# Regex for standard log lines:
# [2026-04-11 10:32:18] [DEBUG] [action_parser.py:646] [action_parser] message
LOG_RE = re.compile(
    r"^\[(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+"
    r"\[(?P<level>\w+)\]\s+"
    r"\[(?P<file>[^\]]+)\]\s+"
    r"\[(?P<component>[^\]]+)\]\s+"
    r"(?P<message>.+)$"
)

mcp = FastMCP("synth-logs")


# ── Helpers ───────────────────────────────────────────────────────────────────


def get_log_files(family: Optional[str] = None) -> list[Path]:
    """
    Return all log files for a family (or all families), sorted oldest→newest.
    Current file is always last (most recent).
    """
    if not LOGS_DIR.exists():
        return []

    families = [family] if family else list(LOG_FAMILIES.keys())
    all_files: list[tuple[float, Path]] = []

    for fam in families:
        if fam not in LOG_FAMILIES:
            continue
        cfg = LOG_FAMILIES[fam]

        # Rotated files
        if cfg["glob"]:
            for p in LOGS_DIR.glob(cfg["glob"]):
                if p.suffix in (".wav",):
                    continue
                all_files.append((p.stat().st_mtime, p))

        # Current file
        cur = LOGS_DIR / cfg["current"]
        if cur.exists():
            all_files.append((cur.stat().st_mtime, cur))

    # Sort oldest first so we read history → present
    all_files.sort(key=lambda x: x[0])
    return [p for _, p in all_files]


def parse_line(line: str) -> Optional[dict]:
    """Parse a standard log line into a dict, or return None if unparseable."""
    m = LOG_RE.match(line.rstrip())
    if not m:
        return None
    return m.groupdict()


def read_lines_from_files(files: list[Path]) -> list[dict]:
    """Read and parse all lines from a list of log files."""
    parsed = []
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                entry = parse_line(line)
                if entry:
                    entry["source_file"] = f.name
                    parsed.append(entry)
        except Exception:
            continue
    return parsed


def format_entry(e: dict) -> str:
    return f"[{e['timestamp']}] [{e['level']}] [{e['component']}] {e['message']}"


def load_jsonl(path: Path, last_n: int = 50) -> list[dict]:
    """Read last N lines from a .jsonl file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        results = []
        for line in lines[-last_n:]:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except Exception:
                    results.append({"raw": line})
        return results
    except Exception as ex:
        return [{"error": str(ex)}]


# ── MCP Tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
def query_logs(
    level: Optional[str] = None,
    component: Optional[str] = None,
    keyword: Optional[str] = None,
    family: Optional[str] = None,
    last_n: int = 50,
) -> str:
    """
    Search Synthetic Heart logs with optional filters.

    Args:
        level: Filter by log level. One of: DEBUG, INFO, WARNING, ERROR. Case-insensitive.
        component: Filter by component name, e.g. 'message_chain', 'action_parser', 'transport'.
        keyword: Filter by keyword anywhere in the message. Case-insensitive.
        family: Restrict to a log family: synth, cortex_api, webui, live_api, memoria, gemini_extract.
                If omitted, searches all families.
        last_n: Return at most this many matching lines (most recent). Default 50.

    Returns:
        Matching log lines as formatted text, or a message if none found.
    """
    files = get_log_files(family)
    entries = read_lines_from_files(files)

    # Apply filters
    if level:
        lvl = level.upper()
        entries = [e for e in entries if e["level"] == lvl]
    if component:
        comp = component.lower()
        entries = [e for e in entries if comp in e["component"].lower()]
    if keyword:
        kw = keyword.lower()
        entries = [e for e in entries if kw in e["message"].lower()]

    if not entries:
        return "No matching log entries found."

    # Return last N
    entries = entries[-last_n:]
    lines = [format_entry(e) for e in entries]
    return f"Found {len(lines)} entries (showing last {last_n}):\n\n" + "\n".join(lines)


@mcp.tool()
def get_errors(
    family: Optional[str] = None,
    last_n: int = 30,
) -> str:
    """
    Get recent ERROR and WARNING log entries across all (or one) log family.

    Args:
        family: Restrict to a log family. If omitted, searches all.
        last_n: Max number of entries to return. Default 30.

    Returns:
        Recent errors and warnings, most recent last.
    """
    files = get_log_files(family)
    entries = read_lines_from_files(files)
    entries = [e for e in entries if e["level"] in ("ERROR", "WARNING")]

    if not entries:
        return "No errors or warnings found."

    entries = entries[-last_n:]
    lines = [f"[{e['source_file']}] {format_entry(e)}" for e in entries]
    return f"{len(lines)} errors/warnings (showing last {last_n}):\n\n" + "\n".join(
        lines
    )


@mcp.tool()
def get_correction_loops(last_n: int = 20) -> str:
    """
    Find LLM correction loop events and JSON parse failures in synth logs.
    These indicate the LLM returned malformed output that triggered the corrector.

    Args:
        last_n: Max entries to return. Default 20.

    Returns:
        Correction loop and fallback events with context.
    """
    files = get_log_files("synth")
    entries = read_lines_from_files(files)

    keywords = (
        "correction loop",
        "corrector",
        "fallback",
        "non-json",
        "llm failure",
        "llm_failed",
    )
    matches = [e for e in entries if any(kw in e["message"].lower() for kw in keywords)]

    if not matches:
        return "No correction loop events found."

    matches = matches[-last_n:]
    lines = [format_entry(e) for e in matches]
    return f"{len(lines)} correction/fallback events:\n\n" + "\n".join(lines)


@mcp.tool()
def get_component_logs(
    component: str,
    last_n: int = 50,
    family: Optional[str] = None,
) -> str:
    """
    Get all recent log lines from a specific component/module.

    Args:
        component: Component name to filter on, e.g. 'message_chain', 'transport', 'db',
                   'action_parser', 'persona_manager', 'chat_context_manager'.
        last_n: Max lines to return. Default 50.
        family: Restrict to a log family. If omitted, searches all.

    Returns:
        Log lines from the specified component.
    """
    files = get_log_files(family)
    entries = read_lines_from_files(files)
    comp = component.lower()
    matches = [e for e in entries if comp in e["component"].lower()]

    if not matches:
        return f"No log entries found for component '{component}'."

    matches = matches[-last_n:]
    lines = [format_entry(e) for e in matches]
    return f"{len(lines)} entries for '{component}':\n\n" + "\n".join(lines)


@mcp.tool()
def get_output_failures(last_n: int = 20) -> str:
    """
    Find failed LLM output events: send failures, transport errors, unsupported action types.

    Args:
        last_n: Max entries to return. Default 20.

    Returns:
        Output failure events.
    """
    files = get_log_files("synth")
    entries = read_lines_from_files(files)

    keywords = (
        "llm failure",
        "llm_failed",
        "unsupported action",
        "sending fallback",
        "output failure",
        "failed to send",
        "send_message",
        "correction loop",
    )
    matches = [
        e
        for e in entries
        if e["level"] in ("ERROR", "WARNING")
        and any(kw in e["message"].lower() for kw in keywords)
    ]

    if not matches:
        return "No output failure events found."

    matches = matches[-last_n:]
    lines = [format_entry(e) for e in matches]
    return f"{len(lines)} output failure events:\n\n" + "\n".join(lines)


@mcp.tool()
def get_fallback_actions(last_n: int = 20) -> str:
    """
    Read the grillo_action_execs_fallback.jsonl file — records of fallback action executions.

    Args:
        last_n: Number of most recent fallback records to return. Default 20.

    Returns:
        Formatted fallback action records.
    """
    jsonl_path = LOGS_DIR / "grillo_action_execs_fallback.jsonl"
    if not jsonl_path.exists():
        return "grillo_action_execs_fallback.jsonl not found."

    records = load_jsonl(jsonl_path, last_n)
    if not records:
        return "No fallback action records found."

    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    return f"{len(lines)} fallback action records:\n\n" + "\n".join(lines)


@mcp.tool()
def list_log_files() -> str:
    """
    List all known log files with their sizes and last-modified times.
    Useful for orientation before querying.

    Returns:
        Table of log files.
    """
    if not LOGS_DIR.exists():
        return f"Log directory not found: {LOGS_DIR}"

    rows = []
    for p in sorted(LOGS_DIR.iterdir()):
        if p.suffix in (".wav",) or p.is_dir():
            continue
        try:
            stat = p.stat()
            size_kb = stat.st_size / 1024
            mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            rows.append(f"{p.name:<55} {size_kb:>8.1f} KB   {mtime}")
        except Exception:
            continue

    if not rows:
        return "No log files found."

    header = f"{'File':<55} {'Size':>10}   {'Modified'}"
    sep = "-" * 80
    return "\n".join([header, sep] + rows)


@mcp.tool()
def get_recent_activity(minutes: int = 10, family: Optional[str] = None) -> str:
    """
    Get all log entries from the last N minutes.

    Args:
        minutes: How many minutes back to look. Default 10.
        family: Restrict to a log family. If omitted, searches all.

    Returns:
        Recent log entries across all levels.
    """
    from datetime import timedelta

    files = get_log_files(family)
    entries = read_lines_from_files(files)

    cutoff = datetime.now() - timedelta(minutes=minutes)
    matches = []
    for e in entries:
        try:
            ts = datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S")
            if ts >= cutoff:
                matches.append(e)
        except Exception:
            continue

    if not matches:
        return f"No log entries in the last {minutes} minutes."

    lines = [format_entry(e) for e in matches]
    return f"{len(lines)} entries in last {minutes} min:\n\n" + "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
