"""Who wrote a plugin.

A plugin names its author or authors with an ``author`` class attribute: a name
(``"XargonWan"``) or a list of names (``["XargonWan", "Scarlet-Raine"]``), the same shapes an
extension's manifest accepts (``author = ...``). A plugin that says nothing is XargonWan's.
A ``get_metadata()`` dict with an ``author`` key wins over the attribute.
"""

from __future__ import annotations

from typing import Any

DEFAULT_AUTHOR = "XargonWan"


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        names: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip() and item.strip() not in names:
                names.append(item.strip())
        return names
    return []


def authors_of(component: object) -> list[str]:
    """The authors of ``component`` (a plugin, an engine): never empty."""
    try:
        getter = getattr(component, "get_metadata", None)
        meta = getter() if callable(getter) else None
        if isinstance(meta, dict):
            named = _names(meta.get("author"))
            if named:
                return named
    except Exception:  # noqa: BLE001 - metadata is informational
        pass
    return _names(getattr(component, "author", None)) or [DEFAULT_AUTHOR]
