from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

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
