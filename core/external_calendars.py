# core/external_calendars.py
"""External calendar subscriptions (ICS URL + CalDAV) for SyntH.

This module lets SyntH subscribe to third-party calendars and ingest their
events. Two protocols are supported:

* ``ics``    -> a plain iCalendar URL (``webcal://``/``https://``). Fetched with
  :mod:`httpx` and parsed with :mod:`icalendar`.
* ``caldav`` -> a full CalDAV endpoint (Nextcloud, Google, ...). Accessed via
  the :mod:`caldav` client.

Ingested occurrences are expanded with :mod:`recurring_ical_events` inside the
requested window and returned as normalised dicts. What SyntH *does* with them
depends on the ``EXTERNAL_CAL_TRIGGER_BEATS`` toggle:

* ``False`` (default) -> occurrences only *enrich the prompt context*; SyntH is
  never proactively alerted about them.
* ``True``            -> upcoming occurrences fire ``scheduled_reminder`` Grillo
  beats exactly like internal events.

Privacy note: subscribing an external calendar means its contents can be read by
SyntH and potentially disclosed to third parties in conversation. The WebUI
surfaces an explicit warning before a subscription is created.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.logging_utils import log_debug, log_error, log_info, log_warning

# Supported subscription protocols.
CALENDAR_TYPES: frozenset[str] = frozenset({"ics", "caldav"})

# Provenance prefix stored on materialised events / context entries.
EXTERNAL_SOURCE_PREFIX = "external:"


def external_source_tag(calendar_id: int | str) -> str:
    """Return the canonical ``source`` tag for an external calendar's events."""
    return f"{EXTERNAL_SOURCE_PREFIX}{calendar_id}"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
