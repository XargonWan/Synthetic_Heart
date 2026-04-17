#!/usr/bin/env python3
"""MCP server exposing the Synthetic Heart MariaDB database to AI agents.

Provides read-only and carefully guarded write access so agents can diagnose
state, inspect config, and make safe corrections without risking corruption.

Read tools
----------
list_tables          -- all tables with row counts
describe_table       -- columns + types for one table
get_config           -- read config registry (config_key / value)
get_memories         -- recent memory entries
get_emotion_state    -- current emotion intensities
get_recent_diary     -- latest diary entries
get_chat_history     -- conversation history for an interface_path
get_grillo_beats     -- Grillo beat schedule
get_external_endpoints -- LLM/API endpoint registry (keys redacted)
run_select           -- arbitrary SELECT query (write keywords rejected)

Write tools  (require confirm=True)
------------------------------------
set_config           -- update / insert a config registry entry
add_memory           -- insert a new memory entry
delete_memory        -- delete a memory by id

Connection
----------
Reads DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME from environment.
Defaults match the dev container: host=localhost, port=3306, user/pass/db=synth.

Usage (stdio transport, registered in .mcp.json)
-------------------------------------------------
    uv run python mcp_servers/synth_db.py
"""

from __future__ import annotations

import os
import re
from typing import Any, Optional

import pymysql
import pymysql.cursors
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------


def _db_kwargs() -> dict[str, Any]:
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "synth"),
        "password": os.getenv("DB_PASS", "synth"),
        "database": os.getenv("DB_NAME", "synth"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 5,
        "autocommit": False,
    }


def _connect() -> pymysql.connections.Connection:
    return pymysql.connect(**_db_kwargs())


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

# Keywords that must not appear in "read-only" SELECT queries.
_WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE"
    r"|CALL|EXEC|LOAD\s+DATA|INTO\s+OUTFILE)\b",
    re.IGNORECASE,
)


def _assert_select_only(sql: str) -> None:
    """Raise ValueError if *sql* is not a plain SELECT statement."""
    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        raise ValueError("Only SELECT statements are permitted.")
    m = _WRITE_RE.search(stripped)
    if m:
        raise ValueError(
            f"Query contains forbidden keyword '{m.group().upper()}'. "
            "Only SELECT is allowed."
        )


def _fmt_rows(rows: list[dict[str, Any]], max_rows: int = 100) -> str:
    """Format a list of dicts as a human-readable table."""
    if not rows:
        return "(no rows)"
    capped = rows[:max_rows]
    lines: list[str] = []
    for row in capped:
        parts = []
        for k, v in row.items():
            val = str(v)
            # Truncate very long cell values so the context doesn't explode.
            if len(val) > 300:
                val = val[:300] + " ...[truncated]"
            parts.append(f"{k}: {val}")
        lines.append("  " + "  |  ".join(parts))
    if len(rows) > max_rows:
        lines.append(f"  ... (showing {max_rows} of {len(rows)} rows)")
    return "\n".join(lines)


mcp = FastMCP("synth-db")


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_tables() -> str:
    """List all tables in the Synth database with their row counts.

    Use the table names returned here with describe_table() and run_select().
    """
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables: list[str] = [list(row.values())[0] for row in cur.fetchall()]
                lines: list[str] = [f"{'TABLE':<40} {'ROWS':>8}", "-" * 52]
                for tbl in sorted(tables):
                    safe_tbl = re.sub(r"[^\w]", "", tbl)
                    try:
                        cur.execute(f"SELECT COUNT(*) AS cnt FROM `{safe_tbl}`")
                        cnt: Any = (cur.fetchone() or {}).get("cnt", "?")
                    except Exception:
                        cnt = "?"
                    lines.append(f"{tbl:<40} {cnt!s:>8}")
                return "\n".join(lines)
    except Exception as exc:
        return f"DB connection error: {exc}"


