#!/usr/bin/env python3
"""MCP server exposing Synthetic Heart database targets to AI agents.

The runtime database moved to PostgreSQL, but the project can still have more
than one relevant store at the same time:

- the current runtime DB from `.env` (`DB_*` / `DATABASE_URL`)
- the legacy/source MariaDB (`SOURCE_DB_*`)
- the SOUL Postgres repository (`SOUL_POSTGRES_DSN`)

This server now treats the repo `.env` as the canonical configuration source
and exposes optional `target` selection on every tool so agents can inspect the
correct backend instead of being pinned to a stale MCP env override.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
from typing import Any, Optional

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover - test/import fallback only

    class FastMCP:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def tool(self):
            def _decorator(func):
                return func

            return _decorator

        def run(self, transport: str = "stdio") -> None:
            raise RuntimeError(
                f"FastMCP dependencies are unavailable; cannot start transport={transport!r}"
            )


try:
    import pymysql
    import pymysql.cursors

    PYMYSQL_AVAILABLE = True
except ImportError:
    PYMYSQL_AVAILABLE = False

try:
    import psycopg2
    import psycopg2.extras

    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False


@dataclass(frozen=True)
class DbTarget:
    name: str
    db_type: str
    host: str
    port: int
    user: str
    password: str
    database: str
    dsn: str | None = None


_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _REPO_ROOT / ".env"
_WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE"
    r"|CALL|EXEC|LOAD\s+DATA|INTO\s+OUTFILE)\b",
    re.IGNORECASE,
)
_TARGET_ALIASES = {
    "default": "runtime",
    "main": "runtime",
    "app": "runtime",
    "legacy": "source",
    "process": "process_env",
}


def _strip_wrapping_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    return stripped


def _load_repo_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return values

    for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        clean_key = key.strip()
        if not clean_key:
            continue
        values[clean_key] = _strip_wrapping_quotes(value)
    return values


_REPO_ENV = _load_repo_env()


def _normalize_db_type(value: str | None, default: str = "mariadb") -> str:
    normalized = str(value or default).strip().lower()
    if normalized in {"mysql", "mariadb"}:
        return "mariadb"
    if normalized in {"postgres", "postgresql"}:
        return "postgres"
    return default


def _repo_or_process_value(key: str, default: str | None = None) -> str | None:
    repo_value = _REPO_ENV.get(key)
    if repo_value not in {None, ""}:
        return repo_value
    process_value = os.getenv(key)
    if process_value in {None, ""}:
        return default
    return process_value


def _process_value(key: str, default: str | None = None) -> str | None:
    process_value = os.getenv(key)
    if process_value in {None, ""}:
        return default
    return process_value


def _coerce_port(value: str | None, default: int) -> int:
    try:
        return int(str(value or default))
    except (TypeError, ValueError):
        return default


def _build_runtime_target() -> DbTarget:
    db_type = _normalize_db_type(
        _repo_or_process_value(
            "SYNTH_DB_TYPE", _repo_or_process_value("DB_TYPE", "mariadb")
        )
    )
    default_port = 5432 if db_type == "postgres" else 3306
    return DbTarget(
        name="runtime",
        db_type=db_type,
        host=str(_repo_or_process_value("DB_HOST", "localhost") or "localhost"),
        port=_coerce_port(_repo_or_process_value("DB_PORT"), default_port),
        user=str(_repo_or_process_value("DB_USER", "synth") or "synth"),
        password=str(_repo_or_process_value("DB_PASS", "synth") or "synth"),
        database=str(_repo_or_process_value("DB_NAME", "synth") or "synth"),
        dsn=(
            str(
                _repo_or_process_value(
                    "DATABASE_URL", _repo_or_process_value("DB_DSN", "")
                )
                or ""
            )
            or None
        ),
    )


def _build_source_target() -> DbTarget | None:
    if not any(
        _repo_or_process_value(key)
        for key in (
            "SOURCE_DB_HOST",
            "SOURCE_DB_PORT",
            "SOURCE_DB_USER",
            "SOURCE_DB_NAME",
        )
    ):
        return None

    db_type = _normalize_db_type(_repo_or_process_value("SOURCE_DB_TYPE", "mariadb"))
    default_port = 5432 if db_type == "postgres" else 3306
    return DbTarget(
        name="source",
        db_type=db_type,
        host=str(_repo_or_process_value("SOURCE_DB_HOST", "localhost") or "localhost"),
        port=_coerce_port(_repo_or_process_value("SOURCE_DB_PORT"), default_port),
        user=str(_repo_or_process_value("SOURCE_DB_USER", "synth") or "synth"),
        password=str(
            _repo_or_process_value(
                "SOURCE_DB_PASSWORD",
                _repo_or_process_value("SOURCE_DB_PASS", "synth"),
            )
            or "synth"
        ),
        database=str(_repo_or_process_value("SOURCE_DB_NAME", "synth") or "synth"),
        dsn=(str(_repo_or_process_value("SOURCE_DATABASE_URL", "") or "") or None),
    )


def _build_soul_target() -> DbTarget | None:
    soul_dsn = _repo_or_process_value("SOUL_POSTGRES_DSN", "") or ""
    if not soul_dsn and not any(
        _repo_or_process_value(key)
        for key in ("SOUL_PG_DB", "SOUL_PG_USER", "SOUL_PG_HOST", "SOUL_PG_PORT")
    ):
        return None

    runtime_target = _build_runtime_target()
    return DbTarget(
        name="soul",
        db_type="postgres",
        host=str(
            _repo_or_process_value("SOUL_PG_HOST", runtime_target.host)
            or runtime_target.host
        ),
        port=_coerce_port(_repo_or_process_value("SOUL_PG_PORT"), runtime_target.port),
        user=str(
            _repo_or_process_value("SOUL_PG_USER", runtime_target.user)
            or runtime_target.user
        ),
        password=str(
            _repo_or_process_value("SOUL_PG_PASSWORD", runtime_target.password)
            or runtime_target.password
        ),
        database=str(
            _repo_or_process_value("SOUL_PG_DB", runtime_target.database)
            or runtime_target.database
        ),
        dsn=soul_dsn or runtime_target.dsn,
    )


def _build_process_env_target() -> DbTarget | None:
    if not any(
        _process_value(key)
        for key in (
            "DB_TYPE",
            "DB_HOST",
            "DB_PORT",
            "DB_USER",
            "DB_NAME",
            "DATABASE_URL",
        )
    ):
        return None

    db_type = _normalize_db_type(
        _process_value("SYNTH_DB_TYPE", _process_value("DB_TYPE", "mariadb"))
    )
    default_port = 5432 if db_type == "postgres" else 3306
    return DbTarget(
        name="process_env",
        db_type=db_type,
        host=str(_process_value("DB_HOST", "localhost") or "localhost"),
        port=_coerce_port(_process_value("DB_PORT"), default_port),
        user=str(_process_value("DB_USER", "synth") or "synth"),
        password=str(_process_value("DB_PASS", "synth") or "synth"),
        database=str(_process_value("DB_NAME", "synth") or "synth"),
        dsn=(
            str(_process_value("DATABASE_URL", _process_value("DB_DSN", "")) or "")
            or None
        ),
    )


def _configured_targets() -> dict[str, DbTarget]:
    targets = {"runtime": _build_runtime_target()}

    source_target = _build_source_target()
    if source_target is not None:
        targets[source_target.name] = source_target

    soul_target = _build_soul_target()
    if soul_target is not None:
        targets[soul_target.name] = soul_target

    process_target = _build_process_env_target()
    if process_target is not None and process_target != targets["runtime"]:
        targets[process_target.name] = process_target

    return targets


def _resolve_target(target: str | None = None) -> DbTarget:
    requested = str(target or os.getenv("SYNTH_DB_TARGET", "runtime")).strip().lower()
    resolved_name = _TARGET_ALIASES.get(requested, requested)
    if resolved_name == "all":
        raise ValueError("Target 'all' is only valid for list_tables().")

    targets = _configured_targets()
    if resolved_name not in targets:
        available = ", ".join(sorted(targets))
        raise ValueError(
            f"Unknown target '{requested}'. Available targets: {available}"
        )
    return targets[resolved_name]


def _target_summary(target: DbTarget) -> str:
    dsn_note = " via dsn" if target.dsn else ""
    return (
        f"{target.name}: {target.db_type} {target.user}@{target.host}:{target.port}/"
        f"{target.database}{dsn_note}"
    )


def _db_kwargs(target: str | None = None) -> dict[str, Any]:
    config = _resolve_target(target)
    if config.db_type == "mariadb":
        if not PYMYSQL_AVAILABLE:
            raise ImportError(
                "pymysql is required for MariaDB connections but not installed"
            )
        return {
            "host": config.host,
            "port": config.port,
            "user": config.user,
            "password": config.password,
            "database": config.database,
            "charset": "utf8mb4",
            "cursorclass": pymysql.cursors.DictCursor,
            "connect_timeout": 5,
            "autocommit": False,
        }

    if config.db_type == "postgres":
        if not PSYCOPG2_AVAILABLE:
            raise ImportError(
                "psycopg2 is required for PostgreSQL connections but not installed"
            )
        kwargs: dict[str, Any] = {
            "cursor_factory": psycopg2.extras.RealDictCursor,
            "connect_timeout": 5,
        }
        if config.dsn:
            kwargs["dsn"] = config.dsn
        else:
            kwargs.update(
                {
                    "host": config.host,
                    "port": config.port,
                    "user": config.user,
                    "password": config.password,
                    "dbname": config.database,
                }
            )
        return kwargs

    raise ValueError(f"Unsupported DB_TYPE: {config.db_type}")


def _connect(target: str | None = None) -> Any:
    config = _resolve_target(target)
    kwargs = _db_kwargs(target)
    if config.db_type == "mariadb":
        return pymysql.connect(**kwargs)
    if config.db_type == "postgres":
        return psycopg2.connect(**kwargs)
    raise ValueError(f"Unsupported DB_TYPE: {config.db_type}")


def _assert_select_only(sql: str) -> None:
    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        raise ValueError("Only SELECT statements are permitted.")
    match = _WRITE_RE.search(stripped)
    if match:
        raise ValueError(
            f"Query contains forbidden keyword '{match.group().upper()}'. Only SELECT is allowed."
        )


def _rows_to_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            normalized.append(row)
            continue
        if hasattr(row, "keys"):
            try:
                normalized.append(dict(row))
                continue
            except Exception:
                pass
        normalized.append({"value": row})
    return normalized


def _fmt_rows(rows: list[Any], max_rows: int = 100) -> str:
    if not rows:
        return "(no rows)"
    dict_rows = _rows_to_dicts(rows)
    capped = dict_rows[:max_rows]
    lines: list[str] = []
    for row in capped:
        parts: list[str] = []
        for key, value in row.items():
            rendered = str(value)
            if len(rendered) > 300:
                rendered = rendered[:300] + " ...[truncated]"
            parts.append(f"{key}: {rendered}")
        lines.append("  " + "  |  ".join(parts))
    if len(dict_rows) > max_rows:
        lines.append(f"  ... (showing {max_rows} of {len(dict_rows)} rows)")
    return "\n".join(lines)


def _quote_identifier(identifier: str, db_type: str) -> str:
    safe_identifier = re.sub(r"[^\w]", "", identifier)
    quote_char = "`" if db_type == "mariadb" else '"'
    return f"{quote_char}{safe_identifier}{quote_char}"


def _get_table_columns(cur: Any, table: str, target: str | None = None) -> list[str]:
    config = _resolve_target(target)
    safe_table = re.sub(r"[^\w]", "", table)
    if not safe_table:
        return []

    if config.db_type == "mariadb":
        cur.execute(f"DESCRIBE {_quote_identifier(safe_table, config.db_type)}")
        return [str(row.get("Field", "")) for row in cur.fetchall()]

    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position",
        (safe_table,),
    )
    return [str(row.get("column_name", "")) for row in cur.fetchall()]


def _select_recent_diary_columns(cur: Any, target: str | None = None) -> list[str]:
    columns = set(_get_table_columns(cur, "ai_diary", target))
    preferred = [
        "id",
        "timestamp",
        "content",
        "interface",
        "chat_id",
        "thread_id",
        "interaction_summary",
        "personal_thought",
        "emotions",
        "user_message",
    ]
    selected = [column for column in preferred if column in columns]
    return selected or ["id", "content"]


def _list_tables_for_target(target: DbTarget) -> str:
    conn = _connect(target.name)
    with conn:
        with conn.cursor() as cur:
            if target.db_type == "mariadb":
                cur.execute("SHOW TABLES")
                tables = [list(row.values())[0] for row in cur.fetchall()]
            else:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
                tables = [row["table_name"] for row in cur.fetchall()]

            lines: list[str] = [
                f"[{target.name}] {_target_summary(target)}",
                f"{'TABLE':<40} {'ROWS':>8}",
                "-" * 52,
            ]
            for table in sorted(tables):
                safe_table = re.sub(r"[^\w]", "", str(table))
                if not safe_table:
                    continue
                try:
                    cur.execute(
                        f"SELECT COUNT(*) AS cnt FROM {_quote_identifier(safe_table, target.db_type)}"
                    )
                    count_row = cur.fetchone() or {}
                    count = count_row.get("cnt", "?")
                except Exception:
                    count = "?"
                lines.append(f"{table:<40} {count!s:>8}")
            return "\n".join(lines)


mcp = FastMCP("synth-db")


@mcp.tool()
def get_db_targets() -> str:
    """List the configured database targets available to this MCP server."""
    targets = _configured_targets()
    lines = [f"Loaded repo env: {_ENV_FILE}"]
    for name in sorted(targets):
        lines.append(f"- {_target_summary(targets[name])}")
    return "\n".join(lines)


@mcp.tool()
def list_tables(target: Optional[str] = None) -> str:
    """List tables and row counts for the selected DB target.

    Args:
        target: Optional target name. Use `runtime` (default), `source`,
            `soul`, `process_env`, or `all`.
    """
    try:
        requested = str(target or "runtime").strip().lower()
        if requested == "all":
            return "\n\n".join(
                _list_tables_for_target(target_config)
                for target_config in _configured_targets().values()
            )
        return _list_tables_for_target(_resolve_target(requested))
    except Exception as exc:
        return f"DB connection error: {exc}"


@mcp.tool()
def describe_table(table: str, target: Optional[str] = None) -> str:
    """Show columns, types, nullability, and defaults for a table."""
    safe_table = re.sub(r"[^\w]", "", table)
    if not safe_table:
        return "Invalid table name."

    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                if config.db_type == "mariadb":
                    cur.execute(
                        f"DESCRIBE {_quote_identifier(safe_table, config.db_type)}"
                    )
                    rows = cur.fetchall()
                    if not rows:
                        return f"Table '{table}' not found or has no columns on target '{config.name}'."
                    lines = [
                        f"[{config.name}] Schema for '{table}':\n",
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

                cur.execute(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = %s "
                    "ORDER BY ordinal_position",
                    (safe_table,),
                )
                rows = cur.fetchall()
                if not rows:
                    return f"Table '{table}' not found or has no columns on target '{config.name}'."
                lines = [
                    f"[{config.name}] Schema for '{table}':\n",
                    f"  {'Column':<30} {'Type':<25} {'Nullable':<8} {'Default'}",
                    "  " + "-" * 70,
                ]
                for row in rows:
                    nullable = "YES" if row.get("is_nullable") == "YES" else "NO"
                    lines.append(
                        f"  {str(row.get('column_name', '')):<30}"
                        f" {str(row.get('data_type', '')):<25}"
                        f" {nullable:<8}"
                        f" {str(row.get('column_default', '') or '')}"
                    )
                return "\n".join(lines)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_config(key: Optional[str] = None, target: Optional[str] = None) -> str:
    """Read config registry values from the selected target."""
    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                if key:
                    cur.execute(
                        "SELECT config_key, value, updated_at FROM config WHERE config_key = %s",
                        (key,),
                    )
                else:
                    cur.execute(
                        "SELECT config_key, value, updated_at FROM config ORDER BY config_key"
                    )
                rows = cur.fetchall()
        if not rows:
            return (
                f"No config entry found for key='{key}' on target '{config.name}'."
                if key
                else f"config table is empty on target '{config.name}'."
            )
        return f"[{config.name}]\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_memories(
    scope: Optional[str] = None,
    author: Optional[str] = None,
    limit: int = 20,
    target: Optional[str] = None,
) -> str:
    """Read memory entries from the selected target."""
    limit = min(max(1, limit), 200)
    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
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
                    f"SELECT id, timestamp, content, author, tags, scope, emotion, intensity "
                    f"FROM memories {where} ORDER BY id DESC LIMIT %s",
                    params,
                )
                rows = cur.fetchall()
        label = f"scope={scope or 'any'}, author={author or 'any'}"
        return f"[{config.name}] {len(rows)} memories ({label}):\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_emotion_state(target: Optional[str] = None) -> str:
    """Read current emotion intensities from the selected target."""
    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT emotion_name, intensity, updated_at FROM emotion_state ORDER BY intensity DESC"
                )
                rows = cur.fetchall()
        if not rows:
            return f"emotion_state table is empty on target '{config.name}'."
        return (
            f"[{config.name}] Current emotion state (strongest first):\n\n"
            + _fmt_rows(rows)
        )
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_recent_diary(limit: int = 10, target: Optional[str] = None) -> str:
    """Read recent diary rows from the selected target.

    This helper now uses the canonical `timestamp` column and dynamically limits
    the selected columns to those actually present in the current schema.
    """
    limit = min(max(1, limit), 50)
    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                selected_columns = _select_recent_diary_columns(cur, config.name)
                rendered_columns = ", ".join(
                    _quote_identifier(column, config.db_type)
                    for column in selected_columns
                )
                order_column = "id" if "id" in selected_columns else selected_columns[0]
                cur.execute(
                    f"SELECT {rendered_columns} FROM {_quote_identifier('ai_diary', config.db_type)} "
                    f"ORDER BY {_quote_identifier(order_column, config.db_type)} DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
        if not rows:
            return f"No diary entries found on target '{config.name}'."
        return (
            f"[{config.name}] {len(rows)} diary entries (newest first):\n\n"
            + _fmt_rows(rows)
        )
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_chat_history(
    interface_path: str, limit: int = 20, target: Optional[str] = None
) -> str:
    """Read recent messages for an interface_path from the selected target."""
    limit = min(max(1, limit), 100)
    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, sender_name, sender_id, message_text, timestamp "
                    "FROM chat_history_cache WHERE interface_path = %s ORDER BY id DESC LIMIT %s",
                    (interface_path, limit),
                )
                rows = cur.fetchall()
        if not rows:
            return f"No chat history found for interface_path='{interface_path}' on target '{config.name}'."
        return (
            f"[{config.name}] {len(rows)} messages for '{interface_path}' (newest first):\n\n"
            + _fmt_rows(rows)
        )
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_grillo_beats(target: Optional[str] = None) -> str:
    """Read the Grillo autonomous beat schedule from the selected target."""
    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM grillo_beats ORDER BY next_beat")
                rows = cur.fetchall()
        if not rows:
            return f"No Grillo beats configured on target '{config.name}'."
        return f"[{config.name}] Grillo beat schedule:\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def get_external_endpoints(target: Optional[str] = None) -> str:
    """Read the endpoint registry from the selected target."""
    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, display_label, protocol, base_url, enabled, capabilities, "
                    "available_models, default_model, probe_status, last_probe_at "
                    "FROM external_endpoints ORDER BY name"
                )
                rows = cur.fetchall()
        if not rows:
            return f"No external endpoints configured on target '{config.name}'."
        return f"[{config.name}] {len(rows)} endpoint(s):\n\n" + _fmt_rows(rows)
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def run_select(sql: str, max_rows: int = 50, target: Optional[str] = None) -> str:
    """Execute an arbitrary SELECT query against the selected DB target."""
    max_rows = min(max(1, max_rows), 200)
    try:
        _assert_select_only(sql)
    except ValueError as exc:
        return f"Query rejected: {exc}"

    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() or []
        return f"[{config.name}] {len(rows)} row(s):\n\n" + _fmt_rows(
            list(rows), max_rows
        )
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def set_config(
    key: str,
    value: str,
    confirm: bool = False,
    target: Optional[str] = None,
) -> str:
    """Update or insert a config registry entry on the selected target."""
    if not confirm:
        return (
            f"DRY RUN — would UPSERT config:\n"
            f"  config_key = '{key}'\n"
            f"  value      = '{value}'\n"
            f"  target     = '{target or 'runtime'}'\n\n"
            "Pass confirm=True to execute."
        )

    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                if config.db_type == "mariadb":
                    cur.execute(
                        "INSERT INTO config (config_key, value) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE value = VALUES(value)",
                        (key, value),
                    )
                else:
                    cur.execute(
                        "INSERT INTO config (config_key, value) VALUES (%s, %s) "
                        "ON CONFLICT (config_key) DO UPDATE SET value = EXCLUDED.value",
                        (key, value),
                    )
            conn.commit()
        return f"OK — [{config.name}] config['{key}'] = '{value}'"
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
    target: Optional[str] = None,
) -> str:
    """Insert a new memory entry into the selected target."""
    if not confirm:
        return (
            f"DRY RUN — would insert memory:\n"
            f"  content: {content[:200]}\n"
            f"  author:  {author}\n"
            f"  tags:    {tags}\n"
            f"  scope:   {scope}\n"
            f"  emotion: {emotion}\n"
            f"  target:  {target or 'runtime'}\n\n"
            "Pass confirm=True to execute."
        )

    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO memories (timestamp, content, author, tags, scope, emotion) "
                    "VALUES (NOW(), %s, %s, %s, %s, %s)",
                    (content, author, tags, scope, emotion),
                )
                last_id = getattr(cur, "lastrowid", None)
            conn.commit()
        return f"OK — [{config.name}] inserted memory id={last_id}"
    except Exception as exc:
        return f"DB error: {exc}"


@mcp.tool()
def delete_memory(
    memory_id: int, confirm: bool = False, target: Optional[str] = None
) -> str:
    """Delete a memory entry by ID on the selected target."""
    if not confirm:
        return (
            f"DRY RUN — would DELETE FROM memories WHERE id = {memory_id}\n"
            f"  target = '{target or 'runtime'}'\n\n"
            "Pass confirm=True to execute."
        )

    try:
        config = _resolve_target(target)
        conn = _connect(config.name)
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
                affected = cur.rowcount
            conn.commit()
        if affected == 0:
            return f"No memory found with id={memory_id} on target '{config.name}' (nothing deleted)."
        return f"OK — [{config.name}] deleted memory id={memory_id}"
    except Exception as exc:
        return f"DB error: {exc}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
