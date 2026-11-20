"""Turn a time-window specification into a ``(start, end)`` UTC interval.

Used wherever a model is asked "what period is this question about?" and the answer
has to become a date filter (the Recon memory recollector). It replaces the date
parser of the `memory_search` plugin, which was deleted in commit 3d5cfa10; to read the
old plugin: ``git log --diff-filter=D -- plugins/memory_search/memory_search.py`` and
``git show 3d5cfa10^:plugins/memory_search/memory_search.py``.

The caller states today's date and asks the model for ABSOLUTE dates, so everything
that depends on the human's language ("yesterday", "last week", "先週", "ayer") is the
model's job and never this parser's: it understands only language-neutral structure.

* an ISO interval string (``2026-01-10/2026-01-12``, with or without times);
* a single ISO date or datetime (``2026-01-10``: that whole day);
* an object with ``start`` and/or ``end`` ISO values;
* an object with a numeric ``duration`` (``{"days": 3, "hours": 2}``) counted back
  from now.

There is deliberately no word vocabulary here (see "No keyword-driven product logic"
in ``AGENTS.md``).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple


def parse_time_window(spec: Any) -> Optional[Tuple[datetime, datetime]]:
    """Parse a time_window specification and return (start_dt, end_dt) in UTC.

    Supported forms: an ISO interval string, a single ISO date, an object with
    ``start`` / ``end`` ISO values, or an object with a numeric ``duration``
    (``{'days': n, 'hours': m}``, counted back from now with 20% slack).
    Returns None if it cannot interpret the spec; words are never interpreted.
    """
    now = datetime.now(timezone.utc)

    def _parse_iso_component(s: str, is_end: bool = False) -> Optional[datetime]:
        """Parse a single ISO datetime or date component.

        - Accepts full ISO datetimes, optionally ending with 'Z'
        - Accepts date-only 'YYYY-MM-DD' and returns start-of-day (or end-of-day when is_end=True)
        """
        if not isinstance(s, str) or not s.strip():
            return None
        s2 = s.strip()
        # Normalize trailing Z to +00:00 for fromisoformat
        if s2.endswith("Z"):
            s2 = s2[:-1] + "+00:00"
        # Check date-only YYYY-MM-DD BEFORE fromisoformat: Python ≥3.11 accepts
        # bare dates and returns midnight, which would silently ignore is_end.
        m = re.match(r"^(?P<d>\d{4}-\d{2}-\d{2})$", s2)
        if m:
            if is_end:
                return datetime.fromisoformat(m.group("d") + "T23:59:59.999999+00:00")
            else:
                return datetime.fromisoformat(m.group("d") + "T00:00:00+00:00")
        try:
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    # If dict-like with explicit start/end
    if isinstance(spec, dict):
        start = None
        end = None
        dur = spec.get("duration")
        if spec.get("start"):
            start = _parse_iso_component(str(spec.get("start")), is_end=False)
        if spec.get("end"):
            end = _parse_iso_component(str(spec.get("end")), is_end=True)
        if dur and isinstance(dur, dict):
            d = timedelta(days=int(dur.get("days", 0)), hours=int(dur.get("hours", 0)))
            # apply 20% slack
            d = timedelta(seconds=int(d.total_seconds() * 1.2))
            start = now - d
            end = now
        if start or end:
            if not start:
                start = now - timedelta(days=365 * 10)  # very old fallback
            if not end:
                end = now
            return (start, end)

    # If string form
    if isinstance(spec, str):
        s = spec.strip()

        # ISO interval e.g. '2026-01-10/2026-01-12' or with times
        if "/" in s:
            left, right = [p.strip() for p in s.split("/", 1)]
            start_dt = _parse_iso_component(left, is_end=False)
            end_dt = _parse_iso_component(right, is_end=True)
            if start_dt and end_dt:
                return (start_dt, end_dt)
            return None

        # A single date or datetime: that whole day (or that instant, as given).
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            start_dt = _parse_iso_component(s, is_end=False)
            end_dt = _parse_iso_component(s, is_end=True)
            if start_dt and end_dt:
                return (start_dt, end_dt)

    return None
