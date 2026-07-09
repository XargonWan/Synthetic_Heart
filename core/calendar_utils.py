# core/calendar_utils.py
"""iCalendar-native helpers for SyntH's scheduled events.

This module centralises the mapping between SyntH's ``scheduled_events`` rows
and the iCalendar (RFC 5545) model. It is used by:

* the auto-migration in :mod:`plugins.event_plugin` (backfill ``uid``/``rrule``),
* :mod:`core.db` recurrence advancing (RRULE-based ``next_run``),
* the WebUI calendar export (``/calendar.ics``),
* the external CalDAV/ICS ingestion.

Timezone rule (hybrid, confirmed with the maintainer):

* ``tzid IS NULL`` -> the event *inherits* the system timezone (``TZ`` config
  var). Its wall-clock ``date``/``time`` is interpreted in the current system
  TZ and therefore shifts when ``TZ`` changes.
* ``tzid`` set -> the event is anchored to that explicit timezone and never
  shifts when the system ``TZ`` changes.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event as ICalEvent

from core.logging_utils import log_debug, log_warning

# Legacy ``recurrence_type`` -> iCalendar RRULE FREQ mapping.
# ``none`` and ``always`` have no RRULE (handled specially by the scheduler).
_RECURRENCE_TO_FREQ: dict[str, str] = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "monthly": "MONTHLY",
}

_FREQ_TO_RECURRENCE: dict[str, str] = {v: k for k, v in _RECURRENCE_TO_FREQ.items()}


def get_calendar_host() -> str:
    """Return a stable host component for iCalendar UIDs.

    Uses ``CALENDAR_UID_HOST`` when set, else the container hostname, else a
    static fallback so UIDs are deterministic across restarts.
    """
    host = os.getenv("CALENDAR_UID_HOST", "").strip()
    if host:
        return host
    try:
        hostname = socket.gethostname().strip()
        if hostname:
            return hostname
    except Exception:  # pragma: no cover - defensive
        pass
    return "synth.local"


def build_event_uid(event_id: int | str) -> str:
    """Return a stable iCalendar UID for an internal event row."""
    return f"synth-{event_id}@{get_calendar_host()}"


def recurrence_to_rrule(recurrence_type: str | None) -> str | None:
    """Map a legacy ``recurrence_type`` to an iCalendar RRULE string.

    Returns ``None`` for ``none``/``always``/unknown (no finite RRULE).
    """
    if not recurrence_type:
        return None
    freq = _RECURRENCE_TO_FREQ.get(recurrence_type.lower())
    if freq is None:
        return None
    return f"FREQ={freq}"


def rrule_to_recurrence(rrule: str | None) -> str:
    """Map an iCalendar RRULE string back to a legacy ``recurrence_type``.

    Only the ``FREQ`` component is considered; unrecognised rules fall back to
    ``none`` so the scheduler treats them as one-shot.
    """
    if not rrule:
        return "none"
    freq: str | None = None
    for part in rrule.split(";"):
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip().upper() == "FREQ":
            freq = value.strip().upper()
            break
    if freq is None:
        return "none"
    return _FREQ_TO_RECURRENCE.get(freq, "none")


def resolve_event_timezone(tzid: str | None, system_tz: ZoneInfo) -> ZoneInfo:
    """Resolve the effective timezone for an event.

    ``tzid`` NULL -> inherit ``system_tz``. A non-NULL but invalid ``tzid``
    logs a warning and also falls back to ``system_tz``.
    """
    if not tzid:
        return system_tz
    try:
        return ZoneInfo(tzid)
    except Exception:
        log_warning(
            f"[calendar_utils] Invalid TZID '{tzid}', inheriting system timezone"
        )
        return system_tz


def advance_next_run_by_rrule(
    current_run_utc: datetime,
    rrule: str | None,
    *,
    tzid: str | None,
    system_tz: ZoneInfo,
) -> datetime | None:
    """Return the next UTC occurrence after ``current_run_utc``.

    Computation is performed in the event's effective timezone so DST and
    month-length transitions are handled correctly, then converted back to UTC.

    Returns ``None`` when the rule is non-recurring (``none``/``always``) or
    unrecognised, so the caller keeps its existing one-shot/always semantics.
    """
    recurrence = rrule_to_recurrence(rrule)
    if recurrence == "none":
        return None

    event_tz = resolve_event_timezone(tzid, system_tz)

    if current_run_utc.tzinfo is None:
        current_run_utc = current_run_utc.replace(tzinfo=timezone.utc)
    local_dt = current_run_utc.astimezone(event_tz)

    if recurrence == "daily":
        from datetime import timedelta

        next_local = local_dt + timedelta(days=1)
    elif recurrence == "weekly":
        from datetime import timedelta

        next_local = local_dt + timedelta(days=7)
    elif recurrence == "monthly":
        import calendar as _calendar

        year = local_dt.year + (local_dt.month // 12)
        month = local_dt.month % 12 + 1
        day = min(local_dt.day, _calendar.monthrange(year, month)[1])
        next_local = local_dt.replace(year=year, month=month, day=day)
    else:  # pragma: no cover - guarded by rrule_to_recurrence
        return None

    return next_local.astimezone(timezone.utc)


def event_row_to_vevent(row: dict, *, system_tz: ZoneInfo) -> ICalEvent:
    """Build an :class:`icalendar.Event` from a ``scheduled_events`` row.

    Expects at least ``id``/``date``/``time``/``description`` and honours the
    optional iCalendar columns (``uid``/``rrule``/``tzid``).
    """
    vevent = ICalEvent()

    event_id = row.get("id")
    uid = row.get("uid") or (
        build_event_uid(event_id) if event_id is not None else None
    )
    if uid:
        vevent.add("uid", uid)

    description = row.get("description") or ""
    vevent.add("summary", description)

    tzid = row.get("tzid")
    event_tz = resolve_event_timezone(tzid, system_tz)

    date_val = row.get("date")
    time_val = row.get("time")
    dtstart = _combine_local(date_val, time_val, event_tz)
    if dtstart is not None:
        vevent.add("dtstart", dtstart)

    created_at = row.get("created_at")
    if isinstance(created_at, datetime):
        vevent.add("dtstamp", created_at)
    else:
        vevent.add("dtstamp", datetime.now(timezone.utc))

    rrule = row.get("rrule")
    if rrule:
        recurrence = rrule_to_recurrence(rrule)
        freq = _RECURRENCE_TO_FREQ.get(recurrence)
        if freq:
            vevent.add("rrule", {"freq": [freq]})

    return vevent


def build_calendar(rows: list[dict], *, system_tz: ZoneInfo) -> Calendar:
    """Serialise a list of event rows into a single VCALENDAR."""
    cal = Calendar()
    cal.add("prodid", "-//Synthetic Heart//SyntH Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    for row in rows:
        try:
            cal.add_component(event_row_to_vevent(row, system_tz=system_tz))
        except Exception as exc:  # pragma: no cover - defensive per-row
            log_warning(
                f"[calendar_utils] Skipping row {row.get('id')} in ICS export: {exc}"
            )
    return cal


def _combine_local(
    date_val: object, time_val: object, event_tz: ZoneInfo
) -> datetime | None:
    """Combine a date and time column value into a tz-aware datetime."""
    from datetime import date as _date, time as _time

    parsed_date: _date | None = None
    if isinstance(date_val, datetime):
        parsed_date = date_val.date()
    elif isinstance(date_val, _date):
        parsed_date = date_val
    elif date_val is not None:
        try:
            parsed_date = datetime.strptime(str(date_val), "%Y-%m-%d").date()
        except Exception:
            log_debug(f"[calendar_utils] Unparseable date: {date_val!r}")
            return None

    if parsed_date is None:
        return None

    parsed_time: _time = _time(0, 0)
    if isinstance(time_val, _time):
        parsed_time = time_val
    elif isinstance(time_val, datetime):
        parsed_time = time_val.time()
    elif time_val is not None:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed_time = datetime.strptime(str(time_val), fmt).time()
                break
            except Exception:
                continue

    return datetime.combine(parsed_date, parsed_time, tzinfo=event_tz)
