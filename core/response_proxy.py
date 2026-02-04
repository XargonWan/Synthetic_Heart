import time

# ⏳ Validity duration for a pending response
TIMEOUT_SECONDS = 60

# user_id: { chat_id, message_id, type, expires_at }
pending_targets = {}


def set_target(user_id, chat_id, message_id, content_type):
    """
    Register a temporary target for a response (e.g. a message pending training).
    """
    expires_at = time.time() + TIMEOUT_SECONDS
    pending_targets[user_id] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "type": content_type,
        "expires_at": expires_at,
    }


def get_target(user_id):
    """
    Retrieve a valid target if not expired.
    """
    entry = pending_targets.get(user_id)
    if not entry:
        return None
    if time.time() > entry["expires_at"]:
        clear_target(user_id)
        return "EXPIRED"
    return entry


def clear_target(user_id):
    """
    Remove an assigned target.
    """
    pending_targets.pop(user_id, None)


def has_pending(user_id):
    """
    Check whether a user has a pending target.
    """
    return user_id in pending_targets
