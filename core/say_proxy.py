# Deprecated: /say command and proxy removed.
# The module is kept as a noop compatibility shim in case any
# external code imports it. Functions are no-ops and return
# neutral values.

def set_target(user_id, chat_id):
    """No-op: /say functionality removed."""
    return None


def get_target(user_id):
    """No-op: always return None (no active target)."""
    return None


def clear(user_id):
    """No-op: nothing to clear."""
    return None
