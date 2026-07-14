"""Minimal three-state circuit breaker (in-service copy).

A trimmed version of the anonymizer's ``utils/circuit_breaker.py``  the Trust
Gate is self-contained, so it carries its own copy rather than importing across
service boundaries.
"""

from __future__ import annotations

import threading
import time


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 30.0,
    ) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failures = 0
        self._last_failure = 0.0
        self._threshold = failure_threshold
        self._recovery = recovery_timeout_sec

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._last_failure >= self._recovery:
                    self._state = self.HALF_OPEN
                    return True
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure = time.monotonic()
            if self._state == self.HALF_OPEN or self._failures >= self._threshold:
                self._state = self.OPEN
