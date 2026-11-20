"""List, inspect, set the privacy of, and delete the people Bio Manager knows.

This is the operator's view behind the WebUI Bio tab: who has a profile, what it
holds, who it is shared with, and a way to forget someone. It lives in core,
apart from ``plugins/bio_manager`` (the plugin the model talks to), so the WebUI can
use it whether or not that plugin is loaded; the ``bio`` table is the single store
either way, and ``PRIVATE`` / ``is_private`` here are the one definition of the
privacy flag that the plugin imports.

Deleting a person removes their ``bio`` row only. Conversation memories in SOUL
that mention them are history and are handled by the memory tools, not here; the
function reports that clearly so the operator is not told more than was done.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.db import DictCursor, get_conn_ctx
from core.logging_utils import log_info

PRIVATE = "private"


def is_private(value: Any) -> bool:
    """Whether a ``privacy`` column value marks the profile as private."""
    return str(value or "").strip().lower() == PRIVATE


def _load_json_field(value: Any, key: str, default: Any) -> Any:
    """A JSON-in-TEXT column as a Python value, or ``default`` when it is empty or bad."""
    if not value:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return default


VALID_PRIVACY: tuple[str, ...] = ("default", PRIVATE)

_NUMERIC_ID = re.compile(r"^-?\d+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_LIST_COLUMNS = ("known_as", "likes", "not_likes", "past_events", "social_accounts")


def classify_person_id(person_id: str) -> str:
    """What kind of identity an id is, so the list can tell people from plumbing.

    ``person``: a platform account id (Telegram, Discord ...).
    ``api``: a pseudo-user the OpenAI/Ollama-compatible API creates per client.
    ``session``: an anonymous session or chat UUID.
    ``other``: anything else (namespaced ids, usernames).
    """
    pid = str(person_id or "").strip()
    if _NUMERIC_ID.match(pid):
        return "person"
    if pid.startswith("ollama-user") or pid.startswith("api-user"):
        return "api"
    if _UUID.match(pid.split(":")[-1].lower()):
        return "session"
    return "other"


def _display_name(user_name: str, known_as: list[Any], seen_name: str) -> str:
    """The name to show for a profile: the one set on it, a nickname, or how the chat shows them."""
    return user_name or (str(known_as[0]) if known_as else "") or seen_name


async def _latest_seen_names(ids: list[str]) -> dict[str, str]:
    """Most recent chat-history name of each id (what the platform called them)."""
    if not ids:
        return {}
    marks = ", ".join(["%s"] * len(ids))
    async with get_conn_ctx() as conn:
        async with conn.cursor(DictCursor) as cur:
            await cur.execute(
                "SELECT DISTINCT ON (sender_id) sender_id, sender_name "
                f"FROM chat_history_cache WHERE sender_id IN ({marks}) "
                "AND sender_name IS NOT NULL AND sender_name <> '' "
                "ORDER BY sender_id, id DESC",
                tuple(ids),
            )
            rows = await cur.fetchall()
    return {str(r["sender_id"]): str(r["sender_name"]) for r in rows or []}


def _summary(row: dict[str, Any], seen_name: str = "") -> dict[str, Any]:
    known_as = _load_json_field(row.get("known_as"), "known_as", [])
    known_as = known_as if isinstance(known_as, list) else []
    information = str(row.get("information") or "")
    user_name = row.get("user_name") or ""
    return {
        "id": str(row.get("id")),
        "kind": classify_person_id(str(row.get("id"))),
        "user_name": user_name,
        "display_name": _display_name(user_name, known_as, seen_name),
        "known_as": known_as,
        "information_preview": information[:160],
        "has_information": bool(information.strip()),
        "privacy": row.get("privacy") or "default",
        "last_accessed": str(row.get("last_accessed") or ""),
        "last_update": str(row.get("last_update") or ""),
        "update_count": int(row.get("update_count") or 0),
    }


async def list_people() -> list[dict[str, Any]]:
    """Every profile, most recently seen first."""
    async with get_conn_ctx() as conn:
        async with conn.cursor(DictCursor) as cur:
            await cur.execute(
                "SELECT id, user_name, known_as, information, privacy, "
                "last_accessed, last_update, update_count FROM bio "
                "ORDER BY last_accessed DESC"
            )
            rows = await cur.fetchall()
    people = [dict(row) for row in rows or []]
    # A profile that has no name yet is still somebody the chat has a name for.
    nameless = [
        str(p["id"])
        for p in people
        if not (p.get("user_name") or "") and p.get("known_as") in (None, "", "[]")
    ]
    seen = await _latest_seen_names(nameless)
    return [_summary(p, seen.get(str(p["id"]), "")) for p in people]


async def get_person(person_id: str) -> dict[str, Any] | None:
    """The whole profile of one person, or ``None``."""
    async with get_conn_ctx() as conn:
        async with conn.cursor(DictCursor) as cur:
            await cur.execute("SELECT * FROM bio WHERE id=%s", (str(person_id),))
            row = await cur.fetchone()
    if not row:
        return None
    row = dict(row)
    seen = await _latest_seen_names([str(row.get("id"))])
    detail = _summary(row, seen.get(str(row.get("id")), ""))
    detail["information"] = str(row.get("information") or "")
    for column in _LIST_COLUMNS:
        detail[column] = _load_json_field(row.get(column), column, [])
    detail["feelings"] = _load_json_field(row.get("feelings"), "feelings", [])
    detail["contacts"] = _load_json_field(row.get("contacts"), "contacts", {})
    detail["created_at"] = str(row.get("created_at") or "")
    return detail


async def set_privacy(person_id: str, privacy: str) -> bool:
    """Mark a profile ``private`` (shown only to the person it is about) or not."""
    value = str(privacy or "").strip().lower()
    if value not in VALID_PRIVACY:
        raise ValueError(f"privacy must be one of {VALID_PRIVACY}")
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE bio SET privacy=%s WHERE id=%s", (value, str(person_id))
            )
            changed = getattr(cur, "rowcount", 1)
    return bool(changed)


async def delete_person(person_id: str) -> bool:
    """Forget a person: delete their ``bio`` row. Returns whether one existed."""
    # TODO(privacy): this removes the profile only. SOUL cells, situational notes and
    # diary entries that mention the person stay (they are conversation history).
    # Purging them needs an audience on cells (who was in the chat), which does not
    # exist yet: see "Privacy" in TODO-TOFIX.md.
    existing = await get_person(person_id)
    if existing is None:
        return False
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM bio WHERE id=%s", (str(person_id),))
    log_info(
        f"[bio_manager] Deleted profile {existing['id']} "
        f"({existing['kind']}, {len(json.dumps(existing, default=str))} bytes)"
    )
    return True