async def ensure_external_calendars_table() -> None:
    """Create the ``external_calendars`` table if missing (backend-aware)."""
    from core.db import get_conn_ctx, _get_db_type

    is_postgres = _get_db_type() == "postgres"
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                if is_postgres:
                    await cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS external_calendars (
                            id BIGSERIAL PRIMARY KEY,
                            name TEXT NOT NULL,
                            url TEXT NOT NULL,
                            cal_type TEXT NOT NULL DEFAULT 'ics',
                            username TEXT,
                            password_enc TEXT,
                            enabled BOOLEAN DEFAULT TRUE,
                            last_synced TIMESTAMPTZ,
                            last_error TEXT,
                            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
                else:
                    await cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS external_calendars (
                            id INT AUTO_INCREMENT PRIMARY KEY,
                            name VARCHAR(255) NOT NULL,
                            url TEXT NOT NULL,
                            cal_type VARCHAR(20) NOT NULL DEFAULT 'ics',
                            username VARCHAR(255),
                            password_enc TEXT,
                            enabled BOOLEAN DEFAULT 1,
                            last_synced DATETIME,
                            last_error TEXT,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                        """
                    )
        log_debug("[external_calendars] ensured external_calendars table exists")
    except Exception as e:
        log_error(
            f"[external_calendars] Failed to ensure external_calendars table: {repr(e)}"
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
_CALENDAR_COLUMNS = [
    "id",
    "name",
    "url",
    "cal_type",
    "username",
    "password_enc",
    "enabled",
    "last_synced",
    "last_error",
    "created_at",
]


async def list_external_calendars(
    *, only_enabled: bool = False
) -> list[dict[str, Any]]:
    """Return all external calendar subscriptions (passwords stay encrypted)."""
    from core.db import get_conn_ctx

    await ensure_external_calendars_table()
    cols = ", ".join(_CALENDAR_COLUMNS)
    query = f"SELECT {cols} FROM external_calendars"
    if only_enabled:
        query += " WHERE enabled = TRUE"
    query += " ORDER BY id ASC"
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query)
                rows = await cur.fetchall()
    except Exception as e:
        log_error(f"[external_calendars] list failed: {repr(e)}")
        return []
    return [dict(zip(_CALENDAR_COLUMNS, row)) for row in (rows or [])]


def _normalize_subscription_url(url: str, cal_type: str) -> tuple[str, str]:
    """Rewrite provider-specific *embed* URLs into a fetchable feed.

    Google Calendar's public "embed" URL
    (``https://calendar.google.com/calendar/embed?src=<CAL_ID>&ctz=...``) is a
    viewer page, not an iCalendar feed, so it never yields events. The public
    ICS export for the same calendar is
    ``https://calendar.google.com/calendar/ical/<CAL_ID>/public/basic.ics``.

    This transform is purely structural (host / path / query parsing) and
    deterministic — it inspects URL components, never message content or
    keywords. When a Google embed URL is detected, it is rewritten to the ICS
    export URL and the type is forced to ``ics``. All other URLs are returned
    unchanged.
    """
    from urllib.parse import parse_qs, quote, urlsplit

    try:
        parts = urlsplit(url)
    except Exception:
        return url, cal_type

    host = (parts.hostname or "").lower()
    is_google = host == "calendar.google.com" or host.endswith(".google.com")
    if is_google and parts.path.rstrip("/").endswith("/calendar/embed"):
        src_values = parse_qs(parts.query).get("src")
        if src_values and src_values[0]:
            cal_id = quote(src_values[0], safe="")
            ics_url = (
                f"https://calendar.google.com/calendar/ical/{cal_id}/public/basic.ics"
            )
            log_info("[external_calendars] rewrote Google embed URL to public ICS feed")
            return ics_url, "ics"

    return url, cal_type


async def add_external_calendar(
    *,
    name: str,
    url: str,
    cal_type: str = "ics",
    username: str | None = None,
    password: str | None = None,
    enabled: bool = True,
) -> int | None:
    """Insert a new external calendar subscription.

    Credentials (``password``) are encrypted at rest with the same Fernet key
    used for external endpoints. Returns the new row id, or ``None`` on failure.
    """
    from core.db import get_conn_ctx, _get_db_type
    from core.external_endpoints.crypto import encrypt_api_key

    if not name or not url:
        log_warning("[external_calendars] name and url are required")
        return None

    url, cal_type = _normalize_subscription_url(url, cal_type)

    if cal_type not in CALENDAR_TYPES:
        log_warning(f"[external_calendars] invalid cal_type '{cal_type}'")
        return None

    await ensure_external_calendars_table()
    password_enc = encrypt_api_key(password) if password else None
    is_postgres = _get_db_type() == "postgres"
    enabled_value: object = enabled if is_postgres else (1 if enabled else 0)

    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                if is_postgres:
                    await cur.execute(
                        """
                        INSERT INTO external_calendars
                            (name, url, cal_type, username, password_enc, enabled)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (name, url, cal_type, username, password_enc, enabled_value),
                    )
                    row = await cur.fetchone()
                    new_id = int(row[0]) if row else None
                else:
                    await cur.execute(
                        """
                        INSERT INTO external_calendars
                            (name, url, cal_type, username, password_enc, enabled)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (name, url, cal_type, username, password_enc, enabled_value),
                    )
                    new_id = int(cur.lastrowid) if cur.lastrowid else None
        log_info(f"[external_calendars] added calendar '{name}' (id={new_id})")
        return new_id
    except Exception as e:
        log_error(f"[external_calendars] add failed: {repr(e)}")
        return None


async def delete_external_calendar(calendar_id: int) -> bool:
    """Delete an external calendar subscription and its materialised events."""
    from core.db import get_conn_ctx

    await ensure_external_calendars_table()
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM external_calendars WHERE id = %s", (calendar_id,)
                )
                # Remove any events this calendar materialised into scheduled_events.
                await cur.execute(
                    "DELETE FROM scheduled_events WHERE source = %s",
                    (external_source_tag(calendar_id),),
                )
        log_info(f"[external_calendars] deleted calendar id={calendar_id}")
        return True
    except Exception as e:
        log_error(f"[external_calendars] delete failed: {repr(e)}")
        return False


async def set_external_calendar_enabled(calendar_id: int, enabled: bool) -> bool:
    """Enable or disable an external calendar subscription."""
    from core.db import get_conn_ctx, _get_db_type

    await ensure_external_calendars_table()
    is_postgres = _get_db_type() == "postgres"
    enabled_value: object = enabled if is_postgres else (1 if enabled else 0)
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE external_calendars SET enabled = %s WHERE id = %s",
                    (enabled_value, calendar_id),
                )
        return True
    except Exception as e:
        log_error(f"[external_calendars] set_enabled failed: {repr(e)}")
        return False


async def _record_sync_result(calendar_id: int, *, error: str | None = None) -> None:
    """Persist the last sync timestamp and error for a calendar."""
    from core.db import get_conn_ctx, _get_db_type

    is_postgres = _get_db_type() == "postgres"
    now_utc = datetime.now(timezone.utc)
    synced_value: object = (
        now_utc if is_postgres else now_utc.strftime("%Y-%m-%d %H:%M:%S")
    )
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE external_calendars "
                    "SET last_synced = %s, last_error = %s WHERE id = %s",
                    (synced_value, error, calendar_id),
                )
    except Exception as e:
        log_warning(f"[external_calendars] failed to record sync result: {repr(e)}")


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------
def _decode_calendar_bytes(raw: bytes):
    """Parse raw iCalendar bytes into an ``icalendar.Calendar``.

    Returns ``None`` when parsing fails.
    """
    from icalendar import Calendar

    try:
        return Calendar.from_ical(raw)
    except Exception as e:
        log_warning(
            f"[external_calendars] failed to parse iCalendar payload: {repr(e)}"
        )
        return None


def _normalise_ics_url(url: str) -> str:
    """Turn a ``webcal://`` subscription URL into an ``https://`` fetch URL."""
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://") :]
    return url


async def _fetch_ics_calendar(cal: dict[str, Any]):
    """Fetch and parse a plain ICS URL subscription. Returns a Calendar or None."""
    import httpx

    url = _normalise_ics_url(str(cal.get("url") or ""))
    if not url:
        return None

    auth = None
    username = cal.get("username")
    if username:
        from core.external_endpoints.crypto import decrypt_api_key

        password = decrypt_api_key(str(cal.get("password_enc") or ""))
        auth = (str(username), password)

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, auth=auth)
            resp.raise_for_status()
            return _decode_calendar_bytes(resp.content)
    except Exception as e:
        log_warning(
            f"[external_calendars] ICS fetch failed for '{cal.get('name')}': {repr(e)}"
        )
        raise


def _fetch_caldav_calendar_sync(cal: dict[str, Any], start: datetime, end: datetime):
    """Blocking CalDAV fetch. Returns a merged ``icalendar.Calendar`` or None.

    Runs the synchronous :mod:`caldav` client; call via ``asyncio.to_thread``.
    """
    import caldav
    from icalendar import Calendar

    url = str(cal.get("url") or "")
    if not url:
        return None

    username = cal.get("username")
    password = ""
    if cal.get("password_enc"):
        from core.external_endpoints.crypto import decrypt_api_key

        password = decrypt_api_key(str(cal.get("password_enc")))

    merged = Calendar()
    merged.add("prodid", "-//Synthetic Heart//External CalDAV//EN")
    merged.add("version", "2.0")

    try:
        client = caldav.DAVClient(  # type: ignore[call-non-callable]
            url=url,
            username=str(username) if username else None,
            password=password or None,
        )
        principal = client.principal()
        for calendar in principal.calendars():
            try:
                results = calendar.date_search(start=start, end=end, expand=False)
            except Exception:
                results = calendar.events()
            for event in results:
                try:
                    parsed = Calendar.from_ical(event.data)
                    for component in parsed.walk("VEVENT"):
                        merged.add_component(component)
                except Exception:
                    continue
        return merged
    except Exception as e:
        log_warning(
            f"[external_calendars] CalDAV fetch failed for '{cal.get('name')}': {repr(e)}"
        )
        raise


async def fetch_external_occurrences(
    cal: dict[str, Any], *, window_start: datetime, window_end: datetime
) -> list[dict[str, Any]]:
    """Fetch a calendar and expand its occurrences within a window.

    ``window_start``/``window_end`` must be timezone-aware datetimes. Returns a
    list of normalised occurrence dicts with keys:
    ``uid``, ``summary``, ``start`` (aware datetime), ``all_day`` (bool),
    ``source`` (``external:<id>``), ``calendar_name``.
    """
    import asyncio

    import recurring_ical_events

    cal_type = str(cal.get("cal_type") or "ics")
    calendar_id = cal.get("id")

    calendar = None
    if cal_type == "caldav":
        calendar = await asyncio.to_thread(
            _fetch_caldav_calendar_sync, cal, window_start, window_end
        )
    else:
        calendar = await _fetch_ics_calendar(cal)

    if calendar is None:
        return []

    occurrences: list[dict[str, Any]] = []
    try:
        expanded = recurring_ical_events.of(calendar).between(window_start, window_end)
    except Exception as e:
        log_warning(
            f"[external_calendars] occurrence expansion failed for "
            f"'{cal.get('name')}': {repr(e)}"
        )
        return []

    source_tag = (
        external_source_tag(calendar_id) if calendar_id is not None else "external"
    )
    for occ in expanded:
        try:
            dtstart_prop = occ.get("dtstart")
            if dtstart_prop is None:
                continue
            dt_value = dtstart_prop.dt
            all_day = not isinstance(dt_value, datetime)
            if all_day:
                # An all-day VEVENT dtstart is a date; anchor at midnight UTC.
                start_dt = datetime(
                    dt_value.year, dt_value.month, dt_value.day, tzinfo=timezone.utc
                )
            else:
                start_dt = dt_value
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=timezone.utc)
            occurrences.append(
                {
                    "uid": str(occ.get("uid") or ""),
                    "summary": str(occ.get("summary") or ""),
                    "start": start_dt,
                    "all_day": all_day,
                    "source": source_tag,
                    "calendar_name": str(cal.get("name") or ""),
                }
            )
        except Exception:
            continue

    log_debug(
        f"[external_calendars] '{cal.get('name')}' expanded to "
        f"{len(occurrences)} occurrence(s)"
    )
    return occurrences


async def gather_all_external_occurrences(
    *, window_start: datetime, window_end: datetime
) -> list[dict[str, Any]]:
    """Fetch and expand occurrences for every enabled external calendar."""
    calendars = await list_external_calendars(only_enabled=True)
    all_occurrences: list[dict[str, Any]] = []
    for cal in calendars:
        cal_id = cal.get("id")
        try:
            occ = await fetch_external_occurrences(
                cal, window_start=window_start, window_end=window_end
            )
            all_occurrences.extend(occ)
            if cal_id is not None:
                await _record_sync_result(int(cal_id), error=None)
        except Exception as e:
            if cal_id is not None:
                await _record_sync_result(int(cal_id), error=str(e))
    return all_occurrences
