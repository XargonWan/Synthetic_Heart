import time

_say_targets = {}
EXPIRE_SECONDS = 120


def set_target(user_id, chat_id):
    _say_targets[user_id] = {"chat_id": chat_id, "ts": time.time()}


def get_target(user_id):
    data = _say_targets.get(user_id)
    if not data:
        return None
    if time.time() - data["ts"] > EXPIRE_SECONDS:
        clear(user_id)
        return "EXPIRED"
    return data["chat_id"]


def clear(user_id):
    _say_targets.pop(user_id, None)
