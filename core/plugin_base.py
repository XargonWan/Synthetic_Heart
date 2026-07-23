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

    def is_enabled(self) -> bool:
        """Return True when this plugin should expose actions in the prompt."""
        return True

    def get_metadata(self) -> dict:
        """Return declarative plugin metadata for the WebUI and docs pipeline.

        The default implementation derives sensible values reflectively so
        existing plugins keep working without changes. Plugins are encouraged
        to override this and return an explicit dict with any of the following
        keys (all optional):

        - ``name`` / ``display_name``: human-friendly plugin name.
        - ``description``: short description shown in the plugin detail pane.
        - ``category``: macro-category for the WebUI grouping. One of
          ``"Core"``, ``"Interfaces"``, ``"Grillo"``, ``"Vessels"``,
          ``"Agent"``, ``"Various"``. When omitted the loader auto-derives it
          from the plugin location/registry.
        - ``guide``: inline Markdown guide (or a relative path to a
          ``guide.md`` file). When omitted the loader looks for a ``guide.md``
          alongside the plugin.
        - ``icon``: relative path to an icon file (``icon.png`` by
          convention). When omitted the loader looks for ``icon.png``
          alongside the plugin and finally falls back to the SyntH logo.
        - ``disable_allowed``: whether the plugin may be disabled at runtime.
          Defaults to ``True`` for everything except a curated core set.
        - ``runnable``: when ``True`` the WebUI shows a "Run Now" button in
          the plugin detail pane that triggers a one-shot action.
        - ``run_action``: the action name POSTed to ``/api/components/run``
          when the "Run Now" button is pressed. Defaults to ``"run_now"``.
        - ``run_label``: caption shown on the "Run Now" button.
        - ``run_title``: tooltip/description shown next to the button.
        - ``version`` / ``author`` / ``type``: informational only.
        """
        return {
            "name": self.get_display_name(),
            "display_name": self.get_display_name(),
            "description": self._reflective_description(),
        }

    def get_display_name(self) -> str:
        """Return a human-friendly name for this plugin."""
        for attr in ("display_name", "friendly_name", "name"):
            value = getattr(self, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return self.__class__.__name__

    def _reflective_description(self) -> str:
        """Best-effort description derived from attributes/docstring."""
        candidate = getattr(self, "description", None)
        if isinstance(candidate, str) and candidate.strip():
            return " ".join(candidate.split())
        doc = (self.__class__.__doc__ or "").strip()
        if doc:
            return " ".join(doc.split())
        return ""

    def get_history_contributions(self, **kwargs) -> List["HistoryContribution"]:
        """Optional: provide history contributions for prompt context.

        Plugins can override this to return one or more contributions. The core
        `HistoryEngine` will handle toggles, ordering, limiting, and dedup.
        """

        return []
