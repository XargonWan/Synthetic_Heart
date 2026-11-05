"""Choosing which of an endpoint's models to start on.

An endpoint that lists a hundred and twenty three models has no meaningful
"default", and the auto-selection used to take whichever one the API happened to
return first. That is how a Venice endpoint came up on a Gemini model, which then
survived a model change (the scope value, not this row, is what the runtime reads).

What is *available* is always the endpoint's own list; this module only decides
which of them to start on, from an ordered list of patterns in
``ENDPOINT_MODEL_PREFERENCES``. Matching is on the model id the endpoint reported,
never on anything a person typed, and no match simply means the first listed model.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Iterable

# Comma or newline separated fnmatch patterns, best first.
PREFERENCES_KEY = "ENDPOINT_MODEL_PREFERENCES"


def model_id(model: Any) -> str:
    """Return the id of a model given either a string or a ``ModelInfo``."""
    for attribute in ("id", "name"):
        value = getattr(model, attribute, None)
        if value:
            return str(value)
    return str(model)


def preference_patterns(raw: str | None) -> list[str]:
    """Split a configured preference string into lowercased patterns."""
    return [
        part.strip().lower()
        for part in re.split(r"[,\n]", str(raw or ""))
        if part.strip()
    ]


def pick_preferred_model(models: Iterable[Any], patterns: Iterable[str]) -> str | None:
    """Return the first model matching the earliest pattern, or ``None``.

    Earlier patterns win over later ones, and within a pattern the endpoint's own
    ordering decides, so a preference never reorders the endpoint's list.
    """
    identifiers = [model_id(model) for model in models]
    for pattern in patterns:
        for identifier in identifiers:
            if fnmatch.fnmatchcase(identifier.lower(), pattern):
                return identifier
    return None


def configured_preferences() -> str:
    """Read the preference string from config, tolerating an unreadable store."""
    try:
        from core.config import config_registry

        return str(config_registry.get_value(PREFERENCES_KEY, "") or "")
    except Exception:
        return ""


def select_default_model(
    models: Iterable[Any], raw_preferences: str | None = None
) -> str | None:
    """Return the model to start an endpoint on, or ``None`` for an empty list.

    A preferred match wins; otherwise the endpoint's first model is used, which is
    what the auto-selection did before preferences existed.
    """
    identifiers = [model_id(model) for model in models]
    if not identifiers:
        return None
    if raw_preferences is None:
        raw_preferences = configured_preferences()
    return (
        pick_preferred_model(identifiers, preference_patterns(raw_preferences))
        or identifiers[0]
    )
