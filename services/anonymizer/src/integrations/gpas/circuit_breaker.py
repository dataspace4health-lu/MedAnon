"""gPAS circuit breaker — prevents cascade failures when gPAS is down.

States:
  CLOSED    — normal operation; failures are counted
  OPEN      — all requests fail-fast without hitting gPAS
  HALF_OPEN — a single probe request is allowed through

Transitions:
  CLOSED -> OPEN:      failure_count >= threshold within the window
  OPEN -> HALF_OPEN:   after recovery_timeout seconds
  HALF_OPEN -> CLOSED: on success of the probe request
  HALF_OPEN -> OPEN:   on failure of the probe request
"""

import logging
import os
import threading
import time

gpas_log = logging.getLogger("medanon.gpas")


class _CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self):
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._threshold = int(os.environ.get("GPAS_CB_FAILURE_THRESHOLD", 5))
        self._recovery_timeout = float(os.environ.get("GPAS_CB_RECOVERY_TIMEOUT_SEC", 30))
        self._window = float(os.environ.get("GPAS_CB_WINDOW_SEC", 60))
        self._window_start = 0.0

    @property
    def state(self):
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._last_failure_time >= self._recovery_timeout:
                    self._state = self.HALF_OPEN
                    gpas_log.info("circuit_breaker state=half_open (recovery probe allowed)")
            return self._state

    def allow_request(self):
        """Return True if the request should proceed."""
        return self.state != self.OPEN

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            if self._state == self.HALF_OPEN:
                gpas_log.info("circuit_breaker state=closed (probe succeeded)")
            self._state = self.CLOSED

    def record_failure(self):
        with self._lock:
            now = time.time()
            if now - self._window_start > self._window:
                self._failure_count = 0
                self._window_start = now
            self._failure_count += 1
            self._last_failure_time = now
            if self._state == self.HALF_OPEN or self._failure_count >= self._threshold:
                self._state = self.OPEN
                gpas_log.warning(
                    "circuit_breaker state=open failures=%d threshold=%d",
                    self._failure_count, self._threshold,
                )


_gpas_circuit_breaker = _CircuitBreaker()
