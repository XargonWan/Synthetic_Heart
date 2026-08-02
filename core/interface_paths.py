# core/interface_paths.py
"""Canonical registry for interface paths used as message targets.

This module is the single source of truth for:

* recording that an ``interface_path`` was used as a message *target*
  (``touch_interface_path``) with a float-epoch ``last_used`` timestamp;
* building a human-readable "pretty name" for any interface path
  (``build_pretty_name``) from data that already exists: the live interface
  ``display_name`` attribute (segment 0) and the ``segment_labels`` that
  interfaces store live at message time via ``resolve_and_touch`` (segments 1+);
* serving the recent-target list to any consumer
  (``get_recent_interface_paths``), replacing the retired ``recent_chats``
  helpers;
* grouping known paths by their root prefix and resolving free-text terms to
  matching channel groups (``find_channel_groups``) for the channel-awareness
  recon plugin;
* daily maintenance: refreshing stale labels (``refresh_all_labels``) and
  pruning rows unused for longer than a threshold (``prune_stale_interface_paths``).

Interfaces register a name resolver via ``set_name_resolver`` and, when they
handle a message, call ``resolve_and_touch`` to fetch the live chat/thread
names and persist them as ``segment_labels`` on the ``interface_paths`` row.
No component builds its own pretty name — they all call ``build_pretty_name``
(or read the stored ``segment_labels``).
"""

from __future__ import annotations

import json
import time
from typing import Any, Awaitable, Callable, Optional

import aiomysql

from core.db import get_conn_ctx
from core.interface_path_utils import parse_interface_path
from core.interfaces_registry import get_interface_registry
from core.logging_utils import log_debug, log_warning

# Default staleness threshold: rows unused for longer than this are pruned.
DEFAULT_STALE_DAYS = 90

# Registry of per-interface name resolvers. A resolver takes
# ``(chat_id, thread_id, bot)`` and returns
# ``{"chat_name": Optional[str], "message_thread_name": Optional[str]}``.
# Interfaces register themselves at startup; nothing is hardcoded here.
NameResolver = Callable[..., Awaitable[dict[str, Optional[str]]]]
_NAME_RESOLVERS: dict[str, NameResolver] = {}


def set_name_resolver(interface: str, resolver: NameResolver) -> None:
    """Register a live chat/thread name resolver for an interface.

    The resolver is called by :func:`resolve_and_touch` to fetch the current
    chat and thread names from the live interface (e.g. Telegram ``getChat``),
    which are then stored as ``segment_labels`` on the interface path.
    """
    _NAME_RESOLVERS[interface] = resolver


def get_name_resolver(interface: str) -> Optional[NameResolver]:
    """Return the registered name resolver for an interface, if any."""
    return _NAME_RESOLVERS.get(interface)


# ---------------------------------------------------------------------------
# Pretty-name building
# ---------------------------------------------------------------------------


def _prettify_segment(raw: str) -> str:
    """Best-effort humanisation of a raw path segment (interface name or id)."""
    text = str(raw or "").strip()
    if not text:
        return text
    # Turn snake_case / kebab-case into Title Case words.
    cleaned = text.replace("_", " ").replace("-", " ").strip()
    if not cleaned:
        return text
    return " ".join(word.capitalize() for word in cleaned.split())


def _interface_label(interface_name: str) -> str:
    """Resolve the human label for an interface via its live ``display_name``.

    No hardcoded interface->label map: this reads the ``display_name`` class
    attribute off the registered interface instance, so new interfaces
    (including userbots) describe themselves. Falls back to a prettified form
    of the interface name when the interface is not registered.
    """
    try:
        instance = get_interface_registry().get_interface(interface_name)
    except Exception:
        instance = None
    if instance is not None:
        label = getattr(instance, "display_name", None)
        if isinstance(label, str) and label.strip():
            return label.strip()
    return _prettify_segment(interface_name)


async def _stored_segment_labels(interface_path: str) -> list[str]:
    """Return the ``segment_labels`` stored on the interface_paths row, if any."""
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT segment_labels FROM interface_paths WHERE interface_path = %s",
                    (interface_path,),
                )
                row = await cur.fetchone()
    except Exception as exc:  # pragma: no cover - best effort
        log_debug(
            f"[interface_paths] stored-label lookup failed for {interface_path}: {exc}"
        )
        return []
    if not row:
        return []
    return _decode_labels(row.get("segment_labels"))


