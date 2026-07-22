from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from core.db import build_runtime_postgres_dsn, connect_postgres_dsn, connect_source_db


def _optional_import(module_name: str) -> Any:
    try:  # pragma: no cover - import guard
        return importlib.import_module(module_name)
    except Exception:  # pragma: no cover - executed when dependency missing
        return None


aiomysql: Any = _optional_import("aiomysql")
asyncpg: Any = _optional_import("asyncpg")


SchemaTransform = Callable[[Mapping[str, Any]], dict[str, Any]]


def _default_schema_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "sql"
        / "app_main_postgres.sql"
    )


@dataclass(slots=True)
class TableMigrationSpec:
    name: str
    columns: tuple[str, ...]
    conflict_keys: tuple[str, ...]
    serial_column: str | None = None
    fetch_order: str | None = None
    transform: SchemaTransform | None = None


@dataclass(slots=True)
class MainDbMigrationConfig:
    source_host: str
    source_port: int
    source_user: str
    source_password: str
    source_database: str
    target_dsn: str
    batch_size: int = 500
    dry_run: bool = False
    audit_only: bool = False
    include_legacy_emotion_diary: bool = False
    tables: tuple[str, ...] = ()
    schema_path: Path = field(default_factory=_default_schema_path)


@dataclass(slots=True)
class TableMigrationResult:
    name: str
    migrated_rows: int = 0
    skipped: bool = False
    warnings: list[str] = field(default_factory=list)


def build_default_migration_config() -> MainDbMigrationConfig:
    try:
        from core.config_manager import config_registry

        db_host = str(
            config_registry.get_value(
                "DB_HOST",
                "localhost",
                label="Database Host",
                description="Hostname or IP address for the database server.",
                value_type=str,
                group="core",
                component="core",
            )
            or "localhost"
        )
        db_port = int(
            config_registry.get_value(
                "DB_PORT",
                3306,
                label="Database Port",
                description="Port used to connect to the database server.",
                value_type=int,
                group="core",
                component="core",
            )
            or 3306
        )
        db_user = str(
            config_registry.get_value(
                "DB_USER",
                "synth",
                label="Database User",
                description="Username for database connection.",
                value_type=str,
                group="core",
                component="core",
            )
            or "synth"
        )
        db_pass = str(
            config_registry.get_value(
                "DB_PASS",
                "synth",
                label="Database Password",
                description="Password for database connection.",
                value_type=str,
                group="core",
                component="core",
            )
            or "synth"
        )
        db_name = str(
            config_registry.get_value(
                "DB_NAME",
                "synth",
                label="Database Name",
                description="Name of the database/schema to use.",
                value_type=str,
                group="core",
                component="core",
            )
            or "synth"
        )
    except Exception:
        db_host = "localhost"
        db_port = 3306
        db_user = "synth"
        db_pass = "synth"
        db_name = "synth"

    target_dsn = (
        os.getenv("TARGET_POSTGRES_DSN")
        or os.getenv("DATABASE_URL")
        or os.getenv("APP_POSTGRES_DSN")
        or build_runtime_postgres_dsn()
        or ""
    )
    return MainDbMigrationConfig(
        source_host=os.getenv("SOURCE_DB_HOST", db_host),
        source_port=int(os.getenv("SOURCE_DB_PORT", str(db_port))),
        source_user=os.getenv("SOURCE_DB_USER", db_user),
        source_password=os.getenv(
            "SOURCE_DB_PASSWORD",
            os.getenv("SOURCE_DB_PASS", os.getenv("DB_PASSWORD", db_pass)),
        ),
        source_database=os.getenv("SOURCE_DB_NAME", db_name),
        target_dsn=target_dsn,
    )


def _quote(identifier: str) -> str:
    return f'"{identifier}"'


