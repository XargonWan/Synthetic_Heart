#!/usr/bin/env python3
"""MCP server exposing Synthetic Heart's persisted LLM failure log to AI agents.

The WebUI "Logs" section surfaces a paginated table backed by the
``llm_failure_log`` DB table (see core/llm_failure_log.py and the WebUI
``get_log_failures`` endpoint in core/webui.py). This server exposes the SAME
data to agents so a diagnosing agent can cross-reference structured failure
entries with the raw runtime logs served by ``synth_logs``.

Why a dedicated server instead of a synth_db query?
--------------------------------------------------
``synth_db`` can already run arbitrary SELECTs, but an agent has to know the
schema, remember the column list, and format the output every time. This server
gives a purpose-built, low-friction view of the failure log with the same
filters the WebUI offers (search / failure_code / stage / recency), plus a
compact aggregate summary so an agent can see "what is failing right now" in a
single call.

DB access is delegated to the shared helpers in ``synth_db`` (target
resolution, host remapping, connection, RealDict rows) so there is exactly one
place that knows how to reach the runtime database.

Tools
-----
list_llm_failures    -- recent failure entries with filters (search/code/stage)
get_llm_failure      -- full detail (incl. metadata JSON) for one entry by id
llm_failure_summary  -- aggregate counts by failure_code / stage / interface

Usage (stdio transport, registered in .mcp.json and config/synth_mcp.json)
------
    uv run python mcp_servers/synth_llm_failures.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

# Ensure sibling mcp_servers modules are importable regardless of how the
# server is launched (script dir is normally on sys.path, but be explicit).
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Reuse the DB plumbing (target resolution, host remap, connect, row mapping)
# from synth_db so there is a single source of truth for reaching the runtime DB.
from synth_db import (  # noqa: E402
    _connect,
    _resolve_target,
    _rows_to_dicts,
    _target_summary,
)

mcp = FastMCP("synth-llm-failures")

# Columns of the llm_failure_log table (see core/llm_failure_log.py).
_COLUMNS = (
    "id",
    "failure_code",
    "stage",
    "reason",
    "interface_path",
    "chat_id",
    "thread_id",
    "engine",
    "model",
    "message_id",
    "content_preview",
    "metadata",
    "created_at",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fmt_ts(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value) if value is not None else "?"


def _parse_metadata(value: Any) -> Any:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _query(sql: str, params: list[Any], target: str | None) -> list[dict[str, Any]]:
    conn = _connect(target)
    with conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return _rows_to_dicts(rows)


def _fmt_entry_line(entry: dict[str, Any]) -> str:
    reason = str(entry.get("reason") or "").replace("\n", " ")
    if len(reason) > 200:
        reason = reason[:200] + " ...[truncated]"
    path = entry.get("interface_path") or "-"
    engine = entry.get("engine") or "-"
    model = entry.get("model") or "-"
    return (
        f"#{entry.get('id')}  [{_fmt_ts(entry.get('created_at'))}]  "
        f"code={entry.get('failure_code')}  stage={entry.get('stage')}\n"
        f"    interface={path}  engine={engine}  model={model}\n"
        f"    reason: {reason}"
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_llm_failures(
    limit: int = 20,
    search: str = "",
    failure_code: str = "",
    stage: str = "",
    since_minutes: Optional[int] = None,
    sort: str = "desc",
    target: Optional[str] = None,
) -> str:
    """List recent persisted LLM failure entries (same data as the WebUI Logs page).

    Each entry is a structured record of a turn that failed somewhere in the
    pipeline (LLM call, action parsing/execution, correction loop, or delivery).
    Cross-reference the ``created_at`` / ``interface_path`` / ``reason`` with
    ``synth_logs.search_logs`` to see the surrounding raw log context.

    Args:
        limit: Max entries to return (default 20, max 200).
        search: Case-insensitive substring matched against reason, interface_path,
                engine, model, and failure_code.
        failure_code: Exact failure code filter, e.g. "delivery_failed", "timeout",
                      "malformed_json", "correction_exhausted", "llm_failure".
        stage: Exact stage filter, e.g. "delivery", "llm_fallback", "action".
        since_minutes: Only entries created in the last N minutes.
        sort: "desc" (newest first, default) or "asc".
        target: DB target (default runtime). Usually leave unset.

    Returns:
        A compact, newest-first list. Use get_llm_failure(id) for full detail.
    """
    limit = max(1, min(limit, 200))
    order = "ASC" if str(sort).lower() == "asc" else "DESC"

    where: list[str] = []
    params: list[Any] = []

    if search:
        term = f"%{search}%"
        where.append(
            "(reason ILIKE %s OR interface_path ILIKE %s OR engine ILIKE %s "
            "OR model ILIKE %s OR failure_code ILIKE %s)"
        )
        params.extend([term] * 5)
    if failure_code:
        where.append("failure_code = %s")
        params.append(failure_code)
    if stage:
        where.append("stage = %s")
        params.append(stage)
    if since_minutes is not None:
        where.append("created_at >= NOW() - (%s || ' minutes')::interval")
        params.append(str(int(since_minutes)))

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT {', '.join(_COLUMNS)} FROM llm_failure_log "
        f"{where_sql} ORDER BY created_at {order}, id {order} LIMIT %s"
    )
    params.append(limit)

    try:
        rows = _query(sql, params, target)
    except Exception as exc:
        return f"Failed to read llm_failure_log: {exc}"

    if not rows:
        filters = []
        if search:
            filters.append(f"search='{search}'")
        if failure_code:
            filters.append(f"code={failure_code}")
        if stage:
            filters.append(f"stage={stage}")
        if since_minutes is not None:
            filters.append(f"last {since_minutes}min")
        suffix = f" (filters: {', '.join(filters)})" if filters else ""
        return f"No LLM failure entries found{suffix}."

    header = f"{len(rows)} LLM failure entr{'y' if len(rows) == 1 else 'ies'} (newest first):"
    body = "\n\n".join(_fmt_entry_line(row) for row in rows)
    return header + "\n\n" + body


@mcp.tool()
def get_llm_failure(failure_id: int, target: Optional[str] = None) -> str:
    """Get full detail for a single LLM failure entry, including metadata JSON.

    Args:
        failure_id: The entry id (from list_llm_failures).
        target: DB target (default runtime). Usually leave unset.
    """
    sql = f"SELECT {', '.join(_COLUMNS)} FROM llm_failure_log WHERE id = %s LIMIT 1"
    try:
        rows = _query(sql, [failure_id], target)
    except Exception as exc:
        return f"Failed to read llm_failure_log: {exc}"

    if not rows:
        return f"No LLM failure entry with id={failure_id}."

    entry = rows[0]
    metadata = _parse_metadata(entry.get("metadata"))
    lines = [
        f"LLM failure entry #{entry.get('id')}",
        f"  created_at:      {_fmt_ts(entry.get('created_at'))}",
        f"  failure_code:    {entry.get('failure_code')}",
        f"  stage:           {entry.get('stage')}",
        f"  interface_path:  {entry.get('interface_path') or '-'}",
        f"  chat_id:         {entry.get('chat_id') or '-'}",
        f"  thread_id:       {entry.get('thread_id') or '-'}",
        f"  engine:          {entry.get('engine') or '-'}",
        f"  model:           {entry.get('model') or '-'}",
        f"  message_id:      {entry.get('message_id') or '-'}",
        "",
        "  reason:",
        f"    {entry.get('reason')}",
    ]
    preview = entry.get("content_preview")
    if preview:
        lines += ["", "  content_preview:", f"    {preview}"]
    if metadata is not None:
        lines += [
            "",
            "  metadata:",
            json.dumps(metadata, indent=2, ensure_ascii=False, default=str),
        ]
    return "\n".join(lines)


@mcp.tool()
def llm_failure_summary(
    since_minutes: int = 60,
    target: Optional[str] = None,
) -> str:
    """Aggregate view of recent LLM failures: counts by code, stage, and interface.

    Fast "what is failing right now?" overview. Follow up with list_llm_failures
    (filtered) and synth_logs for the raw context.

    Args:
        since_minutes: Look-back window in minutes (default 60).
        target: DB target (default runtime). Usually leave unset.
    """
    window = str(int(max(1, since_minutes)))
    time_clause = "created_at >= NOW() - (%s || ' minutes')::interval"

    def _counts(group_col: str) -> list[dict[str, Any]]:
        sql = (
            f"SELECT {group_col} AS k, COUNT(*) AS c FROM llm_failure_log "
            f"WHERE {time_clause} GROUP BY {group_col} ORDER BY c DESC"
        )
        return _query(sql, [window], target)

    try:
        total_rows = _query(
            f"SELECT COUNT(*) AS c FROM llm_failure_log WHERE {time_clause}",
            [window],
            target,
        )
        by_code = _counts("failure_code")
        by_stage = _counts("stage")
        by_iface = _counts("interface_path")
    except Exception as exc:
        return f"Failed to summarize llm_failure_log: {exc}"

    total = total_rows[0].get("c", 0) if total_rows else 0

    try:
        target_note = _target_summary(_resolve_target(target))
    except Exception:
        target_note = target or "runtime"

    if not total:
        return f"No LLM failures in the last {since_minutes} min ({target_note})."

    def _fmt_group(title: str, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return f"  {title}: (none)"
        parts = [f"  {title}:"]
        for row in rows:
            key = row.get("k")
            parts.append(f"    {key if key is not None else '(null)'}: {row.get('c')}")
        return "\n".join(parts)

    return "\n".join(
        [
            f"LLM failures in the last {since_minutes} min: {total} total "
            f"({target_note})",
            "",
            _fmt_group("by failure_code", by_code),
            "",
            _fmt_group("by stage", by_stage),
            "",
            _fmt_group("by interface_path", by_iface),
        ]
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