async def build_pretty_name(
    interface_path: str, *, use_cache: bool = True
) -> dict[str, Any]:
    """Build the pretty name for an ``interface_path``.

    Returns ``{"segment_labels": [...], "display": "A / B / C"}``.

    Resolution order per segment:
    * segment 0 (interface): live ``display_name`` attribute, else prettified name.
    * segments 1+ (chat/channel/thread): the label stored on the interface_paths
      row (written live by interfaces via :func:`resolve_and_touch`), else the
      raw id.

    ``use_cache`` is accepted for backward compatibility with callers but is a
    no-op now that names come from the stored row rather than a chatlink cache.
    """
    interface_name, levels = parse_interface_path(interface_path)
    labels: list[str] = [_interface_label(interface_name)]

    stored = await _stored_segment_labels(interface_path) if levels else []
    # stored[0] is the interface label; the deeper labels line up with levels.
    stored_levels = stored[1:] if len(stored) > 1 else []

    for idx, raw in enumerate(levels):
        raw_str = str(raw)
        label: Optional[str] = None
        if idx < len(stored_levels):
            candidate = stored_levels[idx]
            if candidate and candidate != raw_str:
                label = candidate
        labels.append(label or raw_str)

    return {"segment_labels": labels, "display": " / ".join(labels)}


async def resolve_and_touch(
    interface_path: str,
    chat_id: Any,
    thread_id: Optional[Any] = None,
    *,
    bot: Any = None,
    chat_name: Optional[str] = None,
    thread_name: Optional[str] = None,
) -> None:
    """Resolve live chat/thread names for an interface path and persist them.

    Invokes the name resolver registered for the path's interface (if any) to
    fetch the current ``chat_name`` / ``message_thread_name`` from the live
    interface, then stores them as ``segment_labels`` via
    :func:`touch_interface_path`. Never raises to the caller.

    ``chat_name`` / ``thread_name`` may be passed by the caller to supply names
    that are only available on the incoming update and cannot be fetched via a
    live API lookup (e.g. Telegram forum-topic names). Caller-supplied values
    take precedence over the resolver result.
    """
    if not interface_path or not isinstance(interface_path, str):
        return
    interface_name, levels = parse_interface_path(interface_path)
    resolver = get_name_resolver(interface_name)
    override_chat_name = chat_name
    override_thread_name = thread_name
    chat_name = None
    thread_name = None
    if resolver is not None:
        try:
            try:
                result = await resolver(chat_id, thread_id, bot)
            except TypeError:  # resolver may not accept bot
                result = await resolver(chat_id, thread_id)
            if result:
                chat_name = result.get("chat_name")
                thread_name = result.get("message_thread_name")
        except Exception as exc:
            log_debug(f"[interface_paths] resolver failed for {interface_path}: {exc}")

    # Caller-supplied names win (e.g. Telegram topic name from the update).
    if override_chat_name:
        chat_name = override_chat_name
    if override_thread_name:
        thread_name = override_thread_name

    labels: list[str] = [_interface_label(interface_name)]
    for idx, raw in enumerate(levels):
        raw_str = str(raw)
        if idx == 0 and chat_name:
            labels.append(str(chat_name))
        elif idx == 1 and thread_name:
            labels.append(str(thread_name))
        else:
            labels.append(raw_str)

    await touch_interface_path(interface_path, segment_labels=labels)


# ---------------------------------------------------------------------------
# Touch / upsert
# ---------------------------------------------------------------------------


