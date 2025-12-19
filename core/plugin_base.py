# core/plugin_base.py

from __future__ import annotations

from typing import TYPE_CHECKING, List

if TYPE_CHECKING:  # pragma: no cover
    from core.history_types import HistoryContribution


class PluginBase:
    """Base class for non-LLM plugins."""

    def __init__(self, config=None):
        self.config = config or {}

    def start(self):
        """Optional initialization logic."""
        pass

    def stop(self):
        """Optional teardown logic."""
        pass

    def get_metadata(self) -> dict:
        """Return plugin metadata such as name, description and version."""
        raise NotImplementedError("get_metadata must be implemented by the plugin")

    def get_history_contributions(self, **kwargs) -> List['HistoryContribution']:
        """Optional: provide history contributions for prompt context.

        Plugins can override this to return one or more contributions. The core
        `HistoryEngine` will handle toggles, ordering, limiting, and dedup.
        """

        return []
