import time
from collections import defaultdict, deque
from core.interfaces_registry import get_interface_registry
from typing import Union


class _RateLimiter:
    def __init__(self):
        self.records = defaultdict(deque)

    def is_allowed(
        self,
        key: str,
        user_id: Union[int, str],
        interface_name: str,
        max_messages: int,
        window_seconds: int,
        trainer_fraction: float,
        consume: bool = True,
    ) -> bool:
        now = time.time()
        dq = self.records[(key, user_id)]
        while dq and now - dq[0] > window_seconds:
            dq.popleft()

        quota_trainer = int(max_messages * trainer_fraction)
        quota_other = max_messages - quota_trainer

        # Controlla se l'utente è un trainer per questa interfaccia
        registry = get_interface_registry()
        is_trainer = registry.is_trainer(interface_name, user_id)
        quota = quota_trainer if is_trainer else quota_other

        if len(dq) < quota:
            if consume:
                dq.append(now)
            return True
        return False


_limiter = _RateLimiter()


def is_allowed(
    key: str,
    user_id: Union[int, str],
    interface_name: str,
    max_messages: int,
    window_seconds: int,
    trainer_fraction: float,
    consume: bool = True,
) -> bool:
    """Check if a message from ``user_id`` is allowed."""
    return _limiter.is_allowed(
        key,
        user_id,
        interface_name,
        max_messages,
        window_seconds,
        trainer_fraction,
        consume,
    )