async def touch_interface_path(
    interface_path: str, *, segment_labels: Optional[list[str]] = None
) -> None:
    """Record ``interface_path`` as recently used as a target.

    When ``segment_labels`` is not provided, re-derives them via
    :func:`build_pretty_name` (which reads the previously-stored labels).
    Upserts the row with ``last_used = time.time()`` (float epoch,
    backend-agnostic). Never raises to the caller — logs and returns on failure.
    """
    if not interface_path or not isinstance(interface_path, str):
        return
    try:
        if segment_labels is None:
            pretty = await build_pretty_name(interface_path)
            segment_labels = pretty["segment_labels"]
        labels_json = json.dumps(segment_labels, ensure_ascii=False)
        now = time.time()
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO interface_paths
                        (interface_path, last_used, segment_labels)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        last_used = VALUES(last_used),
                        segment_labels = VALUES(segment_labels)
                    """,
                    (interface_path, now, labels_json),
                )
                await conn.commit()
        log_debug(f"[interface_paths] Touched {interface_path}")
    except Exception as exc:
        log_warning(f"[interface_paths] Failed to touch {interface_path}: {exc}")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _decode_labels(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except Exception:
        pass
    return []


async def list_interface_paths() -> list[dict[str, Any]]:
    """Return all known interface paths with decoded labels and display strings."""
    results: list[dict[str, Any]] = []
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT interface_path, last_used, segment_labels
                    FROM interface_paths
                    ORDER BY last_used DESC
                    """
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_warning(f"[interface_paths] list_interface_paths failed: {exc}")
        return results
    for row in rows or []:
        labels = _decode_labels(row.get("segment_labels"))
        results.append(
            {
                "interface_path": row.get("interface_path"),
                "last_used": row.get("last_used"),
                "segment_labels": labels,
                "display": " / ".join(labels) if labels else row.get("interface_path"),
            }
        )
    return results


# Backwards-friendly alias used by some consumers/tests.
get_all_with_labels = list_interface_paths


async def get_recent_interface_paths(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recently used interface paths (replaces recent_chats).

    Each item: ``{"interface_path", "last_used", "segment_labels", "display"}``.
    """
    results: list[dict[str, Any]] = []
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    """
                    SELECT interface_path, last_used, segment_labels
                    FROM interface_paths
                    ORDER BY last_used DESC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_warning(f"[interface_paths] get_recent_interface_paths failed: {exc}")
        return results
    for row in rows or []:
        labels = _decode_labels(row.get("segment_labels"))
        results.append(
            {
                "interface_path": row.get("interface_path"),
                "last_used": row.get("last_used"),
                "segment_labels": labels,
                "display": " / ".join(labels) if labels else row.get("interface_path"),
            }
        )
    return results


# ---------------------------------------------------------------------------
# Grouping / channel resolution (recon channel resolver)
# ---------------------------------------------------------------------------


def _root_prefix(interface_path: str) -> str:
    """Return the group root prefix: ``interface_name/level1`` (or the path itself)."""
    interface_name, levels = parse_interface_path(interface_path)
    if levels:
        return f"{interface_name}/{levels[0]}"
    return interface_name


def _term_matches(term: str, labels: list[str]) -> bool:
    """Case-insensitive substring match of ``term`` against any segment label."""
    term_low = term.strip().lower()
    if not term_low:
        return False
    return any(term_low in str(label).lower() for label in labels)


async def find_channel_groups(terms: list[str]) -> list[dict[str, Any]]:
    """Resolve free-text terms to matching channel groups.

    A term matches a path when it appears (case-insensitively) in any of the
    path's ``segment_labels``. Matches are grouped by root prefix
    (``interface_name/level1``); one term can match multiple groups across
    interfaces (e.g. a "Dojo del Chaos" on both Telegram and Discord).

    Returns a list of groups::

        {
            "root_prefix": "telegram_bot/-1394756383",
            "group_label": "Telegram Bot / Dojo del Chaos",
            "thread_required": bool,   # True when only thread rows exist
            "children": [{"interface_path": ..., "display": ...}, ...],
        }

    When a group has only thread rows (no bare thread-less row) the header path
    is rendered with a ``/*`` placeholder by the caller and ``thread_required``
    is True, signalling that a thread segment is mandatory.
    """
    cleaned_terms = [t.strip() for t in (terms or []) if str(t).strip()]
    if not cleaned_terms:
        return []

    all_paths = await list_interface_paths()
    if not all_paths:
        return []

    # Collect matching paths grouped by root prefix.
    groups: dict[str, dict[str, Any]] = {}
    for item in all_paths:
        path = item.get("interface_path")
        labels = item.get("segment_labels") or []
        if not path:
            continue
        if not any(_term_matches(t, labels) for t in cleaned_terms):
            continue
        prefix = _root_prefix(path)
        group = groups.setdefault(
            prefix,
            {
                "root_prefix": prefix,
                "group_label": None,
                "children": [],
                "has_root": False,
            },
        )
        # Group label from the first two segment labels (interface + channel).
        if group["group_label"] is None and labels:
            group["group_label"] = " / ".join(str(x) for x in labels[:2])
        group["children"].append(
            {"interface_path": path, "display": item.get("display") or path}
        )
        if path == prefix:
            group["has_root"] = True

    output: list[dict[str, Any]] = []
    for prefix, group in groups.items():
        children = group["children"]
        # A group needs a mandatory thread segment when no bare root row exists
        # but deeper (thread) children do.
        has_deeper = any(c["interface_path"] != prefix for c in children)
        thread_required = (not group["has_root"]) and has_deeper
        output.append(
            {
                "root_prefix": prefix,
                "group_label": group["group_label"] or prefix,
                "thread_required": thread_required,
                "children": sorted(children, key=lambda c: c["interface_path"]),
            }
        )
    return output


# ---------------------------------------------------------------------------
# Maintenance: prune + label refresh
# ---------------------------------------------------------------------------


async def prune_stale_interface_paths(older_than_days: int = DEFAULT_STALE_DAYS) -> int:
    """Delete rows whose ``last_used`` is older than ``older_than_days``.

    Returns the number of rows deleted. Rows are recreated automatically the
    next time the path is used as a target.
    """
    cutoff = time.time() - (int(older_than_days) * 24 * 60 * 60)
    deleted = 0
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM interface_paths WHERE last_used < %s",
                    (cutoff,),
                )
                deleted = int(cur.rowcount or 0)
                await conn.commit()
        if deleted:
            log_debug(f"[interface_paths] Pruned {deleted} stale interface path(s)")
    except Exception as exc:
        log_warning(f"[interface_paths] prune failed: {exc}")
    return deleted


