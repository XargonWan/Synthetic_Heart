"""Interface Path helpers, shared by extensions and the Core gateway.

An Interface Path is the virtual address of a conversation:
``interface_name/level1[/level2/...]``, split on ``/`` with no escaping. It is
**not** a URL, a route or a file system path. How many levels a path has and what
each means belongs to the interface; the Core only treats the first segment as
the interface name.

The build/parse helpers behave exactly like ``core.interface_path_utils`` (a test
compares them), so an extension can build a path locally and the Core will read it
the same way. This module is standard library only.

Each interface **declares the shape of its paths** in its manifest, as patterns:

``example_chat/{chat}``
    exactly two segments: the id, then any one segment
``example_chat/{chat}[/{thread}]``
    one or two levels (a trailing group in ``[...]`` is optional)
``example_chat/dm/{user}``
    a literal segment (``dm``) and a placeholder

:func:`match_path` checks a concrete path against the declared patterns, and
:func:`match_fields` returns what each placeholder matched. The Core reads the chat and
the thread of an incoming message from placeholders named ``chat`` and ``thread`` (or
from the ``chat_id``/``thread_id`` the interface passes): a path such as
``fluxer_bot/{guild}/{channel}`` says which level is the chat through ``chat_id``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

#: The history table stores the path in ``VARCHAR(512)``.
MAX_PATH_LENGTH = 512
MAX_SEGMENT_LENGTH = 200
MAX_LEVELS = 8

_PLACEHOLDER = re.compile(r"^\{[a-z][a-z0-9_]*\}$")
_OPTIONAL_GROUP = re.compile(r"\[(/[^\[\]]*)\]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def build_interface_path(interface_name: str, *levels: Any) -> str:
    """Join ``interface_name`` and the non-``None`` levels with ``/``."""
    valid = [str(level) for level in levels if level is not None]
    if not valid:
        return interface_name
    return f"{interface_name}/{'/'.join(valid)}"


def parse_interface_path(interface_path: str) -> tuple[str, list[str]]:
    """Return ``(interface_name, [levels])``. Does not validate."""
    if not interface_path:
        return ("", [])
    parts = interface_path.split("/")
    return parts[0], parts[1:]


def is_valid_interface_path(interface_path: str) -> bool:
    """True for a non-empty string whose every ``/`` segment is non-blank."""
    if not interface_path or not isinstance(interface_path, str):
        return False
    return all(part and part.strip() for part in interface_path.split("/"))


def path_problem(interface_path: Any) -> str | None:
    """Why ``interface_path`` cannot be accepted, or ``None`` when it is fine.

    Stricter than :func:`is_valid_interface_path`: it also bounds the total and
    per-segment length and the number of levels, and refuses control characters
    (newlines would corrupt log lines and history).
    """
    if not isinstance(interface_path, str) or not interface_path:
        return "must be a non-empty string"
    if len(interface_path) > MAX_PATH_LENGTH:
        return f"is longer than {MAX_PATH_LENGTH} characters"
    if _CONTROL.search(interface_path):
        return "contains control characters"
    if not is_valid_interface_path(interface_path):
        return "has an empty segment"
    parts = interface_path.split("/")
    if len(parts) - 1 > MAX_LEVELS:
        return f"has more than {MAX_LEVELS} levels"
    if any(len(part) > MAX_SEGMENT_LENGTH for part in parts):
        return f"has a segment longer than {MAX_SEGMENT_LENGTH} characters"
    return None


def expand_pattern(pattern: str) -> list[tuple[str, ...]]:
    """Expand a declared pattern into the concrete shapes it allows.

    ``"chat/{chat}[/{thread}]"`` gives ``[("chat", "{chat}"), ("chat", "{chat}",
    "{thread}")]``. Optional groups must be trailing and each is only allowed after
    the previous one. Raises :class:`ValueError` for a malformed pattern.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("pattern must be a non-empty string")
    groups = _OPTIONAL_GROUP.findall(pattern)
    base = _OPTIONAL_GROUP.sub("", pattern)
    if "[" in base or "]" in base:
        raise ValueError("unbalanced or nested '[...]' in pattern")
    if groups and not pattern.endswith("]"):
        raise ValueError("optional '[...]' groups must come last")
    if "".join(f"[{g}]" for g in groups) != pattern[len(base) :]:
        raise ValueError("optional '[...]' groups must come last")

    def segments(text: str) -> tuple[str, ...]:
        parts = text.split("/")
        for part in parts:
            if not part:
                raise ValueError("empty segment in pattern")
            if "{" in part or "}" in part:
                if not _PLACEHOLDER.match(part):
                    raise ValueError(f"invalid placeholder {part!r}")
        return tuple(parts)

    shapes = [segments(base)]
    current = base
    for group in groups:
        current += group
        shapes.append(segments(current))
    return shapes


def match_fields(interface_path: str, patterns: Iterable[str]) -> dict[str, str] | None:
    """The placeholders of the first declared pattern ``interface_path`` has the shape of.

    ``"example/{chat}[/{thread}]"`` matched by ``"example/r1/t9"`` gives
    ``{"room": "r1", "thread": "t9"}``. ``None`` when no pattern fits. A placeholder
    matches any one non-empty segment; a literal segment must be equal; a malformed
    pattern never matches.
    """
    parts = tuple(interface_path.split("/"))
    for pattern in patterns:
        try:
            shapes = expand_pattern(pattern)
        except ValueError:
            continue
        for shape in shapes:
            if len(shape) != len(parts):
                continue
            if all(
                want == got or _PLACEHOLDER.match(want) is not None
                for want, got in zip(shape, parts, strict=True)
            ):
                return {
                    want[1:-1]: got
                    for want, got in zip(shape, parts, strict=True)
                    if _PLACEHOLDER.match(want) is not None
                }
    return None


def match_path(interface_path: str, patterns: Iterable[str]) -> bool:
    """True if ``interface_path`` has the shape of at least one declared pattern."""
    return match_fields(interface_path, patterns) is not None


#: Used when an interface declares no pattern: the legacy ``chat[/thread]`` shape.
def default_patterns(interface_id: str) -> tuple[str, ...]:
    return (f"{interface_id}/{{chat}}[/{{thread}}]",)
