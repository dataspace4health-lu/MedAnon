"""Shared three-state circuit breaker for service integrations.

States:
  CLOSED    — normal operation; failures are counted
  OPEN      — all requests fail-fast without hitting the upstream
  HALF_OPEN — a limited number of probe requests are allowed through

Transitions:
  CLOSED -> OPEN:      failure_count >= threshold within the sliding window
  OPEN -> HALF_OPEN:   after recovery_timeout seconds
  HALF_OPEN -> CLOSED: on success of a probe request
  HALF_OPEN -> OPEN:   on failure of a probe request
"""

import logging
import threading
import time

_log = logging.getLogger("medanon.circuit_breaker")


class CircuitBreaker:
    """Reusable three-state circuit breaker with configurable thresholds."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 30,
        window_sec: float = 60,
        half_open_probes: int = 3,
        timeout_threshold: int = 2,
    ) -> None:
        self._name = name
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failure_count = 0
        self._timeout_count = 0
        self._last_failure_time = 0.0
        self._probes_in_flight = 0
        self._successful_probes = 0
        self._threshold = failure_threshold
        self._timeout_threshold = timeout_threshold
        self._recovery_timeout = recovery_timeout_sec
        self._window = window_sec
        self._window_start = 0.0
        self._half_open_probes = half_open_probes
        self._total_trips = 0

    @property
    def stats(self) -> dict:
        """Return a snapshot of circuit breaker stats for health checks."""
        with self._lock:
            return {
                "name": self._name,
                "state": self._state,
                "failure_count": self._failure_count,
                "total_trips": self._total_trips,
                "threshold": self._threshold,
                "recovery_timeout_sec": self._recovery_timeout,
            }

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow_request(self) -> bool:
        """Return True if the request should proceed.

        In HALF_OPEN state, up to ``half_open_probes`` concurrent probe
        requests are allowed through. All other callers are rejected.
        """
        with self._lock:
            if self._state == self.OPEN:
                if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                    self._state = self.HALF_OPEN
                    self._probes_in_flight = 0
                    self._successful_probes = 0
                    _log.info(
                        "circuit_breaker[%s] state=half_open (recovery probes allowed)",
                        self._name,
                    )

            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                return False
            # HALF_OPEN
            if self._probes_in_flight < self._half_open_probes:
                self._probes_in_flight += 1
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if self._state == self.HALF_OPEN:
                self._successful_probes += 1
                if self._successful_probes >= self._half_open_probes:
                    _log.info(
                        "circuit_breaker[%s] state=closed (%d/%d probes succeeded)",
                        self._name,
                        self._successful_probes,
                        self._half_open_probes,
                    )
                    self._failure_count = 0
                    self._timeout_count = 0
                    self._state = self.CLOSED
            elif self._state == self.CLOSED:
                # Decrement rather than reset — a single success should not
                # erase multiple prior failures within the sliding window.
                self._failure_count = max(0, self._failure_count - 1)
                self._timeout_count = max(0, self._timeout_count - 1)

    def record_failure(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if now - self._window_start > self._window:
                self._failure_count = 0
                self._timeout_count = 0
                self._window_start = now
            self._failure_count += 1
            self._last_failure_time = now
            if self._state == self.HALF_OPEN or self._failure_count >= self._threshold:
                if self._state != self.OPEN:
                    self._total_trips += 1
                self._state = self.OPEN
                _log.warning(
                    "circuit_breaker[%s] state=open failures=%d threshold=%d total_trips=%d",
                    self._name,
                    self._failure_count,
                    self._threshold,
                    self._total_trips,
                )

    def record_timeout(self) -> None:
        """Record a timeout failure — trips the CB faster than generic failures.

        Timeouts are a strong signal that the upstream is overloaded or
        unreachable. The timeout_threshold (default 2) is much lower than
        the generic failure_threshold (default 5).
        """
        with self._lock:
            now = time.monotonic()
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if now - self._window_start > self._window:
                self._failure_count = 0
                self._timeout_count = 0
                self._window_start = now
            self._failure_count += 1
            self._timeout_count += 1
            self._last_failure_time = now
            should_trip = (
                self._state == self.HALF_OPEN
                or self._failure_count >= self._threshold
                or self._timeout_count >= self._timeout_threshold
            )
            if should_trip:
                if self._state != self.OPEN:
                    self._total_trips += 1
                self._state = self.OPEN
                _log.warning(
                    "circuit_breaker[%s] state=open timeouts=%d timeout_threshold=%d total_trips=%d",
                    self._name,
                    self._timeout_count,
                    self._timeout_threshold,
                    self._total_trips,
                )

    def reset(self) -> None:
        """Manually reset to CLOSED state (operational recovery)."""
        with self._lock:
            prev = self._state
            self._state = self.CLOSED
            self._failure_count = 0
            self._timeout_count = 0
            self._probes_in_flight = 0
            self._successful_probes = 0
            if prev != self.CLOSED:
                _log.info(
                    "circuit_breaker[%s] manually reset from %s to closed",
                    self._name,
                    prev,
                )
