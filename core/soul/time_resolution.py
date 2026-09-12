from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

_WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_WEEKDAY_TO_IDX = {name: idx for idx, name in enumerate(_WEEKDAYS)}


@dataclass(slots=True)
class AbsoluteTimeResolver:
    """Resolve relative temporal phrases to absolute dates.

    This is intentionally deterministic and conservative. If a phrase cannot
    be resolved safely it is left untouched.
    """

    current_date: date

    def resolve_text(self, text: str) -> str:
        if not text:
            return text

        resolved = text
        resolved = self._replace_simple_terms(resolved)
        resolved = self._replace_week_labels(resolved)
        resolved = self._replace_explicit_weekdays(resolved)
        resolved = self._replace_relative_day_counts(resolved)
        return resolved

    def _replace_simple_terms(self, text: str) -> str:
        replacements = {
            r"\btoday\b": self.current_date.isoformat(),
            r"\byesterday\b": (self.current_date - timedelta(days=1)).isoformat(),
            r"\btomorrow\b": (self.current_date + timedelta(days=1)).isoformat(),
        }
        out = text
        for pattern, replacement in replacements.items():
            out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
        return out

    def _replace_week_labels(self, text: str) -> str:
        this_week_start = self.current_date - timedelta(
            days=self.current_date.weekday()
        )
        last_week_start = this_week_start - timedelta(days=7)
        next_week_start = this_week_start + timedelta(days=7)

        out = re.sub(
            r"\bthis week\b",
            f"week of {this_week_start.isoformat()}",
            text,
            flags=re.IGNORECASE,
        )
        out = re.sub(
            r"\blast week\b",
            f"week of {last_week_start.isoformat()}",
            out,
            flags=re.IGNORECASE,
        )
        out = re.sub(
            r"\bnext week\b",
            f"week of {next_week_start.isoformat()}",
            out,
            flags=re.IGNORECASE,
        )
        return out

    def _replace_explicit_weekdays(self, text: str) -> str:
        out = text

        def _replace_with_direction(match: re.Match[str]) -> str:
            direction = match.group(1).lower()
            day_name = match.group(2).lower()
            target_idx = _WEEKDAY_TO_IDX[day_name]
            current_idx = self.current_date.weekday()
            delta = (target_idx - current_idx) % 7
            if delta == 0:
                delta = 7
            if direction == "last":
                delta = delta - 7
            target = self.current_date + timedelta(days=delta)
            return target.isoformat()

        out = re.sub(
            r"\b(last|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            _replace_with_direction,
            out,
            flags=re.IGNORECASE,
        )

        def _replace_on_weekday(match: re.Match[str]) -> str:
            day_name = match.group(1).lower()
            target_idx = _WEEKDAY_TO_IDX[day_name]
            current_idx = self.current_date.weekday()
            # Resolve to the same week if possible, otherwise upcoming occurrence.
            delta = target_idx - current_idx
            if delta < 0:
                delta += 7
            target = self.current_date + timedelta(days=delta)
            return f"on {target.isoformat()}"

        out = re.sub(
            r"\bon\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            _replace_on_weekday,
            out,
            flags=re.IGNORECASE,
        )
        return out

    def _replace_relative_day_counts(self, text: str) -> str:
        out = text

        def _ago(match: re.Match[str]) -> str:
            days = int(match.group(1))
            target = self.current_date - timedelta(days=days)
            return target.isoformat()

        def _ahead(match: re.Match[str]) -> str:
            days = int(match.group(1))
            target = self.current_date + timedelta(days=days)
            return target.isoformat()

        out = re.sub(r"\b(\d+)\s+days\s+ago\b", _ago, out, flags=re.IGNORECASE)
        out = re.sub(r"\bin\s+(\d+)\s+days\b", _ahead, out, flags=re.IGNORECASE)
        return out


@dataclass(slots=True)
class TemporalRenderer:
    """Render absolute datetimes as human-relative temporal phrases.

    This is the dual of :class:`AbsoluteTimeResolver`: where the resolver turns
    "tomorrow" → ``2026-04-18`` for storage, the renderer turns a stored
    absolute timestamp back into "tomorrow" / "today" / "in 3 days" etc. at
    injection time (relative to ``now``).

    Storage is always absolute (timezone-aware); relative text is presentation.
    """

    now: datetime

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            self.now = self.now.replace(tzinfo=timezone.utc)

    def render_relative(self, ts: datetime | None) -> str:
        """Return a short human-relative phrase for ``ts`` relative to ``now``."""
        if ts is None:
            return "ongoing"
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = ts - self.now

        if abs(delta) < timedelta(minutes=5):
            if delta >= timedelta(0):
                return "in a few minutes"
            return "just now"

        if delta < timedelta(0):
            return self._render_past(-delta)
        return self._render_future(delta)

    def _render_past(self, delta: timedelta) -> str:
        days = delta.days
        if days == 0:
            seconds = delta.seconds
            if seconds < 3600:
                minutes = seconds // 60
                if minutes < 1:
                    return "just now"
                if minutes == 1:
                    return "a minute ago"
                if minutes < 60:
                    return f"{minutes} minutes ago"
            hours = seconds // 3600
            if hours == 1:
                return "an hour ago"
            if hours < 12:
                return f"{hours} hours ago"
            return "earlier today"
        if days == 1:
            return "yesterday"
        if days <= 6:
            return f"{days} days ago"
        if days <= 13:
            return "last week"
        if days <= 30:
            weeks = days // 7
            if weeks == 1:
                return "last week"
            return f"{weeks} weeks ago"
        return f"{days // 30} months ago"

    def _render_future(self, delta: timedelta) -> str:
        days = delta.days
        if days == 0:
            seconds = delta.seconds
            if seconds < 3600:
                minutes = seconds // 60
                if minutes < 1:
                    return "in a few minutes"
                if minutes == 1:
                    return "in a minute"
                if minutes < 60:
                    return f"in {minutes} minutes"
            hours = seconds // 3600
            if hours == 1:
                return "in an hour"
            if hours < 12:
                return f"in {hours} hours"
            return "later today"
        if days == 1:
            return "tomorrow"
        if days <= 6:
            return f"in {days} days"
        if days <= 13:
            return "next week"
        if days <= 30:
            weeks = days // 7
            if weeks == 1:
                return "next week"
            return f"in {weeks} weeks"
        if days <= 60:
            return "next month"
        return f"in {days // 30} months"
