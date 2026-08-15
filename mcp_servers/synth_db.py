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
from datetime import datetime, timezone
from pathlib import Path
import gzip
import os
import re
import shutil
import socket
import subprocess
from typing import Any, Optional
from urllib.parse import unquote, urlparse

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
_PRIMARY_DB_TARGET_ALIASES = {
    "memory": "memory",
    "mariadb": "memory",
    "mysql": "memory",
    "soul": "soul",
    "postgres": "soul",
    "postgresql": "soul",
}


def _strip_wrapping_quotes(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}:
        return stripped[1:-1]
    # Drop inline comments on unquoted values (e.g. ``4306   # external port``).
    hash_index = stripped.find("#")
    if hash_index != -1:
        stripped = stripped[:hash_index].strip()
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


def _infer_db_type_from_port(port_value: str | None) -> str | None:
    """Infer the DB engine from a well-known port when the type is undeclared.

    5432 -> postgres, 3306 -> mariadb. Returns None for anything else so the
    caller keeps its explicit default.
    """
    try:
        port = int(str(port_value).strip())
    except (TypeError, ValueError):
        return None
    if port == 5432:
        return "postgres"
    if port == 3306:
        return "mariadb"
    return None


def _normalize_primary_db_target(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    return _PRIMARY_DB_TARGET_ALIASES.get(normalized)


def _parse_dsn_components(
    dsn: str | None,
) -> tuple[str | None, int | None, str | None, str | None, str | None]:
    if not dsn:
        return None, None, None, None, None

    parsed = urlparse(dsn)
    database = parsed.path.lstrip("/") or None
    return (
        parsed.hostname,
        parsed.port,
        unquote(parsed.username) if parsed.username else None,
        unquote(parsed.password) if parsed.password else None,
        database,
    )


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


def _build_runtime_db_target(
    *, forced_db_type: str | None = None, include_dsn: bool = True
) -> DbTarget:
    declared_type = _repo_or_process_value(
        "SYNTH_DB_TYPE", _repo_or_process_value("DB_TYPE")
    )
    # The runtime DB moved to PostgreSQL (see core/db.py), so when no engine is
    # declared and no well-known port is present we default to postgres rather
    # than the legacy mariadb default. Explicit config still wins.
    inferred_default = (
        _infer_db_type_from_port(_repo_or_process_value("DB_PORT")) or "postgres"
    )
    db_type = forced_db_type or _normalize_db_type(
        declared_type, default=inferred_default
    )
    default_port = 5432 if db_type == "postgres" else 3306
    dsn = None
    if include_dsn:
        dsn = (
            str(
                _repo_or_process_value(
                    "DATABASE_URL", _repo_or_process_value("DB_DSN", "")
                )
                or ""
            )
            or None
        )

    # Default the host to the Docker service name used by docker-compose
    # (``synth-db``). _remap_for_host_access rewrites it to 127.0.0.1:<EXT_DB_PORT>
    # when running on the host where the service name does not resolve, so the
    # same default works both inside the container and from the host.
    return DbTarget(
        name="runtime",
        db_type=db_type,
        host=str(_repo_or_process_value("DB_HOST", "synth-db") or "synth-db"),
        port=_coerce_port(_repo_or_process_value("DB_PORT"), default_port),
        user=str(_repo_or_process_value("DB_USER", "synth") or "synth"),
        password=str(_repo_or_process_value("DB_PASS", "synth") or "synth"),
        database=str(_repo_or_process_value("DB_NAME", "synth") or "synth"),
        dsn=dsn,
    )


def _resolve_soul_dsn(*, allow_runtime_fallback: bool = False) -> str | None:
    dsn = str(_repo_or_process_value("SOUL_POSTGRES_DSN", "") or "") or None
    if dsn or not allow_runtime_fallback:
        return dsn
    return (
        str(
            _repo_or_process_value("DATABASE_URL", _repo_or_process_value("DB_DSN", ""))
            or ""
        )
        or None
    )


def _build_soul_target_config(
    *, name: str = "soul", allow_runtime_fallback: bool = False
) -> DbTarget | None:
    soul_dsn = _resolve_soul_dsn(allow_runtime_fallback=allow_runtime_fallback)
    if not soul_dsn and not any(
        _repo_or_process_value(key)
        for key in ("SOUL_PG_DB", "SOUL_PG_USER", "SOUL_PG_HOST", "SOUL_PG_PORT")
    ):
        return None

    dsn_host, dsn_port, dsn_user, dsn_pass, dsn_db = _parse_dsn_components(soul_dsn)
    return DbTarget(
        name=name,
        db_type="postgres",
        host=str(
            _repo_or_process_value("SOUL_PG_HOST", dsn_host or "localhost")
            or dsn_host
            or "localhost"
        ),
        port=_coerce_port(_repo_or_process_value("SOUL_PG_PORT"), dsn_port or 5432),
        user=str(
            _repo_or_process_value("SOUL_PG_USER", dsn_user or "soul")
            or dsn_user
            or "soul"
        ),
        password=str(
            _repo_or_process_value(
                "SOUL_PG_PASSWORD",
                _repo_or_process_value("SOUL_PG_PASS", dsn_pass or "soul"),
            )
            or dsn_pass
            or "soul"
        ),
        database=str(
            _repo_or_process_value("SOUL_PG_DB", dsn_db or "soul") or dsn_db or "soul"
        ),
        dsn=soul_dsn,
    )


def _build_runtime_target() -> DbTarget:
    primary_db_target = _normalize_primary_db_target(
        _repo_or_process_value("SYNTH_PRIMARY_DB")
    )
    if primary_db_target == "soul":
        soul_target = _build_soul_target_config(
            name="runtime", allow_runtime_fallback=True
        )
        if soul_target is not None:
            return soul_target
    if primary_db_target == "memory":
        return _build_runtime_db_target(forced_db_type="mariadb", include_dsn=False)
    return _build_runtime_db_target()


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
    return _build_soul_target_config(name="soul")


def _build_process_env_target() -> DbTarget | None:
    if not any(
        _process_value(key)
        for key in (
            "SYNTH_PRIMARY_DB",
            "DB_TYPE",
            "DB_HOST",
            "DB_PORT",
            "DB_USER",
            "DB_NAME",
            "DATABASE_URL",
            "SOUL_POSTGRES_DSN",
            "SOUL_PG_HOST",
            "SOUL_PG_PORT",
            "SOUL_PG_USER",
            "SOUL_PG_DB",
        )
    ):
        return None

    primary_db_target = _normalize_primary_db_target(_process_value("SYNTH_PRIMARY_DB"))
    if primary_db_target == "memory":
        db_type = "mariadb"
    elif primary_db_target == "soul":
        db_type = "postgres"
    else:
        db_type = _normalize_db_type(
            _process_value("SYNTH_DB_TYPE", _process_value("DB_TYPE", "mariadb"))
        )
    default_port = 5432 if db_type == "postgres" else 3306
    process_dsn = (
        str(_process_value("DATABASE_URL", _process_value("DB_DSN", "")) or "") or None
    )
    if primary_db_target == "memory":
        process_dsn = None
    elif primary_db_target == "soul":
        process_dsn = (
            str(
                _process_value(
                    "SOUL_POSTGRES_DSN",
                    _process_value("DATABASE_URL", _process_value("DB_DSN", "")),
                )
                or ""
            )
            or None
        )

    dsn_host, dsn_port, dsn_user, dsn_pass, dsn_db = _parse_dsn_components(process_dsn)
    if primary_db_target == "soul":
        host = str(
            _process_value("SOUL_PG_HOST", dsn_host or "localhost")
            or dsn_host
            or "localhost"
        )
        port = _coerce_port(_process_value("SOUL_PG_PORT"), dsn_port or default_port)
        user = str(
            _process_value("SOUL_PG_USER", dsn_user or "soul") or dsn_user or "soul"
        )
        password = str(
            _process_value(
                "SOUL_PG_PASSWORD",
                _process_value("SOUL_PG_PASS", dsn_pass or "soul"),
            )
            or dsn_pass
            or "soul"
        )
        database = str(
            _process_value("SOUL_PG_DB", dsn_db or "soul") or dsn_db or "soul"
        )
    else:
        host = str(_process_value("DB_HOST", "localhost") or "localhost")
        port = _coerce_port(_process_value("DB_PORT"), default_port)
        user = str(_process_value("DB_USER", "synth") or "synth")
        password = str(_process_value("DB_PASS", "synth") or "synth")
        database = str(_process_value("DB_NAME", "synth") or "synth")

    return DbTarget(
        name="process_env",
        db_type=db_type,
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        dsn=process_dsn,
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


def _running_inside_container() -> bool:
    """Best-effort detection of whether this process runs inside the Synth container.

    When true, Docker-internal service hostnames (e.g. ``synth-db``) resolve and
    must be used as-is. When false (running on the host), those hostnames are not
    resolvable and connections must go through the published port on localhost.
    """
    return Path("/.dockerenv").exists()


def _hostname_resolvable(hostname: str) -> bool:
    if not hostname:
        return False
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False


def _host_exposed_port(target: DbTarget) -> int:
    """Return the host-published port for a Docker-internal DB target."""
    if target.db_type == "postgres":
        return _coerce_port(_repo_or_process_value("EXT_DB_PORT"), target.port)
    return _coerce_port(_repo_or_process_value("EXT_DB_PORT"), target.port)


def _remap_dsn_for_host_access(dsn: str, host: str, port: int) -> str:
    parsed = urlparse(dsn)
    userinfo = ""
    if parsed.username:
        userinfo = unquote(parsed.username)
        if parsed.password:
            userinfo += f":{unquote(parsed.password)}"
        userinfo += "@"
    rebuilt = parsed._replace(netloc=f"{userinfo}{host}:{port}")
    return rebuilt.geturl()


def _remap_for_host_access(target: DbTarget) -> DbTarget:
    """Rewrite Docker-internal hostnames to localhost when running on the host.

    Inside the container the service hostname (``synth-db``) resolves and is left
    untouched. On the host it does not resolve, so we fall back to ``127.0.0.1``
    and the published port (``EXT_DB_PORT``) so the MCP server can reach the DB.
    """
    if _running_inside_container():
        return target
    if _hostname_resolvable(target.host):
        return target

    new_host = "127.0.0.1"
    new_port = _host_exposed_port(target)
    new_dsn = target.dsn
    if new_dsn:
        new_dsn = _remap_dsn_for_host_access(new_dsn, new_host, new_port)

    return DbTarget(
        name=target.name,
        db_type=target.db_type,
        host=new_host,
        port=new_port,
        user=target.user,
        password=target.password,
        database=target.database,
        dsn=new_dsn,
    )


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
    return _remap_for_host_access(targets[resolved_name])


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
        "created_at",
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
    target = _remap_for_host_access(target)
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
        lines.append(f"- {_target_summary(_remap_for_host_access(targets[name]))}")
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
                    f"SELECT id, created_at, content, author, tags, scope, emotion, intensity "
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

    This helper now uses the canonical `created_at` column and dynamically limits
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
                    "SELECT id, sender_name, sender_id, message_text, created_at "
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
                    "INSERT INTO memories (created_at, content, author, tags, scope, emotion) "
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


# ---------------------------------------------------------------------------
# Backups (shell out to pg_dump / mysqldump, gzip-compressed output)
# ---------------------------------------------------------------------------

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _backups_dir() -> Path:
    raw = os.getenv("SYNTH_BACKUPS_DIR", str(_REPO_ROOT / "backups"))
    return Path(raw)


def _sanitize_table_names(tables: list[str]) -> list[str]:
    """Validate + de-duplicate table identifiers, raising on anything unsafe."""
    seen: set[str] = set()
    clean: list[str] = []
    for raw in tables:
        name = str(raw).strip()
        if not name:
            continue
        if not _TABLE_NAME_RE.match(name):
            raise ValueError(f"Invalid table name: {name!r}")
        if name not in seen:
            seen.add(name)
            clean.append(name)
    if not clean:
        raise ValueError("No valid table names provided.")
    return clean


def _dump_binary(db_type: str) -> str | None:
    tool = "pg_dump" if db_type == "postgres" else "mysqldump"
    return shutil.which(tool)


def _build_dump_command(
    config: DbTarget, tables: list[str] | None
) -> tuple[list[str], dict[str, str]]:
    """Return (argv, env additions) for a pg_dump / mysqldump invocation."""
    env: dict[str, str] = {}
    binary = _dump_binary(config.db_type)
    if not binary:
        want = "pg_dump" if config.db_type == "postgres" else "mysqldump"
        raise ValueError(f"{want} not found on PATH.")

    if config.db_type == "postgres":
        cmd = [
            binary,
            "--host",
            str(config.host),
            "--port",
            str(config.port),
            "--username",
            str(config.user),
            "--dbname",
            str(config.database),
            "--format=plain",
            "--no-owner",
            "--no-privileges",
            "--encoding=UTF8",
        ]
        for tbl in tables or []:
            cmd += ["--table", tbl]
        env["PGPASSWORD"] = config.password or ""
        return cmd, env

    if config.db_type == "mariadb":
        cmd = [
            binary,
            "--host",
            str(config.host),
            "--port",
            str(config.port),
            "--user",
            str(config.user),
            "--single-transaction",
            "--quick",
            "--routines",
            "--triggers",
            "--skip-lock-tables",
            str(config.database),
        ]
        cmd += tables or []
        env["MYSQL_PWD"] = config.password or ""
        return cmd, env

    raise ValueError(f"Unsupported DB_TYPE for backup: {config.db_type}")


def _run_dump(config: DbTarget, tables: list[str] | None, prefix: str) -> Path:
    cmd, env_add = _build_dump_command(config, tables)
    out_dir = _backups_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{prefix}-{config.database}-{stamp}.sql.gz"

    run_env = dict(os.environ)
    run_env.update(env_add)
    proc = subprocess.run(
        cmd,
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"dump failed (exit {proc.returncode}): {err[:500]}")
    with gzip.open(out_path, "wb") as fh:
        fh.write(proc.stdout)
    return out_path


@mcp.tool()
def backup_database(
    confirm: bool = False,
    target: Optional[str] = None,
) -> str:
    """Create a gzip-compressed full logical backup of the selected DB target.

    Shells out to pg_dump (Postgres) or mysqldump (MariaDB) and writes a
    ``.sql.gz`` file into the backups directory (``SYNTH_BACKUPS_DIR``,
    default ``backups/``).

    Args:
        confirm: Pass True to actually run the backup (dry-run preview otherwise).
        target: Optional DB target selector (default runtime).
    """
    if not confirm:
        return (
            "DRY RUN — would create a FULL database backup\n"
            f"  target  = '{target or 'runtime'}'\n"
            f"  out dir = {_backups_dir()}\n\n"
            "Pass confirm=True to execute."
        )
    try:
        config = _resolve_target(target)
        config = _remap_for_host_access(config)
        path = _run_dump(config, None, f"runtime-{config.db_type}")
        return f"OK — [{config.name}] backup written: {path}"
    except Exception as exc:
        return f"Backup error: {exc}"


@mcp.tool()
def backup_table(
    tables: list[str],
    confirm: bool = False,
    target: Optional[str] = None,
) -> str:
    """Create a gzip-compressed backup of one or more specific tables.

    Args:
        tables: A LIST of table names to include in the backup.
        confirm: Pass True to actually run the backup (dry-run preview otherwise).
        target: Optional DB target selector (default runtime).
    """
    try:
        clean = _sanitize_table_names(list(tables or []))
    except ValueError as exc:
        return f"Backup rejected: {exc}"

    if not confirm:
        return (
            "DRY RUN — would create a per-table backup\n"
            f"  tables  = {clean}\n"
            f"  target  = '{target or 'runtime'}'\n"
            f"  out dir = {_backups_dir()}\n\n"
            "Pass confirm=True to execute."
        )
    try:
        config = _resolve_target(target)
        config = _remap_for_host_access(config)
        path = _run_dump(config, clean, f"runtime-{config.db_type}-tables")
        return (
            f"OK — [{config.name}] table backup written ({len(clean)} table(s)): {path}"
        )
    except Exception as exc:
        return f"Backup error: {exc}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
