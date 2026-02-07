from typing import Dict, Union

# Centralized storage for chat attention (wake/sleep) state.
# Keys: chat identifier (str or int), Values: bool (True=awake, False=asleep)
CHAT_ATTENTION_STATE: Dict[Union[str, int], bool] = {}


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


__all__ = ["set_attention", "get_attention", "CHAT_ATTENTION_STATE"]