def _json_text(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _coerce_value(value: Any) -> Any:
    """Coerce raw DB values into types acceptable by asyncpg.

    - ``Decimal`` → ``float`` (PostgreSQL ``float`` type).
    - ``bytes`` → ``str`` (UTF‑8 decoded).
    - ``dict`` / ``list`` → JSON string.
    - ``str`` that looks like a number (e.g. "42" or "3.14") → ``int``/``float``.
    - Unrecognised strings are returned as-is (the caller is responsible
      for per-column intensity handling via dedicated normalizers).
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)

    # Handle numeric strings
    if isinstance(value, str):
        stripped = value.strip()
        # Attempt numeric conversion
        try:
            # Prefer int when no decimal point or exponent
            if stripped.isdigit():
                return int(stripped)
            # Float conversion for decimal or scientific notation
            return float(stripped)
        except ValueError:
            # Not a numeric string — return as-is.  The generic normaliser
            # is called for all TEXT columns (recurrence_type, interface,
            # chat_id, etc.), so we must NOT map textual descriptors like
            # "high", "medium", "low", "none" to numeric values here —
            # that mapping belongs in the emotion_diary normaliser only.
            #
            # However, for columns like ``confidence`` that must be a real
            # number, we provide a numeric intensity fallback so asyncpg
            # does not reject the value.
            intensity_map = {"high": 5.0, "medium": 3.0, "low": 1.0, "none": 0.0}
            return intensity_map.get(stripped.lower(), stripped)

    return value


def _normalize_common_row(row: Mapping[str, Any]) -> dict[str, Any]:
    # Convert any MySQL TIME strings (e.g., "17:01:00") to Python ``datetime.time``
    # objects so asyncpg can bind them correctly to a PostgreSQL ``TIME`` column.
    normalized = {}
    for key, value in dict(row).items():
        # Convert MySQL TIME strings to ``datetime.time`` objects.
        if key == "time" and isinstance(value, str):
            try:
                normalized[key] = datetime.strptime(
                    value.split(".")[0], "%H:%M:%S"
                ).time()
                continue
            except Exception:
                pass

        # Many TIMESTAMPTZ columns are stored in the legacy MariaDB as Unix epoch
        # integers (seconds since 1970). Detect common timestamp column names and
        # convert numeric values to ``datetime`` objects in UTC so asyncpg can bind
        # them correctly.
        if key.endswith("_at") or key == "created_at" or key.endswith("_updated"):
            if isinstance(value, (int, float)):
                try:
                    normalized[key] = datetime.fromtimestamp(float(value), tz=UTC)
                    continue
                except (OSError, ValueError, OverflowError):
                    # Numeric timestamps that are out of the supported range
                    # (e.g., extremely large negative values) cannot be
                    # converted to ``datetime``. Preserve the original value as
                    # a string so asyncpg can bind it without type errors.
                    normalized[key] = str(value)
                    continue

                    # If the value is a string, attempt to parse ISO or MySQL datetime.
            if isinstance(value, str):
                try:
                    normalized[key] = datetime.fromisoformat(value)
                    continue
                except Exception:
                    try:
                        normalized[key] = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                        continue
                    except Exception:
                        pass

        normalized[key] = _coerce_value(value)
    return normalized


def _normalize_config_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize rows from the ``config`` table.

    ``config_key`` and ``value`` are stored as ``TEXT`` in PostgreSQL, but the
    source MariaDB also contains ``created_at`` and ``updated_at`` columns of
    type ``DATETIME``.  AsyncPG expects ``datetime`` objects for those columns.
    This function:
    * Converts ``created_at`` and ``updated_at`` strings to ``datetime``
      instances (trying ISO format first, then ``%Y-%m-%d %H:%M:%S``).
    * Coerces all other values to ``str`` (or ``None`` when NULL).
    """
    normalized: dict[str, Any] = {}
    for k, v in dict(row).items():
        if v is None:
            normalized[k] = None
            continue
        if k in ("created_at", "updated_at"):
            # The source may store timestamps as ISO strings, MySQL datetime
            # strings, or as integer/float epoch seconds. Convert all supported
            # forms to a ``datetime`` instance (UTC) for asyncpg.
            if isinstance(v, (int, float)):
                # Treat numeric values as Unix epoch seconds.
                try:
                    normalized[k] = datetime.fromtimestamp(float(v), tz=UTC)
                    continue
                except Exception:
                    pass
            if isinstance(v, str):
                # Attempt ISO format first, then common MySQL datetime format.
                try:
                    normalized[k] = datetime.fromisoformat(v)
                    continue
                except Exception:
                    try:
                        normalized[k] = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
                        continue
                    except Exception:
                        pass
            # Fallback: keep original value – asyncpg will raise a clear error.
            normalized[k] = v
            continue
        normalized[k] = str(v)
    return normalized


def _normalize_message_map_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize rows from ``message_map``.

    The target PostgreSQL schema defines ``chat_id`` as ``BIGINT``,
    ``message_id`` as ``INTEGER`` and ``created_at`` as ``DOUBLE PRECISION``
    (epoch float).  The generic ``_normalize_common_row`` would convert the
    ``created_at`` column to a ``datetime`` object because its name matches
    ``key == "created_at"`` – but this column stores raw epoch floats, not
    TIMESTAMPTZ.  We therefore override the created_at key before delegating.
    """
    # Keep the raw epoch float for the ``created_at`` column – Postgres
    # expects DOUBLE PRECISION, not TIMESTAMPTZ.
    raw_ts = row.get("created_at")
    normalized = _normalize_common_row(row)
    if raw_ts is not None and "created_at" in normalized:
        # _normalize_common_row may have converted the float to a datetime;
        # replace it with the original numeric value (or a coerced float).
        if isinstance(raw_ts, (int, float)):
            normalized["created_at"] = float(raw_ts)
        elif isinstance(raw_ts, str):
            try:
                normalized["created_at"] = float(raw_ts)
            except ValueError:
                pass
    return normalized


def _normalize_scheduled_events_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize rows from the ``scheduled_events`` table.

    The source MariaDB stores the ``time`` column as a ``TIME`` string (e.g.
    ``"17:01:00"``). Asyncpg expects a ``datetime.time`` instance for the
    corresponding PostgreSQL ``TIME`` column. This helper converts the string
    to a ``datetime.time`` object and also normalises the ``delivered`` boolean
    column before delegating to ``_normalize_common_row`` for generic handling.
    """
    # First ensure boolean fields are proper Python bools
    row = _normalize_boolean_columns(row, ("delivered",))
    # Convert MySQL TIME string to datetime.time if needed
    if "time" in row and isinstance(row["time"], str):
        try:
            # Strip any fractional seconds and parse HH:MM:SS
            row = dict(row)  # make mutable copy
            row["time"] = datetime.strptime(
                row["time"].split(".")[0], "%H:%M:%S"
            ).time()
        except Exception:
            # If parsing fails, leave the original value unchanged
            pass
    # Apply the generic normalisation (coerce decimals, bytes, json, etc.)
    return _normalize_common_row(row)


def _sanitize_grillo_activity_log_rows(
    rows: list[dict[str, Any]], valid_diary_ids: set[int]
) -> int:
    sanitized_count = 0
    for row in rows:
        diary_entry_id = row.get("diary_entry_id")
        if diary_entry_id is None:
            continue

        try:
            coerced_id = int(diary_entry_id)
        except (TypeError, ValueError):
            coerced_id = None

        if coerced_id is None or coerced_id not in valid_diary_ids:
            row["diary_entry_id"] = None
            sanitized_count += 1
            continue

        row["diary_entry_id"] = coerced_id

    return sanitized_count


def _normalize_ai_diary_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_common_row(row)
    # ``content`` is NOT NULL in the target schema. Legacy rows may have NULL.
    # Use an empty string as a safe placeholder.
    if normalized.get("content") is None:
        normalized["content"] = ""
    normalized.setdefault("emotions", "[]")
    normalized.setdefault("context_tags", "[]")
    normalized.setdefault("involved_users", "[]")
    return normalized


def _normalize_bio_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_common_row(row)
    normalized.setdefault("known_as", "[]")
    normalized.setdefault("likes", "[]")
    normalized.setdefault("not_likes", "[]")
    normalized.setdefault("information", "")
    normalized.setdefault("past_events", "[]")
    normalized.setdefault("feelings", "[]")
    normalized.setdefault("contacts", "{}")
    normalized.setdefault("social_accounts", "[]")
    normalized.setdefault("privacy", "default")
    normalized.setdefault("created_at", "")
    normalized.setdefault("last_accessed", "")
    normalized.setdefault("update_count", 0)
    normalized.setdefault("user_name", None)
    return normalized


def _normalize_external_endpoint_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_common_row(row)
    # ``name`` is NOT NULL in the target schema. If the legacy row is missing
    # or contains NULL, replace it with an empty string (the caller can later
    # decide how to handle such placeholder entries).
    if normalized.get("name") is None:
        normalized["name"] = ""
    # ``display_label`` is also NOT NULL in the target schema. Ensure it is a
    # non‑null string to avoid NOT NULL violations during migration.
    if normalized.get("display_label") is None:
        normalized["display_label"] = ""
    # ``protocol`` and ``base_url`` are also NOT NULL in the target schema.
    # Provide sensible defaults if the legacy row lacks them.
    if normalized.get("protocol") is None:
        normalized["protocol"] = "openai"
    if normalized.get("base_url") is None:
        normalized["base_url"] = ""
    elif not isinstance(normalized["base_url"], str):
        # Legacy rows may store numeric placeholders; cast to string for TEXT column.
        normalized["base_url"] = str(normalized["base_url"])
    # ``probe_status`` is NOT NULL with a default of 'never' in the target schema.
    # Ensure a sensible default if the legacy row lacks a value.
    if normalized.get("probe_status") is None:
        normalized["probe_status"] = "never"
    # ``api_key_enc`` should be stored as TEXT. Legacy rows sometimes contain a
    # numeric placeholder (e.g., 0 or an integer ID). Convert any non‑string
    # value to a string so asyncpg can bind it as TEXT.
    if normalized.get("api_key_enc") is not None and not isinstance(
        normalized["api_key_enc"], str
    ):
        normalized["api_key_enc"] = str(normalized["api_key_enc"])

    # ``last_probe_at`` is a TIMESTAMPTZ column. Some legacy rows store it as
    # a Unix epoch integer (seconds since 1970). Convert such values to a
    # ``datetime`` instance; asyncpg will then serialize it correctly.
    if normalized.get("last_probe_at") is not None and not isinstance(
        normalized["last_probe_at"], datetime
    ):
        val = normalized["last_probe_at"]
        try:
            # Accept int or float epoch seconds
            ts = float(val)
            normalized["last_probe_at"] = datetime.fromtimestamp(ts, tz=UTC)
        except Exception:
            # Fallback: keep original value (asyncpg may raise a clear error)
            pass
    for column in ("capabilities", "subsystem_map", "available_models", "extra_config"):
        normalized[column] = _json_text(
            normalized.get(column),
            "{}"
            if column in {"capabilities", "subsystem_map", "extra_config"}
            else "[]",
        )
    enabled = normalized.get("enabled")
    normalized["enabled"] = bool(enabled) if enabled is not None else True
    return normalized


def _normalize_chat_history_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_common_row(row)
    normalized["metadata"] = (
        _json_text(normalized.get("metadata"), "null")
        if normalized.get("metadata") is not None
        else None
    )
    # ``sender_id`` is defined as TEXT in the target schema, but legacy rows
    # may store it as an integer (e.g., a numeric user ID). Ensure it is a
    # string so asyncpg can bind it correctly.
    if "sender_id" in normalized and normalized["sender_id"] is not None:
        if not isinstance(normalized["sender_id"], str):
            normalized["sender_id"] = str(normalized["sender_id"])
    # ``interface_path`` is NOT NULL in the target schema. If the legacy row
    # lacks a value, replace it with an empty string (the application can later
    # interpret an empty path as unknown).
    # ``interface_path`` must be a non‑null TEXT. If the source row lacks the
    # column or contains NULL, replace it with an empty string.
    if not normalized.get("interface_path"):
        normalized["interface_path"] = ""
    # ``message_text`` is NOT NULL in the target schema. If the legacy row lacks
    # a value, replace it with an empty string. Additionally, if the value is a
    # non‑string (e.g., an integer ID), cast it to ``str``.
    if "message_text" in normalized:
        if normalized["message_text"] is None:
            normalized["message_text"] = ""
        elif not isinstance(normalized["message_text"], str):
            normalized["message_text"] = str(normalized["message_text"])
    return normalized


def _normalize_boolean_columns(
    row: Mapping[str, Any], columns: tuple[str, ...]
) -> dict[str, Any]:
    normalized = _normalize_common_row(row)
    for column in columns:
        if column in normalized and normalized[column] is not None:
            normalized[column] = bool(normalized[column])
    return normalized


def _normalize_emotion_diary_row(row: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _normalize_common_row(row)
    raw_id = normalized.get("id")
    legacy_numeric_id: int | None = None

    if isinstance(raw_id, int):
        legacy_numeric_id = raw_id
        normalized["id"] = f"emotion_{raw_id}"
    else:
        raw_text = str(raw_id).strip() if raw_id is not None else ""
        normalized["id"] = raw_text or f"emotion_{datetime.now(UTC).timestamp():.0f}"
        if raw_text.isdigit():
            legacy_numeric_id = int(raw_text)

    intensity = normalized.get("intensity")
    normalized["intensity"] = float(intensity) if intensity is not None else None
    normalized["legacy_numeric_id"] = legacy_numeric_id
    normalized["created_at"] = (
        normalized.get("created_at")
        or normalized.get("next_check")
        or datetime.now(UTC)
    )
    return normalized


TABLE_SPECS: dict[str, TableMigrationSpec] = {
    "config": TableMigrationSpec(
        name="config",
        columns=("config_key", "value", "created_at", "updated_at"),
        conflict_keys=("config_key",),
        transform=_normalize_config_row,
    ),
    "external_endpoints": TableMigrationSpec(
        name="external_endpoints",
        # Exclude the primary‑key ``id`` column from the insert payload. The
        # target PostgreSQL table defines ``id`` as a SERIAL primary key, so we
        # let the database generate a fresh identifier for each migrated row.
        # This prevents duplicate‑key violations when the migration is run
        # multiple times (e.g., after a partial failure).
        columns=(
            "name",
            "display_label",
            "protocol",
            "base_url",
            "api_key_enc",
            "enabled",
            "capabilities",
            "subsystem_map",
            "available_models",
            "default_model",
            "probe_status",
            "last_probe_at",
            "extra_config",
            "created_at",
            "updated_at",
        ),
        conflict_keys=("name",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_external_endpoint_row,
    ),
    "chat_history_cache": TableMigrationSpec(
        name="chat_history_cache",
        columns=(
            "id",
            "interface_path",
            "sender_name",
            "sender_id",
            "message_text",
            "metadata",
            "created_at",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_chat_history_row,
    ),
    "chat_session_meta": TableMigrationSpec(
        name="chat_session_meta",
        columns=("interface_path", "meta", "updated_at"),
        conflict_keys=("interface_path",),
        transform=_normalize_common_row,
    ),
    "chat_archives": TableMigrationSpec(
        name="chat_archives",
        columns=("id", "session_id", "name", "messages", "metadata", "created_at"),
        conflict_keys=("id",),
        transform=_normalize_common_row,
    ),
    "ai_diary": TableMigrationSpec(
        name="ai_diary",
        columns=(
            "id",
            "content",
            "personal_thought",
            "emotions",
            "interaction_summary",
            "created_at",
            "interface",
            "chat_id",
            "thread_id",
            "user_message",
            "context_tags",
            "involved_users",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_ai_diary_row,
    ),
    "ai_diary_archive": TableMigrationSpec(
        name="ai_diary_archive",
        columns=(
            "id",
            "content",
            "personal_thought",
            "emotions",
            "interaction_summary",
            "created_at",
            "interface",
            "chat_id",
            "thread_id",
            "user_message",
            "context_tags",
            "involved_users",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_ai_diary_row,
    ),
    "memories": TableMigrationSpec(
        name="memories",
        columns=(
            "id",
            "created_at",
            "content",
            "author",
            "source",
            "tags",
            "scope",
            "emotion",
            "intensity",
            "emotion_state",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_common_row,
    ),
    "emotion_state": TableMigrationSpec(
        name="emotion_state",
        columns=("id", "emotion_name", "intensity", "created_at", "updated_at"),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_common_row,
    ),
    "emotion_diary": TableMigrationSpec(
        name="emotion_diary",
        columns=(
            "id",
            "legacy_numeric_id",
            "source",
            "event",
            "emotion",
            "intensity",
            "state",
            "trigger_condition",
            "decision_logic",
            "next_check",
            "created_at",
        ),
        conflict_keys=("id",),
        transform=_normalize_emotion_diary_row,
    ),
    "bio": TableMigrationSpec(
        name="bio",
        columns=(
            "id",
            "known_as",
            "likes",
            "not_likes",
            "information",
            "past_events",
            "feelings",
            "contacts",
            "social_accounts",
            "privacy",
            "created_at",
            "last_accessed",
            "last_update",
            "update_count",
            "user_name",
        ),
        conflict_keys=("id",),
        transform=_normalize_bio_row,
    ),
    "scheduled_events": TableMigrationSpec(
        name="scheduled_events",
        columns=(
            "id",
            "date",
            "time",
            "recurrence_type",
            "next_run",
            "description",
            "created_at",
            "delivered",
            "created_by",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        # Use the dedicated normalizer that handles TIME conversion and booleans.
        transform=_normalize_scheduled_events_row,
    ),
    "blocklist": TableMigrationSpec(
        name="blocklist",
        columns=("user_id", "reason", "blocked_at"),
        conflict_keys=("user_id",),
        transform=_normalize_common_row,
    ),
    "message_map": TableMigrationSpec(
        name="message_map",
        columns=("trainer_message_id", "chat_id", "message_id", "created_at"),
        conflict_keys=("trainer_message_id",),
        transform=_normalize_message_map_row,
    ),
    "grillo_beats": TableMigrationSpec(
        name="grillo_beats",
        columns=(
            "id",
            "beat_type",
            "next_beat",
            "metadata",
            "enabled",
            "plugin_enabled",
            "created_at",
            "updated_at",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=lambda row: _normalize_boolean_columns(
            row, ("enabled", "plugin_enabled")
        ),
    ),
    "grillo_activity_log": TableMigrationSpec(
        name="grillo_activity_log",
        columns=(
            "id",
            "beat_type",
            "prompt_text",
            "response_text",
            "diary_entry_id",
            "executed_at",
            "metadata",
            "suppressed_count",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_common_row,
    ),
    "grillo_action_execs": TableMigrationSpec(
        name="grillo_action_execs",
        columns=(
            "id",
            "activity_log_id",
            "action_index",
            "action_type",
            "payload",
            "status",
            "error_text",
            "result",
            "created_at",
            "updated_at",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_common_row,
    ),
    "agent_tasks": TableMigrationSpec(
        name="agent_tasks",
        columns=(
            "id",
            "engine",
            "status",
            "input",
            "iterations_meta",
            "output",
            "trainer_id",
            "metadata",
            "created_at",
            "updated_at",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_common_row,
    ),
    "message_logs": TableMigrationSpec(
        name="message_logs",
        columns=(
            "id",
            "chat_id",
            "interface",
            "sender_id",
            "sender_name",
            "content",
            "role",
            "metadata",
            "created_at",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_common_row,
    ),
    "archived_memories": TableMigrationSpec(
        name="archived_memories",
        columns=(
            "id",
            "tag",
            "summary",
            "source_ids",
            "source_count",
            "llm_model",
            "confidence",
            "notes",
            "compaction_level",
            "total_source_chars",
            "summary_chars",
            "created_by",
            "created_at",
        ),
        conflict_keys=("id",),
        serial_column="id",
        fetch_order="id",
        transform=_normalize_common_row,
    ),
}


MIGRATION_ORDER: tuple[str, ...] = (
    "config",
    "external_endpoints",
    "chat_history_cache",
    "chat_session_meta",
    "chat_archives",
    "ai_diary",
    "ai_diary_archive",
    "memories",
    "emotion_state",
    "bio",
    "scheduled_events",
    "blocklist",
    "message_map",
    "grillo_beats",
    "grillo_activity_log",
    "grillo_action_execs",
    "agent_tasks",
    "message_logs",
    "archived_memories",
)

LEGACY_OPTIONAL_TABLES: tuple[str, ...] = ("emotion_diary",)


def resolve_selected_tables(config: MainDbMigrationConfig) -> tuple[str, ...]:
    if config.tables:
        return config.tables

    selected_tables = list(MIGRATION_ORDER)
    if config.include_legacy_emotion_diary:
        for table_name in LEGACY_OPTIONAL_TABLES:
            if table_name not in selected_tables:
                selected_tables.append(table_name)
    return tuple(selected_tables)


def load_app_postgres_schema(schema_path: Path | None = None) -> str:
    path = schema_path or _default_schema_path()
    return path.read_text(encoding="utf-8")


def audit_source_schema(table_name: str, column_types: Mapping[str, str]) -> list[str]:
    warnings: list[str] = []
    lowered = {key: str(value).lower() for key, value in column_types.items()}

    if table_name == "emotion_diary":
        id_type = lowered.get("id", "")
        intensity_type = lowered.get("intensity", "")
        if "varchar" in id_type or "text" in id_type:
            warnings.append(
                "emotion_diary.id is a legacy text primary key in MariaDB; target keeps text ids and adds legacy_numeric_id for newer numeric rows."
            )
        if intensity_type.startswith("int"):
            warnings.append(
                "emotion_diary.intensity is integral in MariaDB; target widens it to double precision to preserve low-intensity values."
            )
        if "created_at" not in lowered:
            warnings.append(
                "emotion_diary is missing a created_at column; migration will backfill created_at from next_check or current time."
            )

    if table_name == "ai_diary":
        user_message_type = lowered.get("user_message", "")
        if user_message_type.startswith("varchar"):
            warnings.append(
                "ai_diary.user_message is width-limited in MariaDB; target widens it to text to avoid diary truncation on migration."
            )

    if table_name == "chat_history_cache" and "metadata" not in lowered:
        warnings.append(
            "chat_history_cache.metadata is missing in MariaDB; target retains the column and fills existing rows with null metadata."
        )

    return warnings


def normalize_table_row(table_name: str, row: Mapping[str, Any]) -> dict[str, Any]:
    spec = TABLE_SPECS[table_name]
    transformer = spec.transform or _normalize_common_row
    normalized = transformer(row)
    return {column: normalized.get(column) for column in spec.columns}


def build_postgres_upsert_sql(spec: TableMigrationSpec) -> str:
    """Build an UPSERT statement for a given table spec.

    For most tables the generic placeholder list ``$1, $2, …`` works because
    asyncpg can infer the target column types.  The ``message_map`` table,
    however, stores ``chat_id`` as ``BIGINT`` and ``message_id`` as ``INTEGER``
    in the target PostgreSQL schema while the source MariaDB provides them as
    Python ``int`` objects.  AsyncPG sometimes treats those placeholders as
    text, leading to ``expected str, got int`` errors.  To avoid this we add an
    explicit cast for the numeric columns.
    """

    quoted_columns = ", ".join(_quote(column) for column in spec.columns)

    # Mapping of table -> column -> PostgreSQL type for explicit casts.
    numeric_casts: dict[str, dict[str, str]] = {
        "message_map": {"chat_id": "BIGINT", "message_id": "INTEGER"},
        # The config table stores both columns as TEXT in PostgreSQL. Some legacy
        # values are integers (e.g., AWAIT_RESPONSE_TIMEOUT = 600). Cast the
        # "value" column to TEXT to avoid "expected str, got int" errors.
        "config": {"value": "TEXT"},
        # Add other tables here if similar type‑mismatch errors appear.
    }

    placeholders: list[str] = []
    for idx, column in enumerate(spec.columns, start=1):
        ph = f"${idx}"
        if spec.name in numeric_casts and column in numeric_casts[spec.name]:
            ph = f"${idx}::{numeric_casts[spec.name][column]}"
        placeholders.append(ph)
    placeholders_sql = ", ".join(placeholders)

    conflict_target = ", ".join(_quote(column) for column in spec.conflict_keys)
    assignments = [
        f"{_quote(column)} = EXCLUDED.{_quote(column)}"
        for column in spec.columns
        if column not in spec.conflict_keys
    ]
    update_sql = ", ".join(assignments)
    if not update_sql:
        return (
            f"INSERT INTO {_quote(spec.name)} ({quoted_columns}) VALUES ({placeholders_sql}) "
            f"ON CONFLICT ({conflict_target}) DO NOTHING"
        )
    return (
        f"INSERT INTO {_quote(spec.name)} ({quoted_columns}) VALUES ({placeholders_sql}) "
        f"ON CONFLICT ({conflict_target}) DO UPDATE SET {update_sql}"
    )


def _split_sql_statements(sql_text: str) -> list[str]:
    filtered_lines = [
        line for line in sql_text.splitlines() if not line.strip().startswith("--")
    ]
    return [
        statement.strip()
        for statement in "\n".join(filtered_lines).split(";")
        if statement.strip()
    ]


class MainDbMigrator:
    def __init__(self, config: MainDbMigrationConfig) -> None:
        self.config = config
        self._source_id_cache: dict[str, set[int]] = {}

    async def run(self) -> list[TableMigrationResult]:
        requires_target = not (self.config.dry_run or self.config.audit_only)
        if aiomysql is None:
            raise RuntimeError("aiomysql is required to read the legacy MariaDB source")
        if requires_target and asyncpg is None:
            raise RuntimeError("asyncpg is required to write the PostgreSQL target")
        if requires_target and not self.config.target_dsn:
            raise RuntimeError(
                "Target PostgreSQL DSN is empty. Set TARGET_POSTGRES_DSN, DATABASE_URL, or APP_POSTGRES_DSN."
            )

        source_env = {
            "SOURCE_DB_TYPE": "mariadb",
            "SOURCE_DB_HOST": str(self.config.source_host),
            "SOURCE_DB_PORT": str(self.config.source_port),
            "SOURCE_DB_USER": str(self.config.source_user),
            "SOURCE_DB_PASSWORD": str(self.config.source_password),
            "SOURCE_DB_NAME": str(self.config.source_database),
        }
        original_source_env = {key: os.environ.get(key) for key in source_env}
        for key, value in source_env.items():
            os.environ[key] = value

        source_conn = await connect_source_db()
        target_conn = (
            await connect_postgres_dsn(self.config.target_dsn)
            if requires_target
            else None
        )

        try:
            if target_conn is not None:
                await self._apply_schema(target_conn)

            selected_tables = resolve_selected_tables(self.config)
            results: list[TableMigrationResult] = []
            for table_name in selected_tables:
                if table_name not in TABLE_SPECS:
                    raise KeyError(f"Unknown migration table: {table_name}")
                result = await self._migrate_table(
                    source_conn,
                    target_conn,
                    TABLE_SPECS[table_name],
                )
                results.append(result)

            if target_conn is not None:
                await self._reset_sequences(target_conn, results)

            return results
        finally:
            source_conn.close()
            if target_conn is not None:
                await target_conn.close()
            for key, value in original_source_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def _apply_schema(self, target_conn: Any) -> None:
        schema_sql = load_app_postgres_schema(self.config.schema_path)
        for statement in _split_sql_statements(schema_sql):
            await target_conn.execute(statement)

    async def _migrate_table(
        self,
        source_conn: Any,
        target_conn: Any,
        spec: TableMigrationSpec,
    ) -> TableMigrationResult:
        result = TableMigrationResult(name=spec.name)
        async with source_conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute("SHOW TABLES LIKE %s", (spec.name,))
            if not await cursor.fetchone():
                result.skipped = True
                result.warnings.append(
                    f"Source table '{spec.name}' does not exist; skipped."
                )
                return result

            await cursor.execute(f"SHOW COLUMNS FROM `{spec.name}`")
            source_columns = await cursor.fetchall()
            column_types = {
                row["Field"]: row["Type"]
                for row in source_columns
                if row.get("Field") and row.get("Type")
            }
            result.warnings.extend(audit_source_schema(spec.name, column_types))

            if self.config.audit_only:
                return result

            offset = 0
            order_column = spec.fetch_order or spec.conflict_keys[0]
            insert_sql = build_postgres_upsert_sql(spec)
            sanitized_diary_refs = 0
            valid_diary_ids = (
                await self._load_source_id_set(source_conn, "ai_diary")
                if spec.name == "grillo_activity_log"
                else None
            )

            while True:
                await cursor.execute(
                    f"SELECT * FROM `{spec.name}` ORDER BY `{order_column}` LIMIT %s OFFSET %s",
                    (self.config.batch_size, offset),
                )
                rows = await cursor.fetchall()
                if not rows:
                    break

                normalized_rows = [normalize_table_row(spec.name, row) for row in rows]
                if spec.name == "grillo_activity_log" and valid_diary_ids is not None:
                    sanitized_diary_refs += _sanitize_grillo_activity_log_rows(
                        normalized_rows, valid_diary_ids
                    )
                if not self.config.dry_run:
                    # Ensure TEXT columns receive string values. The legacy MariaDB
                    # rows sometimes store numeric identifiers for ``sender_id``
                    # or ``message_text``. Cast them to ``str`` here to avoid the
                    # asyncpg ``expected str, got int`` DataError.
                    for normalized_row in normalized_rows:
                        if (
                            "sender_id" in normalized_row
                            and normalized_row["sender_id"] is not None
                        ):
                            if not isinstance(normalized_row["sender_id"], str):
                                normalized_row["sender_id"] = str(
                                    normalized_row["sender_id"]
                                )
                        if (
                            "message_text" in normalized_row
                            and normalized_row["message_text"] is not None
                        ):
                            if not isinstance(normalized_row["message_text"], str):
                                normalized_row["message_text"] = str(
                                    normalized_row["message_text"]
                                )
                    # Final safeguard: guarantee NOT NULL TEXT columns have
                    # non‑null string values. ``interface_path`` and ``message_text``
                    # are required by the target schema. If they are missing or
                    # falsy after normalisation, replace with an empty string.
                    payloads = []
                    for normalized_row in normalized_rows:
                        # ``interface_path`` is part of a unique index together
                        # with ``timestamp``. Converting NULL to a plain empty
                        # string would cause collisions when multiple rows share
                        # the same timestamp (common for legacy data). To keep
                        # the NOT‑NULL constraint while preserving uniqueness,
                        # generate a deterministic placeholder that incorporates
                        # the primary key ``id`` when the original value is
                        # missing or falsy.
                        if not normalized_row.get("interface_path"):
                            # Use the row's ``id`` if available; fall back to a
                            # generic placeholder.
                            placeholder = (
                                f"legacy_{normalized_row.get('id')}"
                                if normalized_row.get("id") is not None
                                else "legacy_unknown"
                            )
                            normalized_row["interface_path"] = placeholder
                        if not normalized_row.get("message_text"):
                            normalized_row["message_text"] = ""
                        # ``chat_session_meta`` requires ``meta`` NOT NULL.
                        if (
                            spec.name == "chat_session_meta"
                            and normalized_row.get("meta") is None
                        ):
                            normalized_row["meta"] = ""
                        # ``chat_archives`` requires a non‑null primary key ``id``
                        # and a non‑null ``messages`` column. Legacy rows may have
                        # ``id`` set to NULL and ``messages`` missing. Generate a
                        # deterministic placeholder for ``id`` using ``session_id``
                        # and ``timestamp`` (both are present in the source) and
                        # default ``messages`` to an empty JSON array.
                        if spec.name == "chat_archives":
                            if not normalized_row.get("id"):
                                session_part = (
                                    normalized_row.get("session_id") or "unknown"
                                )
                                ts_part = normalized_row.get("created_at")
                                placeholder = (
                                    f"legacy_{session_part}_{ts_part}"
                                    if ts_part
                                    else f"legacy_{session_part}"
                                )
                                normalized_row["id"] = placeholder
                            if normalized_row.get("messages") is None:
                                normalized_row["messages"] = "[]"
                            # ``session_id`` and ``name`` are TEXT columns in the target schema.
                            # Legacy rows may store them as numeric types. Cast to ``str``
                            # to satisfy asyncpg's type expectations.
                            if normalized_row.get(
                                "session_id"
                            ) is not None and not isinstance(
                                normalized_row["session_id"], str
                            ):
                                normalized_row["session_id"] = str(
                                    normalized_row["session_id"]
                                )
                            if normalized_row.get(
                                "name"
                            ) is not None and not isinstance(
                                normalized_row["name"], str
                            ):
                                normalized_row["name"] = str(normalized_row["name"])
                        # ``ai_diary`` stores several TEXT columns that may contain
                        # numeric values in the legacy MariaDB (e.g., ``chat_id``
                        # could be ``-1.0``). Cast them to ``str`` to satisfy the
                        # PostgreSQL TEXT type and avoid asyncpg type errors.
                        if spec.name in ("ai_diary", "ai_diary_archive"):
                            for col in (
                                "interface",
                                "chat_id",
                                "thread_id",
                                "user_message",
                            ):
                                if normalized_row.get(
                                    col
                                ) is not None and not isinstance(
                                    normalized_row[col], str
                                ):
                                    normalized_row[col] = str(normalized_row[col])
                            # ``user_message`` is NOT NULL in the target schema.
                            if normalized_row.get("user_message") is None:
                                normalized_row["user_message"] = ""
                        # Universal safeguard: cast columns that must be TEXT in
                        # PostgreSQL back to strings.  ``_coerce_value`` may have
                        # converted numeric-looking strings (e.g. chat IDs, user
                        # IDs) to int/float, which asyncpg rejects for TEXT
                        # columns.
                        _TEXT_COLUMNS: dict[str, tuple[str, ...]] = {
                            "ai_diary": (
                                "interface",
                                "chat_id",
                                "thread_id",
                                "user_message",
                            ),
                            "ai_diary_archive": (
                                "interface",
                                "chat_id",
                                "thread_id",
                                "user_message",
                            ),
                            "blocklist": ("user_id",),
                            "message_logs": (
                                "content",
                                "metadata",
                                "chat_id",
                                "interface",
                                "sender_id",
                                "sender_name",
                            ),
                            "chat_session_meta": ("interface_path",),
                            "external_endpoints": (
                                "name",
                                "display_label",
                                "base_url",
                                "api_key_enc",
                                "probe_status",
                                "default_model",
                            ),
                            "chat_archives": ("id", "session_id", "name"),
                            "bio": ("id", "created_at", "last_accessed"),
                            "grillo_beats": ("beat_type",),
                            "grillo_activity_log": ("beat_type",),
                            "grillo_action_execs": ("action_type", "status"),
                            "agent_tasks": ("engine", "status", "trainer_id"),
                            "archived_memories": (
                                "tag",
                                "summary",
                                "source_ids",
                                "llm_model",
                                "created_by",
                            ),
                            "scheduled_events": (
                                "recurrence_type",
                                "created_by",
                                "description",
                            ),
                            "memories": (
                                "author",
                                "source",
                                "tags",
                                "scope",
                                "emotion",
                                "emotion_state",
                            ),
                            "emotion_state": ("emotion_name",),
                        }
                        for col in _TEXT_COLUMNS.get(spec.name, ()):
                            val = normalized_row.get(col)
                            if val is not None and not isinstance(val, str):
                                normalized_row[col] = str(val)
                        payloads.append(
                            tuple(normalized_row.get(column) for column in spec.columns)
                        )
                    await target_conn.executemany(insert_sql, payloads)

                result.migrated_rows += len(normalized_rows)
                offset += len(rows)
                if len(rows) < self.config.batch_size:
                    break

            if sanitized_diary_refs:
                result.warnings.append(
                    "grillo_activity_log.diary_entry_id has orphaned ai_diary references in MariaDB; "
                    f"migrated {sanitized_diary_refs} broken references as null to satisfy the target foreign key."
                )

        return result

    async def _load_source_id_set(self, source_conn: Any, table_name: str) -> set[int]:
        cached = self._source_id_cache.get(table_name)
        if cached is not None:
            return cached

        ids: set[int] = set()
        async with source_conn.cursor(aiomysql.DictCursor) as cursor:
            await cursor.execute(f"SELECT `id` FROM `{table_name}`")
            for row in await cursor.fetchall():
                raw_id = row.get("id")
                if raw_id is None:
                    continue
                try:
                    ids.add(int(raw_id))
                except (TypeError, ValueError):
                    continue

        self._source_id_cache[table_name] = ids
        return ids

    async def _reset_sequences(
        self, target_conn: Any, results: list[TableMigrationResult]
    ) -> None:
        migrated_tables = {
            result.name for result in results if result.migrated_rows > 0
        }
        for spec in TABLE_SPECS.values():
            if spec.name not in migrated_tables or spec.serial_column is None:
                continue
            await target_conn.execute(
                f"SELECT setval(pg_get_serial_sequence('{spec.name}', '{spec.serial_column}'), "
                f"COALESCE((SELECT MAX({_quote(spec.serial_column)}) FROM {_quote(spec.name)}), 1), true)"
            )
