"""gPAS circuit breaker — prevents cascade failures when gPAS is down.

``GpasUnavailableError`` is raised (not ``ValueError``) whenever gPAS is
unreachable: circuit OPEN, connection refused, or timeout after all retries.
Callers distinguish this from ordinary validation errors and surface HTTP 503.

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


class GpasUnavailableError(Exception):
    """Raised when gPAS is unreachable (circuit OPEN, connection failure, timeout).

    Distinct from ``ValueError`` so callers can return HTTP 503 and stop
    streaming immediately rather than treating this as a bad-input error.
    """


class _CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self):
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._probes_in_flight = 0
        self._threshold = int(os.environ.get("GPAS_CB_FAILURE_THRESHOLD", 5))
        self._recovery_timeout = float(os.environ.get("GPAS_CB_RECOVERY_TIMEOUT_SEC", 30))
        self._window = float(os.environ.get("GPAS_CB_WINDOW_SEC", 60))
        self._window_start = 0.0
        self._half_open_probes = int(os.environ.get("GPAS_CB_HALF_OPEN_PROBES", 3))
        self._total_trips = 0

    @property
    def stats(self) -> dict:
        """Return a snapshot of circuit breaker stats for health checks."""
        with self._lock:
            return {
                "state": self._state,
                "failure_count": self._failure_count,
                "total_trips": self._total_trips,
                "threshold": self._threshold,
                "recovery_timeout_sec": self._recovery_timeout,
            }

    @property
    def state(self):
        """Return the current circuit state.

        The OPEN→HALF_OPEN transition is handled exclusively in
        ``allow_request`` (under the same lock) to avoid duplicate logic.
        """
        with self._lock:
            return self._state

    def allow_request(self):
        """Return True if the request should proceed.

        In HALF_OPEN state, up to ``GPAS_CB_HALF_OPEN_PROBES`` concurrent
        probe requests are allowed through.  All other concurrent callers
        are rejected until a probe succeeds.
        """
        with self._lock:
            # Refresh state (OPEN → HALF_OPEN transition if recovery elapsed)
            if self._state == self.OPEN:
                if time.time() - self._last_failure_time >= self._recovery_timeout:
                    self._state = self.HALF_OPEN
                    self._probes_in_flight = 0
                    gpas_log.info("circuit_breaker state=half_open (recovery probes allowed)")

            if self._state == self.CLOSED:
                return True
            if self._state == self.OPEN:
                return False
            # HALF_OPEN: allow up to _half_open_probes concurrent probes
            if self._probes_in_flight < self._half_open_probes:
                self._probes_in_flight += 1
                return True
            return False

    def record_success(self):
        with self._lock:
            self._failure_count = 0
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if self._state == self.HALF_OPEN:
                gpas_log.info("circuit_breaker state=closed (probe succeeded)")
            self._state = self.CLOSED

    def record_failure(self):
        with self._lock:
            now = time.time()
            self._probes_in_flight = max(0, self._probes_in_flight - 1)
            if now - self._window_start > self._window:
                self._failure_count = 0
                self._window_start = now
            self._failure_count += 1
            self._last_failure_time = now
            if self._state == self.HALF_OPEN or self._failure_count >= self._threshold:
                if self._state != self.OPEN:
                    self._total_trips += 1
                self._state = self.OPEN
                gpas_log.warning(
                    "circuit_breaker state=open failures=%d threshold=%d total_trips=%d",
                    self._failure_count, self._threshold, self._total_trips,
                )

    def reset(self):
        """Manually reset the circuit breaker to CLOSED state.

        Intended for operational recovery (e.g. after confirming gPAS is back).
        """
        with self._lock:
            prev = self._state
            self._state = self.CLOSED
            self._failure_count = 0
            self._probes_in_flight = 0
            if prev != self.CLOSED:
                gpas_log.info("circuit_breaker manually reset from %s to closed", prev)


_gpas_circuit_breaker = _CircuitBreaker()