async def refresh_all_labels() -> int:
    """Re-derive ``segment_labels`` for every row; update the ones that changed.

    Returns the number of rows whose labels were updated. Labels are re-read
    from the stored row, so the interface label (segment 0) is refreshed while
    the live-resolved deeper labels are preserved.
    """
    updated = 0
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT interface_path, segment_labels FROM interface_paths"
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log_warning(f"[interface_paths] refresh_all_labels read failed: {exc}")
        return 0

    for row in rows or []:
        path = row.get("interface_path")
        if not path:
            continue
        current = _decode_labels(row.get("segment_labels"))
        try:
            pretty = await build_pretty_name(path, use_cache=True)
        except Exception as exc:
            log_debug(f"[interface_paths] refresh build failed for {path}: {exc}")
            continue
        new_labels = pretty["segment_labels"]
        if new_labels == current:
            continue
        try:
            labels_json = json.dumps(new_labels, ensure_ascii=False)
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE interface_paths SET segment_labels = %s WHERE interface_path = %s",
                        (labels_json, path),
                    )
                    await conn.commit()
            updated += 1
        except Exception as exc:
            log_warning(f"[interface_paths] refresh update failed for {path}: {exc}")
    if updated:
        log_debug(f"[interface_paths] Refreshed labels for {updated} path(s)")
    return updated


# ---------------------------------------------------------------------------
# Schema init (idempotent — called by the startup preflight)
# ---------------------------------------------------------------------------


async def init_interface_paths_table() -> None:
    """Create the ``interface_paths`` table if it does not exist.

    Written in MariaDB DDL; the backend translation layer converts it to the
    Postgres equivalent automatically. Idempotent (``IF NOT EXISTS``).
    """
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS interface_paths (
                    interface_path VARCHAR(512) PRIMARY KEY,
                    last_used DOUBLE NOT NULL,
                    segment_labels TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_interface_paths_last_used (last_used)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
                """
            )
            await conn.commit()
    log_debug("[interface_paths] ensured interface_paths table")
