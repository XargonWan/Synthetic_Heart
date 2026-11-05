"""Per-endpoint circuit breaker for LLM request timeouts.

A wedged backend (observed: the Zen/Gemini engine hung for 37 hours) makes every
request wait the full generation timeout, times the retry count - 30 minutes x 3
per call, for every plugin and background job at once. After a few consecutive
timeouts further calls fail immediately; one *probe* request at a time, with a
short timeout, tests whether the backend recovered.
"""

from __future__ import annotations

import time
from typing import Callable, Dict

THRESHOLD = 2  # consecutive timeouts before the circuit opens
COOLDOWN_SEC = 120.0  # how long calls fail fast before a probe is allowed
PROBE_TIMEOUT_SEC = 300.0  # request timeout used for the probe


class TimeoutBreaker:
    def __init__(
        self,
        threshold: int = THRESHOLD,
        cooldown: float = COOLDOWN_SEC,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self.consecutive = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        return self.consecutive >= self.threshold

    def retry_in(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self.cooldown - (self._clock() - self._opened_at))

    def acquire(self) -> tuple[bool, bool]:
        """Return ``(allowed, is_probe)`` for the next request."""
        if not self.is_open:
            return True, False
        if self.retry_in() > 0 or self._probing:
            return False, False
        self._probing = True
        return True, True

    def record_success(self) -> None:
        self.consecutive = 0
        self._opened_at = None
        self._probing = False

    def record_timeout(self) -> None:
        self.consecutive += 1
        self._probing = False
        if self.is_open:
            self._opened_at = self._clock()

    def release_probe(self) -> None:
        """A probe ended without a verdict (non-timeout error): allow another."""
        self._probing = False


_breakers: Dict[str, TimeoutBreaker] = {}


def get_breaker(endpoint_name: str) -> TimeoutBreaker:
    breaker = _breakers.get(endpoint_name)
    if breaker is None:
        breaker = _breakers[endpoint_name] = TimeoutBreaker()
    return breaker
