#!/usr/bin/env python3
"""MCP server exposing Synthetic Heart log files to AI agents.

Provides structured access to all logs/* files so agents can search, tail,
and inspect runtime logs without reading raw files line by line.

Tools
-----
list_log_files    -- metadata for every active log file
search_logs       -- regex / text search with level + time filters
tail_log          -- last N lines from a named log (stitches across rotations)
get_recent_errors -- shortcut: recent ERROR / WARNING entries across all logs

Rotation behaviour
------------------
Logs rotate at 2000 lines (fast in debug mode).  Every tool that reads log
content transparently spans the active file PLUS the N most-recent rotated
backups (controlled by the `lookback_files` parameter, default 3).  This
means an agent asking for the last 50 lines or searching for an error will
see content across multiple rotations, not just whatever landed in the tiny
current file.

Usage (stdio transport, registered in .mcp.json)
------
    uv run python mcp_servers/synth_logs.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_LOG_DIR = Path(os.getenv("LOG_DIR", str(_SCRIPT_DIR.parent / "logs")))

_LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
}

# Standard log line format produced by logging_utils.py
# [2026-04-12 23:45:18] [INFO] [file.py:42] message
_STD_LINE_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[(\w+)\] \[(.+?)\] (.*)$"
)

# cortex_api.log and live_api.log use a section/banner format with large LLM
# payloads.  Lines from these files are truncated to avoid flooding context.
_LARGE_PAYLOAD_FILES: frozenset[str] = frozenset({"cortex_api", "live_api"})
_PAYLOAD_TRUNCATE = 400  # characters per line

mcp = FastMCP("synth-logs")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _active_stems() -> list[str]:
    """Return the logical names of all active (non-rotated) log files."""
    if not _LOG_DIR.exists():
        return []
    stems: list[str] = []
    for p in sorted(_LOG_DIR.iterdir()):
        if not p.is_file() or p.suffix != ".log":
            continue
        if re.search(r"\.\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", p.name):
            continue
        if re.search(r"\.log\.\d+$", p.name):
            continue
        stems.append(p.stem)
    return stems


def _rotated_files(stem: str) -> list[Path]:
    """Return rotated backups for *stem*, sorted newest-first by mtime."""
    if not _LOG_DIR.exists():
        return []
    candidates: list[Path] = []
    for p in _LOG_DIR.iterdir():
        if not p.is_file():
            continue
        name = p.name
        # Timestamped rotation: synth.2026-04-12_22-45-18.log
        if re.match(
            rf"^{re.escape(stem)}\.\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}.*\.log$",
            name,
        ):
            candidates.append(p)
        # Numbered rotation: cortex_api.log.1, cortex_api.log.2
        elif re.match(rf"^{re.escape(stem)}\.log\.\d+$", name):
            candidates.append(p)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates


def _files_for_log(stem: str, lookback: int) -> list[Path]:
    """Files to read for *stem*, newest-first: active file + up to *lookback* rotated.

    Newest-first so search results surface the most recent activity first.
    """
    result: list[Path] = []
    active = _LOG_DIR / f"{stem}.log"
    if active.exists():
        result.append(active)
    if lookback > 0:
        result.extend(_rotated_files(stem)[:lookback])
    return result


def _parse_std_ts(ts_str: str) -> datetime | None:
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _tail_file(path: Path, n_lines: int) -> list[str]:
    """Efficiently read the last *n_lines* from *path* without loading it all."""
    chunk = 8 * 1024
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            pos = fh.tell()
            buf = b""
            while pos > 0:
                read = min(chunk, pos)
                pos -= read
                fh.seek(pos)
                buf = fh.read(read) + buf
                lines = buf.decode("utf-8", errors="replace").splitlines()
                if len(lines) > n_lines:
                    return lines[-n_lines:]
            return buf.decode("utf-8", errors="replace").splitlines()[-n_lines:]
    except OSError:
        return []


def _count_rotated(stem: str) -> int:
    return len(_rotated_files(stem))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_log_files() -> str:
    """List all active log files with size, last-modified time, and rotated backup count.

    Use the returned name (e.g. 'synth', 'cortex_api') in other tools.
    Searches and tails automatically span the active file plus recent backups
    (see the lookback_files parameter on search_logs and tail_log).
    """
    stems = _active_stems()
    if not stems:
        return f"No log files found in {_LOG_DIR}"

    rows: list[str] = [
        f"Log directory: {_LOG_DIR}\n",
        f"{'NAME':<22} {'SIZE':>10}  {'LAST MODIFIED':<24} BACKUPS",
        "-" * 72,
    ]
    for stem in stems:
        path = _LOG_DIR / f"{stem}.log"
        try:
            stat = path.stat()
            size_kb = stat.st_size / 1024
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
        except OSError:
            size_kb = 0.0
            mtime = "unknown"
        backups = _count_rotated(stem)
        rows.append(f"{stem:<22} {size_kb:>8.1f} KB  {mtime:<24} {backups}")

    rows.append("")
    rows.append(
        "Note: search_logs and tail_log span the active file + recent backups by default."
    )
    return "\n".join(rows)


@mcp.tool()
def search_logs(
    query: str,
    log_files: Optional[list[str]] = None,
    level: Optional[str] = None,
    since_minutes: Optional[int] = None,
    max_results: int = 50,
    context_lines: int = 0,
    lookback_files: int = 3,
) -> str:
    """Search log files for a text or regex pattern, spanning recent rotations.

    Args:
        query: Plain text or Python regex to search for (case-insensitive).
        log_files: Restrict search to these log names, e.g. ["synth", "webui"].
                   Omit to search all logs.
        level: Minimum level filter for standard-format lines:
               DEBUG | INFO | WARNING | ERROR.
               Non-standard logs (cortex_api, live_api) are skipped when a
               level filter is active since their level is not parseable.
        since_minutes: Only return entries from the last N minutes.
                       Applies only to standard-format lines.
        max_results: Hard cap on returned matches (default 50, max 200).
        context_lines: Extra surrounding lines to include per match (default 0).
        lookback_files: How many rotated backups to include per log in addition
                        to the active file (default 3).  Increase if you need
                        more history -- logs rotate at 2000 lines in debug mode.

    Returns:
        Formatted hit list.  Each match shows  <filename>:<lineno>  <content>
        so you can tell whether a result is from the current file or a backup.
    """
    max_results = min(max_results, 200)

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        return f"Invalid regex pattern: {exc}"

    now = datetime.now(tz=timezone.utc)
    since_dt = (now - timedelta(minutes=since_minutes)) if since_minutes else None
    min_level_val = _LEVEL_ORDER.get((level or "").upper(), 0)
    filtering = min_level_val > 0 or since_dt is not None

    stems = log_files if log_files else _active_stems()

    results: list[str] = []
    total = 0

    for stem in stems:
        if total >= max_results:
            break
        is_large = stem in _LARGE_PAYLOAD_FILES
        files = _files_for_log(stem, lookback_files)

        for path in files:
            if total >= max_results:
                break
            label = path.name  # e.g. "synth.log" or "synth.2026-04-12_22-45-18.log"

            try:
                all_lines = path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError as exc:
                results.append(f"[{label}] read error: {exc}")
                continue

            for i, line in enumerate(all_lines):
                if total >= max_results:
                    break
                if not pattern.search(line):
                    continue

                m = _STD_LINE_RE.match(line)
                if m:
                    ts_str, lvl, _loc, _msg = m.groups()
                    if (
                        min_level_val > 0
                        and _LEVEL_ORDER.get(lvl.upper(), 0) < min_level_val
                    ):
                        continue
                    if since_dt:
                        ts = _parse_std_ts(ts_str)
                        if ts and ts < since_dt:
                            continue
                elif filtering:
                    # Cannot determine level/time for non-standard lines; skip.
                    continue

                def _fmt(ln: str, _large: bool = is_large) -> str:
                    if _large and len(ln) > _PAYLOAD_TRUNCATE:
                        return ln[:_PAYLOAD_TRUNCATE] + " ...[truncated]"
                    return ln

                block: list[str] = []
                for k in range(max(0, i - context_lines), i):
                    block.append(f"  {label}:{k + 1}  {_fmt(all_lines[k])}")
                block.append(f">> {label}:{i + 1}  {_fmt(line)}")
                for k in range(i + 1, min(len(all_lines), i + context_lines + 1)):
                    block.append(f"  {label}:{k + 1}  {_fmt(all_lines[k])}")

                results.append("\n".join(block))
                total += 1

    if not results:
        filters = []
        if level:
            filters.append(f"level>={level}")
        if since_minutes:
            filters.append(f"last {since_minutes}min")
        filter_str = f" with filters ({', '.join(filters)})" if filters else ""
        return f"No matches for '{query}'{filter_str}"

    header = f"{total} match(es)"
    if total >= max_results:
        header += (
            f" (capped at {max_results}"
            " - narrow with level/since_minutes/log_files or increase max_results)"
        )
    return header + "\n\n" + "\n\n".join(results)


@mcp.tool()
def tail_log(
    log_file: str,
    lines: int = 50,
    level: Optional[str] = None,
    lookback_files: int = 2,
) -> str:
    """Get the last N lines from a named log, stitching across recent rotations.

    Because logs rotate at 2000 lines, the active file may have very few lines.
    This tool transparently reads across the active file and recent backups so
    you always get the requested number of lines regardless of rotation state.

    Args:
        log_file: Log stem without extension: 'synth', 'cortex_api', 'webui', etc.
                  Use list_log_files() to see what is available.
        lines: Number of lines to return (default 50, max 500).
        level: Optional minimum level filter (DEBUG/INFO/WARNING/ERROR).
               Filters standard-format lines; section-format lines are kept as-is.
        lookback_files: Rotated backups to include in addition to the active file
                        (default 2).  Increase if you need more history.
    """
    lines = min(max(1, lines), 500)

    files = _files_for_log(log_file, lookback_files)
    if not files:
        available = ", ".join(_active_stems())
        return f"Log '{log_file}' not found. Available: {available}"

    is_large = log_file in _LARGE_PAYLOAD_FILES

    # Files are newest-first; reverse to chronological order so we can
    # concatenate lines from oldest->newest and then take the tail.
    files_chrono = list(reversed(files))
    combined: list[str] = []
    for path in files_chrono:
        # Over-read from each file; we trim to `lines` at the end.
        chunk = _tail_file(path, lines * (4 if level else 1))
        combined.extend(chunk)

    if level:
        min_val = _LEVEL_ORDER.get(level.upper(), 0)
        filtered: list[str] = []
        for ln in combined:
            m = _STD_LINE_RE.match(ln)
            if m:
                if _LEVEL_ORDER.get(m.group(2).upper(), 0) >= min_val:
                    filtered.append(ln)
            else:
                filtered.append(ln)  # keep section headers / banners
        combined = filtered

    raw = combined[-lines:]

    if is_large:
        raw = [
            (
                ln[:_PAYLOAD_TRUNCATE] + " ...[truncated]"
                if len(ln) > _PAYLOAD_TRUNCATE
                else ln
            )
            for ln in raw
        ]

    # Show which files contributed so the agent has provenance
    file_names = " + ".join(p.name for p in reversed(files))
    return f"Last {len(raw)} lines from: {file_names}\n\n" + "\n".join(raw)


@mcp.tool()
def get_recent_errors(
    minutes: int = 60,
    include_warnings: bool = True,
) -> str:
    """Get recent ERROR (and optionally WARNING) entries across all standard logs.

    Spans rotated backups automatically -- safe to call even right after a
    log rotation.  Large-payload logs (cortex_api, live_api) are excluded
    because their format cannot be level-filtered.

    Args:
        minutes: Look back this many minutes (default 60).
        include_warnings: Also return WARNING lines, not just ERROR (default True).
    """
    min_level = "WARNING" if include_warnings else "ERROR"
    standard_stems = [s for s in _active_stems() if s not in _LARGE_PAYLOAD_FILES]
    return search_logs(
        query=r".",
        log_files=standard_stems,
        level=min_level,
        since_minutes=minutes,
        max_results=100,
        lookback_files=5,  # generous lookback since this is a diagnostic shortcut
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
