from typing import Dict, Union, List

from core.variables_engine import register_exposed_var
from core.config_manager import config_registry

# Centralized storage for chat attention (wake/sleep) state.
# Keys: chat identifier (str or int), Values: bool (True=awake, False=asleep)
CHAT_ATTENTION_STATE: Dict[Union[str, int], bool] = {}


# Register exposed variables so the WebUI can adjust wake/sleep trigger phrases
register_exposed_var(
    "CHAT_SLEEP_COMMANDS",
    label="Chat Sleep Commands",
    default="",
    value_type=str,
    ui_type="text",
    description="Comma-separated list of phrases that put a chat to sleep (substring match). Leave empty to disable.",
    scope="core",
    component="chat_attention",
    tags=["interface"],
)

register_exposed_var(
    "CHAT_WAKE_COMMANDS",
    label="Chat Wake Commands",
    default="",
    value_type=str,
    ui_type="text",
    description="Comma-separated list of phrases that wake a chat (substring match). Leave empty to disable.",
    scope="core",
    component="chat_attention",
    tags=["interface"],
)


def _parse_comma_list(value: str) -> List[str]:
    if not value:
        return []
    return [v.strip().lower() for v in str(value).split(",") if v.strip()]


def get_sleep_triggers() -> List[str]:
    """Return configured sleep triggers as lowercase substrings."""
    raw = config_registry.get_value("CHAT_SLEEP_COMMANDS", "")
    return _parse_comma_list(raw)


def get_wake_triggers() -> List[str]:
    """Return configured wake triggers as lowercase substrings."""
    raw = config_registry.get_value("CHAT_WAKE_COMMANDS", "")
    return _parse_comma_list(raw)


def evaluate_triggers(text: str) -> tuple[bool, bool, bool]:
    """Evaluate a message text against configured wake/sleep triggers.

    Returns a tuple: (should_sleep, should_wake, is_wake_sleep_command)
    - should_sleep: True if any sleep trigger matches the text
    - should_wake: True if any wake trigger matches the text
    - is_wake_sleep_command: True if either matched (used to mark prompt engine)

    Note: sleep has priority when both match.
    """
    if not text:
        return False, False, False
    txt = str(text).lower()
    sleep_triggers = get_sleep_triggers()
    wake_triggers = get_wake_triggers()

    should_sleep = any(t in txt for t in sleep_triggers) if sleep_triggers else False
    should_wake = any(t in txt for t in wake_triggers) if wake_triggers else False

    # If both matched, prefer sleep (explicit sleep should have priority)
    if should_sleep:
        should_wake = False

    is_wake_sleep_command = should_sleep or should_wake
    return should_sleep, should_wake, is_wake_sleep_command


def set_attention(scope_id: Union[str, int], value: bool) -> None:
    """Set wake/sleep state for a chat scope.

    Args:
        scope_id: Channel/thread/user id (string or int)
        value: True to set awake, False to set asleep
    """
    CHAT_ATTENTION_STATE[scope_id] = bool(value)


def get_attention(scope_id: Union[str, int], default: bool = True) -> bool:
    """Get wake/sleep state for a chat scope.

    Returns default when not explicitly set.
    """
    return CHAT_ATTENTION_STATE.get(scope_id, default)


__all__ = [
    "set_attention",
    "get_attention",
    "CHAT_ATTENTION_STATE",
    "get_sleep_triggers",
    "get_wake_triggers",
    "evaluate_triggers",
]
