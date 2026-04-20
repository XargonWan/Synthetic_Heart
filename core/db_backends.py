from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

try:  # pragma: no cover - import guard
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover - executed when asyncpg missing
    asyncpg = None


_CONFLICT_KEYS: dict[str, tuple[str, ...]] = {
    "agent_action_execs": ("id",),
    "agent_activity_log": ("id",),
    "agent_tasks": ("id",),
    "bio": ("id",),
    "blocklist": ("user_id",),
    "chat_archives": ("id",),
    "chat_history_cache": ("interface_path", "timestamp"),
    "chat_session_meta": ("interface_path",),
    "chatlink": ("interface", "chat_id"),
    "config": ("config_key",),
    "emotion_diary": ("id",),
    "emotion_state": ("id",),
    "external_endpoints": ("name",),
    "grillo_action_execs": ("id",),
    "grillo_activity_log": ("id",),
    "grillo_beats": ("id",),
    "memories": ("id",),
    "message_logs": ("id",),
    "message_map": ("trainer_message_id",),
    "recent_chats": ("chat_id",),
    "scheduled_events": ("id",),
    "settings": ("setting_key",),
}


def postgres_driver_available() -> bool:
    return asyncpg is not None


async def create_postgres_pool(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    minsize: int,
    maxsize: int,
    dsn: str | None = None,
) -> Any:
    if asyncpg is None:
        raise RuntimeError("asyncpg is not installed")

    kwargs: dict[str, Any] = {
        "min_size": minsize,
        "max_size": maxsize,
        "command_timeout": 30,
    }
    if dsn:
        kwargs["dsn"] = dsn
    else:
        kwargs.update(
            {
                "host": host,
                "port": port,
                "user": user,
                "password": password,
                "database": database,
            }
        )
    return await asyncpg.create_pool(**kwargs)


async def probe_postgres_connection(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    dsn: str | None = None,
) -> Any:
    if asyncpg is None:
        raise RuntimeError("asyncpg is not installed")

    kwargs: dict[str, Any] = {}
    if dsn:
        kwargs["dsn"] = dsn
    else:
        kwargs.update(
            {
                "host": host,
                "port": port,
                "user": user,
                "password": password,
                "database": database,
            }
        )
    return await asyncpg.connect(**kwargs)


def _clean_identifier(identifier: str) -> str:
    return identifier.strip().strip('`"')


def _quote_identifier(identifier: str) -> str:
    return f'"{_clean_identifier(identifier)}"'


def _split_identifiers(columns_sql: str) -> list[str]:
    return [_clean_identifier(part) for part in columns_sql.split(",") if part.strip()]


def _replace_mysql_values_refs(update_sql: str) -> str:
    def _repl(match: re.Match[str]) -> str:
        return f"EXCLUDED.{_quote_identifier(match.group(1))}"

    return re.sub(
        r"VALUES\s*\(\s*[`\"]?([A-Za-z_][\w]*)[`\"]?\s*\)",
        _repl,
        update_sql,
        flags=re.IGNORECASE,
    )


