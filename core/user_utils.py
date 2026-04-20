# core/user_utils.py
"""User utilities helpers.

This file provides helper functions to consistently extract user display
information across the codebase. It helps avoid duplicated logic and
prevents AttributeError when fields like `full_name` are missing from
SimpleNamespace or other lightweight user objects.
"""

from typing import Any
from types import SimpleNamespace


def get_user_display_name(user: Any | None) -> str:
    """Return the best available display name for a user object.

    Tries the following fields in order and falls back to a default string:
    - full_name
    - first_name
    - username
    - id (stringified)
    - "Unknown"

    Accepts any object (SimpleNamespace, dataclass, or other) and uses
    getattr fallbacks to avoid AttributeError.
    """
    if user is None:
        return "Unknown"

    # Prefer full_name
    full = getattr(user, "full_name", None)
    if full:
        return str(full)

    # Then first_name
    first = getattr(user, "first_name", None)
    if first:
        return str(first)

    # Then username
    username = getattr(user, "username", None)
    if username:
        return str(username)

    # Then id
    uid = getattr(user, "id", None)
    if uid is not None:
        return str(uid)

    return "Unknown"


def get_user_usertag(user: Any | None) -> str:
    """Return the usertag string (@username) or a fallback "(no tag)".

    Safely fetches `.username` and returns a string with '@' prefix if available.
    """
    if user is None:
        return "(no tag)"
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    return "(no tag)"


def ensure_message_user_fields(message: Any) -> None:
    """Ensure that message.from_user has predictable fields.

    This function mutates the message in-place only for objects that
    have settable attributes (SimpleNamespace wrappers). It ensures that
    the following attributes exist on `message.from_user`:
    - full_name
    - first_name
    - username

    The purpose is to centralize defaulting logic and avoid repeated
    getattr/fallbacks across the codebase. Avoids AttributeErrors in
    `build_json_prompt` and similar code paths.
    """
    if not message:
        return
    user = getattr(message, "from_user", None)
    if user is None:
        # Replace with SimpleNamespace so we can safely write attributes
        message.from_user = SimpleNamespace(
            id=0, username=None, first_name=None, full_name=None
        )
        return

    # If it's a SimpleNamespace or has a writable __dict__, set missing attributes
    writable = hasattr(user, "__dict__")
    if not writable:
        return

    # Provide a sensible full_name fallback from first_name, username, id
    if getattr(user, "full_name", None) is None:
        candidate = (
            getattr(user, "first_name", None)
            or getattr(user, "username", None)
            or getattr(user, "id", None)
        )
        if candidate is None:
            candidate = "Unknown"
        try:
            setattr(user, "full_name", str(candidate))
        except Exception:
            pass

    # Ensure first_name exists
    if getattr(user, "first_name", None) is None:
        try:
            setattr(user, "first_name", getattr(user, "full_name", None))
        except Exception:
            pass

    # Ensure username exists - not all interfaces provide username
    if getattr(user, "username", None) is None:
        uname = None
        # If full_name looks like a handle, prefer it
        try:
            uname = getattr(user, "username", None)
        except Exception:
            uname = None
        try:
            setattr(user, "username", uname)
        except Exception:
            pass

    # Ensure the message has a date (UTC datetime) so callers can call isoformat()
    try:
        if getattr(message, "date", None) is None:
            from datetime import datetime

            setattr(message, "date", datetime.utcnow())
    except Exception:
        pass
