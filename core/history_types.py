from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence, Union

HistoryEntry = Union[str, dict[str, Any]]


@dataclass
class HistoryContribution:
    """A single history contribution provided by a plugin/interface/core.

    The HistoryEngine is responsible for toggles, ordering, limiting, and dedup.
    """

    name: str
    priority: int
    entries: Sequence[HistoryEntry]

    # Optional gating/tuning
    enabled_var: Optional[str] = None
    max_items: Optional[int] = None

    # Where this contribution should go in the final prompt context.
    # Supported targets are defined by HistoryEngine (e.g. 'history_current_chat',
    # 'history_recent', 'thoughts', 'tags_placeholder', 'memories').
    target: Optional[str] = None