@mcp.tool()
def describe_table(table: str) -> str:
    """Show columns, types, nullability, and keys for a table.

    Args:
        table: Table name (e.g. 'config', 'memories', 'chat_history_cache').
    """
    # Sanitise: only allow identifier characters to prevent injection.
    safe_table = re.sub(r"[^\w]", "", table)
    if not safe_table:
        return "Invalid table name."
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(f"DESCRIBE `{safe_table}`")
                rows = cur.fetchall()
        if not rows:
            return f"Table '{table}' not found or has no columns."
        lines: list[str] = [
            f"Schema for '{table}':\n",
            f"  {'Field':<30} {'Type':<25} {'Null':<6} {'Key':<6} {'Default'}",
            "  " + "-" * 78,
        ]
        for row in rows:
            lines.append(
                f"  {str(row.get('Field', '')):<30}"
                f" {str(row.get('Type', '')):<25}"
                f" {str(row.get('Null', '')):<6}"
                f" {str(row.get('Key', '')):<6}"
                f" {str(row.get('Default', ''))}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_config(key: Optional[str] = None) -> str:
    """Read config registry values from the config table.

    Args:
        key: Specific config_key to look up (e.g. 'BASE_CORTEX', 'SYNTH_NAME').
             Omit to list every key/value pair sorted alphabetically.
    """
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                if key:
                    cur.execute(
                        "SELECT config_key, value, updated_at FROM config"
                        " WHERE config_key = %s",
                        (key,),
                    )
                else:
                    cur.execute(
                        "SELECT config_key, value, updated_at FROM config"
                        " ORDER BY config_key"
                    )
                rows = cur.fetchall()
        if not rows:
            return (
                f"No config entry found for key='{key}'"
                if key
                else "config table is empty."
            )
        return _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_memories(
    scope: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Read memory entries from the memories table.

    Args:
        scope:  Optional scope filter (e.g. 'global', 'trainer').
        author: Optional author filter (e.g. 'synth', 'agent').
        limit:  Max rows to return (default 20, max 200).
    """
    limit = min(max(1, limit), 200)
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                conditions: list[str] = []
                params: list[Any] = []
                if scope:
                    conditions.append("scope = %s")
                    params.append(scope)
                if author:
                    conditions.append("author = %s")
                    params.append(author)
                where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
                params.append(limit)
                cur.execute(
                    f"SELECT id, timestamp, content, author, tags, scope, emotion,"
                    f" intensity FROM memories {where} ORDER BY id DESC LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
        label = f"scope={scope or 'any'}, author={author or 'any'}"
        return f"{len(rows)} memories ({label}):\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_emotion_state() -> str:
    """Read current emotion intensities from the emotion_state table.

    Results are sorted by intensity descending so the strongest emotions appear first.
    """
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT emotion_name, intensity, updated_at"
                    " FROM emotion_state ORDER BY intensity DESC"
                )
                rows = cur.fetchall()
        if not rows:
            return "emotion_state table is empty."
        return "Current emotion state (strongest first):\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_recent_diary(limit: int = 10) -> str:
    """Read the most recent diary entries from ai_diary.

    Args:
        limit: Max entries to return (default 10, max 50).
    """
    limit = min(max(1, limit), 50)
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, content, created_at FROM ai_diary"
                    " ORDER BY id DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        if not rows:
            return "No diary entries found."
        return f"{len(rows)} diary entries (newest first):\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_chat_history(interface_path: str, limit: int = 20) -> str:
    """Read recent messages for a given interface_path from chat_history_cache.

    Args:
        interface_path: e.g. 'telegram_bot/12345', 'discord_bot/guild/channel',
                        'synth_webui/<uuid>'.
        limit: Max messages to return (default 20, max 100).
    """
    limit = min(max(1, limit), 100)
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, sender_name, sender_id, message_text, timestamp"
                    " FROM chat_history_cache"
                    " WHERE interface_path = %s"
                    " ORDER BY id DESC LIMIT %s",
                    (interface_path, limit),
                )
                rows = cur.fetchall()
        if not rows:
            return f"No chat history found for interface_path='{interface_path}'."
        return (
            f"{len(rows)} messages for '{interface_path}' (newest first):\n\n"
            + _fmt_rows(rows)
        )
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_grillo_beats() -> str:
    """Read the Grillo autonomous beat schedule.

    Shows beat_type, next scheduled time, and enabled status.
    """
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM grillo_beats ORDER BY next_beat")
                rows = cur.fetchall()
        if not rows:
            return "No Grillo beats configured."
        return "Grillo beat schedule:\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_external_endpoints() -> str:
    """Read the LLM / API endpoint registry.

    API keys (api_key_enc) are always redacted. Use this to inspect configured
    endpoints, their protocol, probe status, and available models.
    """
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, display_label, protocol, base_url,"
                    " enabled, capabilities, available_models, default_model,"
                    " probe_status, last_probe_at"
                    " FROM external_endpoints ORDER BY name"
                )
                rows = cur.fetchall()
        if not rows:
            return "No external endpoints configured."
        return f"{len(rows)} endpoint(s):\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def run_select(sql: str, max_rows: int = 50) -> str:
    """Execute an arbitrary SELECT query against the Synth database.

    Any query containing INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE,
    REPLACE, GRANT, REVOKE, or LOAD DATA is rejected before reaching the DB.

    Args:
        sql:      A valid SELECT statement. Parameterisation is not supported
                  here — build a literal query. Avoid embedding untrusted strings.
        max_rows: Cap on returned rows (default 50, max 200).
    """
    max_rows = min(max(1, max_rows), 200)
    try:
        _assert_select_only(sql)
    except ValueError as exc:
        return f"Query rejected: {exc}"
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() or []
        return f"{len(rows)} row(s):\n\n" + _fmt_rows(list(rows), max_rows)
    except Exception as exc:
        return f"DB error: {exc}"


# ---------------------------------------------------------------------------
# Write tools  (require confirm=True)
# ---------------------------------------------------------------------------


@mcp.tool()
def set_config(key: str, value: str, confirm: bool = False) -> str:
    """Update or insert a config registry entry (config table).

    Changes take effect the next time Synth reads that config key — typically
    on the next request cycle. Some keys require a full service restart.

    ⚠ Modifies live runtime configuration. Pass confirm=True to execute.

    Args:
        key:     Config key, e.g. 'BASE_CORTEX', 'SYNTH_NAME'.
        value:   New value string.
        confirm: Pass True to actually write; False (default) returns a dry-run preview.
    """
    if not confirm:
        return (
            f"DRY RUN — would UPSERT config:\n"
            f"  config_key = '{key}'\n"
            f"  value      = '{value}'\n\n"
            "Pass confirm=True to execute."
        )
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO config (config_key, value)"
                    " VALUES (%s, %s)"
                    " ON DUPLICATE KEY UPDATE value = VALUES(value)",
                    (key, value),
                )
            conn.commit()
        return f"OK — config['{key}'] = '{value}'"
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def add_memory(
    content: str,
    author: str = "agent",
    tags: str = "",
    scope: str = "global",
    emotion: str = "",
    confirm: bool = False,
) -> str:
    """Insert a new memory entry into the memories table.

    Memories are injected into Synth's context on future interactions — write
    only factual, relevant information.

    ⚠ Persists data that affects Synth's behaviour. Pass confirm=True to execute.

    Args:
        content: The memory text (keep concise).
        author:  Who is recording this (default 'agent').
        tags:    Comma-separated tags for retrieval.
        scope:   Memory scope — e.g. 'global', 'trainer' (default 'global').
        emotion: Optional emotion label associated with this memory.
        confirm: Pass True to actually write; False (default) returns dry-run preview.
    """
    if not confirm:
        return (
            f"DRY RUN — would insert memory:\n"
            f"  content: {content[:200]}\n"
            f"  author:  {author}\n"
            f"  tags:    {tags}\n"
            f"  scope:   {scope}\n"
            f"  emotion: {emotion}\n\n"
            "Pass confirm=True to execute."
        )
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO memories (timestamp, content, author, tags, scope, emotion)"
                    " VALUES (NOW(), %s, %s, %s, %s, %s)",
                    (content, author, tags, scope, emotion),
                )
                last_id = cur.lastrowid
            conn.commit()
        return f"OK — inserted memory id={last_id}"
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def delete_memory(memory_id: int, confirm: bool = False) -> str:
    """Delete a memory entry by its integer ID.

    Use get_memories() to find the ID of the entry to remove.

    ⚠ Permanent deletion. Pass confirm=True to execute.

    Args:
        memory_id: Integer primary key from the memories table.
        confirm:   Pass True to actually delete; False (default) returns dry-run preview.
    """
    if not confirm:
        return (
            f"DRY RUN — would DELETE FROM memories WHERE id = {memory_id}\n\n"
            "Pass confirm=True to execute."
        )
    try:
        conn = _connect()
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
                affected = cur.rowcount
            conn.commit()
        if affected == 0:
            return f"No memory found with id={memory_id} (nothing deleted)."
        return f"OK — deleted memory id={memory_id}"
    except Exception as exc:
        return f"DB error: {exc}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