def _translate_replace_into(sql: str) -> str:
    match = re.search(
        r"^\s*REPLACE\s+INTO\s+[`\"]?([A-Za-z_][\w]*)[`\"]?\s*\((.*?)\)\s*VALUES\s*\((.*?)\)\s*;?\s*$",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql

    table = _clean_identifier(match.group(1))
    conflict_keys = _CONFLICT_KEYS.get(table)
    if not conflict_keys:
        return sql

    columns = _split_identifiers(match.group(2))
    assignments = [
        f"{_quote_identifier(column)} = EXCLUDED.{_quote_identifier(column)}"
        for column in columns
        if column not in conflict_keys
    ]
    conflict_target = ", ".join(_quote_identifier(key) for key in conflict_keys)
    update_sql = ", ".join(assignments) if assignments else "NOTHING"
    if update_sql == "NOTHING":
        return (
            f"INSERT INTO {_quote_identifier(table)} ({match.group(2)}) VALUES ({match.group(3)}) "
            f"ON CONFLICT ({conflict_target}) DO NOTHING"
        )
    return (
        f"INSERT INTO {_quote_identifier(table)} ({match.group(2)}) VALUES ({match.group(3)}) "
        f"ON CONFLICT ({conflict_target}) DO UPDATE SET {update_sql}"
    )


def _translate_insert_ignore(sql: str) -> str:
    match = re.search(
        r"^\s*INSERT\s+IGNORE\s+INTO\s+[`\"]?([A-Za-z_][\w]*)[`\"]?\s*\((.*?)\)\s*VALUES\s*\((.*?)\)\s*;?\s*$",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql

    table = _clean_identifier(match.group(1))
    conflict_keys = _CONFLICT_KEYS.get(table)
    if not conflict_keys:
        return sql.replace("INSERT IGNORE", "INSERT", 1)

    conflict_target = ", ".join(_quote_identifier(key) for key in conflict_keys)
    return (
        f"INSERT INTO {_quote_identifier(table)} ({match.group(2)}) VALUES ({match.group(3)}) "
        f"ON CONFLICT ({conflict_target}) DO NOTHING"
    )


def _translate_on_duplicate_key(sql: str) -> str:
    match = re.search(
        r"^\s*INSERT\s+INTO\s+[`\"]?([A-Za-z_][\w]*)[`\"]?\s*\((.*?)\)\s*VALUES\s*\((.*?)\)\s*ON\s+DUPLICATE\s+KEY\s+UPDATE\s+(.*?)\s*;?\s*$",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return sql

    table = _clean_identifier(match.group(1))
    conflict_keys = _CONFLICT_KEYS.get(table)
    if not conflict_keys:
        return sql

    update_sql = _replace_mysql_values_refs(match.group(4).strip())
    conflict_target = ", ".join(_quote_identifier(key) for key in conflict_keys)
    return (
        f"INSERT INTO {_quote_identifier(table)} ({match.group(2)}) VALUES ({match.group(3)}) "
        f"ON CONFLICT ({conflict_target}) DO UPDATE SET {update_sql}"
    )


def _strip_mysql_indexes(sql: str) -> str:
    stripped_lines: list[str] = []
    for line in sql.splitlines():
        raw = line.rstrip()
        compact = raw.strip()
        if not compact:
            stripped_lines.append(raw)
            continue
        if re.match(r"(?i)INDEX\s+", compact) or re.match(r"(?i)KEY\s+", compact):
            continue
        unique_match = re.match(
            r"(?i)UNIQUE\s+KEY\s+[A-Za-z_][\w]*\s*\((.+)\)\s*,?",
            compact,
        )
        if unique_match:
            indent = raw[: len(raw) - len(raw.lstrip())]
            stripped_lines.append(f"{indent}UNIQUE ({unique_match.group(1)}),")
            continue
        stripped_lines.append(raw)

    translated = "\n".join(stripped_lines)
    return re.sub(r",\s*\)", "\n)", translated, flags=re.DOTALL)


def _translate_create_table(sql: str) -> str:
    translated = sql
    translated = translated.replace("`", '"')
    translated = re.sub(r"\bLONGTEXT\b", "TEXT", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bMEDIUMTEXT\b", "TEXT", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bDATETIME\b", "TIMESTAMPTZ", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"\bTIMESTAMP\b", "TIMESTAMPTZ", translated, flags=re.IGNORECASE
    )
    translated = re.sub(
        r"\bDOUBLE\b", "DOUBLE PRECISION", translated, flags=re.IGNORECASE
    )
    translated = re.sub(r"\bJSON\b", "JSONB", translated, flags=re.IGNORECASE)
    translated = re.sub(r"\bENUM\s*\([^\)]*\)", "TEXT", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"\bTINYINT\s*\(\s*1\s*\)", "BOOLEAN", translated, flags=re.IGNORECASE
    )
    translated = re.sub(r"\bTINYINT\b", "INTEGER", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"\bBIGINT\s+AUTO_INCREMENT\s+PRIMARY\s+KEY\b",
        "BIGSERIAL PRIMARY KEY",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bINT\s+AUTO_INCREMENT\s+PRIMARY\s+KEY\b",
        "SERIAL PRIMARY KEY",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bBIGINT\s+AUTO_INCREMENT\b",
        "BIGINT GENERATED BY DEFAULT AS IDENTITY",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bINT\s+AUTO_INCREMENT\b",
        "INTEGER GENERATED BY DEFAULT AS IDENTITY",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bON\s+UPDATE\s+CURRENT_TIMESTAMP\b", "", translated, flags=re.IGNORECASE
    )
    translated = re.sub(r"\s+COMMENT\s+'[^']*'", "", translated, flags=re.IGNORECASE)
    translated = re.sub(
        r"\)\s*ENGINE\s*=\s*\w+[^;]*", ")", translated, flags=re.IGNORECASE | re.DOTALL
    )
    translated = re.sub(
        r"\bBOOLEAN\s+DEFAULT\s+0\b",
        "BOOLEAN DEFAULT FALSE",
        translated,
        flags=re.IGNORECASE,
    )
    translated = re.sub(
        r"\bBOOLEAN\s+DEFAULT\s+1\b",
        "BOOLEAN DEFAULT TRUE",
        translated,
        flags=re.IGNORECASE,
    )
    return _strip_mysql_indexes(translated)


def _translate_update_set_alias(sql: str) -> str:
    return re.sub(
        r"(\bSET\s+)([A-Za-z_][\w]*)\.",
        r"\1",
        sql,
        count=1,
        flags=re.IGNORECASE,
    )


def _translate_placeholders(sql: str) -> str:
    counter = 0

    def _repl(_: re.Match[str]) -> str:
        nonlocal counter
        counter += 1
        return f"${counter}"

    return re.sub(r"%s", _repl, sql)


def _translate_mysql_functions(sql: str) -> str:
    # JSON_LENGTH(col) → jsonb_array_length((col)::jsonb)
    sql = re.sub(
        r"\bJSON_LENGTH\s*\(([^)]+)\)",
        lambda m: f"jsonb_array_length(({m.group(1).strip()})::jsonb)",
        sql,
        flags=re.IGNORECASE,
    )
    # JSON_EXTRACT(col, '$[N].key') → (col)::jsonb->N->>'key'
    sql = re.sub(
        r"\bJSON_EXTRACT\s*\(\s*([^,]+),\s*'\$\[(\d+)\]\.(\w+)'\s*\)",
        lambda m: f"({m.group(1).strip()})::jsonb->{m.group(2)}->>'{m.group(3)}'",
        sql,
        flags=re.IGNORECASE,
    )
    # GROUP_CONCAT(col ORDER BY x ASC/DESC SEPARATOR 'sep')
    # → string_agg(col, 'sep' ORDER BY x ASC/DESC)
    sql = re.sub(
        r"\bGROUP_CONCAT\s*\(\s*(\w+)\s+ORDER\s+BY\s+(\w+)\s+(ASC|DESC)\s+SEPARATOR\s+'([^']*)'\s*\)",
        lambda m: (
            f"string_agg({m.group(1)}, '{m.group(4)}' ORDER BY {m.group(2)} {m.group(3)})"
        ),
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def translate_postgres_sql(sql: str) -> str:
    translated = (sql or "").strip()
    translated = translated.replace("UTC_TIMESTAMP()", "CURRENT_TIMESTAMP")
    translated = translated.replace("CURDATE()", "CURRENT_DATE")
    translated = re.sub(r"\bIFNULL\s*\(", "COALESCE(", translated, flags=re.IGNORECASE)
    translated = _translate_mysql_functions(translated)
    translated = _translate_insert_ignore(translated)
    translated = _translate_replace_into(translated)
    translated = _translate_on_duplicate_key(translated)
    translated = _translate_update_set_alias(translated)
    translated = translated.replace("`", '"')
    if re.match(r"^\s*CREATE\s+TABLE", translated, flags=re.IGNORECASE):
        translated = _translate_create_table(translated)
    translated = _translate_placeholders(translated)
    # Unescape %% → % (aiomysql uses %% for literal percent; Postgres uses $N params)
    translated = translated.replace("%%", "%")
    return re.sub(r";\s*$", "", translated)


def statement_returns_rows(sql: str) -> bool:
    compact = (sql or "").strip().upper()
    if not compact:
        return False
    if compact.startswith(("SELECT", "SHOW", "DESCRIBE", "WITH")):
        return True
    return " RETURNING " in f" {compact} "


def _extract_rowcount(command_tag: str | None) -> int:
    if not command_tag:
        return -1
    try:
        return int(str(command_tag).split()[-1])
    except Exception:
        return -1


def _normalize_params(params: Any) -> tuple[Any, ...]:
    if params is None:
        return ()
    if isinstance(params, tuple):
        return params
    if isinstance(params, list):
        return tuple(params)
    return (params,)


def _dict_cursor_requested(args: Sequence[Any], kwargs: dict[str, Any]) -> bool:
    cursor_type = kwargs.get("cursor")
    if cursor_type is not None and getattr(cursor_type, "__name__", "") == "DictCursor":
        return True
    for arg in args:
        if getattr(arg, "__name__", "") == "DictCursor":
            return True
    return False


class PostgresCompatCursor:
    def __init__(self, conn: Any, *, dict_mode: bool = False) -> None:
        self._conn = conn
        self._dict_mode = dict_mode
        self._rows: list[Any] = []
        self.lastrowid: Any = None
        self.rowcount = -1

    async def execute(self, query: str, params: Any = None) -> None:
        translated = translate_postgres_sql(query)
        bound = _normalize_params(params)
        if statement_returns_rows(translated):
            rows = await self._conn.fetch(translated, *bound)
            self._rows = list(rows or [])
            self.rowcount = len(self._rows)
            if self._rows:
                first = self._rows[0]
                try:
                    self.lastrowid = first["id"]
                except Exception:
                    self.lastrowid = None
            return

        command_tag = await self._conn.execute(translated, *bound)
        self._rows = []
        self.rowcount = _extract_rowcount(command_tag)
        self.lastrowid = None

    async def executemany(self, query: str, params_seq: Iterable[Any]) -> None:
        translated = translate_postgres_sql(query)
        normalized = [_normalize_params(params) for params in params_seq]
        await self._conn.executemany(translated, normalized)
        self._rows = []
        self.lastrowid = None
        self.rowcount = len(normalized)

    def _coerce_row(self, row: Any) -> Any:
        if row is None or not self._dict_mode:
            return row
        try:
            return dict(row)
        except Exception:
            return row

    async def fetchone(self) -> Any:
        if not self._rows:
            return None
        row = self._rows.pop(0)
        return self._coerce_row(row)

    async def fetchall(self) -> list[Any]:
        rows = [self._coerce_row(row) for row in self._rows]
        self._rows = []
        return rows

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> "PostgresCompatCursor":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class PostgresCompatConnection:
    def __init__(self, conn: Any, pool: Any) -> None:
        self._conn = conn
        self._pool = pool
        self._released = False

    def __getattr__(self, item: str) -> Any:
        return getattr(self._conn, item)

    def close(self) -> Any:
        if self._released:
            return None
        self._released = True
        release_fn = getattr(self._pool, "release", None)
        if callable(release_fn):
            return release_fn(self._conn)

        close_fn = getattr(self._conn, "close", None)
        if callable(close_fn):
            return close_fn()
        return None

    async def aclose(self) -> None:
        import inspect

        result = self.close()
        if inspect.isawaitable(result):
            await result

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        dict_mode = _dict_cursor_requested(args, kwargs)

        async def _make_cursor() -> PostgresCompatCursor:
            return PostgresCompatCursor(self._conn, dict_mode=dict_mode)

        return _make_cursor()
