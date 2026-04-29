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

Redis-backed shared state
-------------------------
Pass ``redis_client`` (a ``redis.Redis`` instance) to share circuit-breaker
state across all anonymizer replicas.  When Redis is unavailable the breaker
degrades silently to per-process in-memory state so individual replicas still
protect themselves.

Redis key layout (all with TTL = window_sec * 4):
  <prefix>:state          TEXT  — "closed" | "open" | "half_open"
  <prefix>:failures       INT   — failure count in current window
  <prefix>:timeouts       INT   — timeout count in current window
  <prefix>:last_fail      FLOAT — monotonic epoch of last failure
  <prefix>:window_start   FLOAT — start of current failure-count window
  <prefix>:trips          INT   — total trip count (counter, no TTL)
"""

import logging
import threading
import time

_log = logging.getLogger("medanon.circuit_breaker")

# Lazy import of Prometheus metrics — utils.metrics imports prometheus_client
# which is an optional dependency in some test contexts; guard at use sites.
try:
    from utils.metrics import CIRCUIT_BREAKER_STATE, CIRCUIT_BREAKER_TRIPS
    _METRICS_OK = True
except Exception:  # pragma: no cover — defensive
    CIRCUIT_BREAKER_STATE = None
    CIRCUIT_BREAKER_TRIPS = None
    _METRICS_OK = False

_STATE_NUMERIC = {"closed": 0, "half_open": 1, "open": 2}


def _publish_state(name: str, state: str) -> None:
    if _METRICS_OK and CIRCUIT_BREAKER_STATE is not None:
        try:
            CIRCUIT_BREAKER_STATE.labels(name=name).set(_STATE_NUMERIC.get(state, 0))
        except Exception:
            pass


def _publish_trip(name: str) -> None:
    if _METRICS_OK and CIRCUIT_BREAKER_TRIPS is not None:
        try:
            CIRCUIT_BREAKER_TRIPS.labels(name=name).inc()
        except Exception:
            pass


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
                    _publish_state(self._name, self.HALF_OPEN)

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
                    _publish_state(self._name, self.CLOSED)
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
                    _publish_trip(self._name)
                self._state = self.OPEN
                _log.warning(
                    "circuit_breaker[%s] state=open failures=%d threshold=%d total_trips=%d",
                    self._name,
                    self._failure_count,
                    self._threshold,
                    self._total_trips,
                )
                _publish_state(self._name, self.OPEN)

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
                    _publish_trip(self._name)
                self._state = self.OPEN
                _log.warning(
                    "circuit_breaker[%s] state=open timeouts=%d timeout_threshold=%d total_trips=%d",
                    self._name,
                    self._timeout_count,
                    self._timeout_threshold,
                    self._total_trips,
                )
                _publish_state(self._name, self.OPEN)

    def release_probe(self) -> None:
        """Release a probe slot without recording success or failure.

        Use this in a ``finally`` block when ``allow_request()`` returned True
        but the caller's own pre-flight raised before any HTTP call could be
        made.  Without this, the probe slot is permanently consumed and
        eventually blocks all recovery in HALF_OPEN.
        """
        with self._lock:
            self._probes_in_flight = max(0, self._probes_in_flight - 1)

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


# ---------------------------------------------------------------------------
# Redis-backed shared circuit breaker
# ---------------------------------------------------------------------------


class RedisCircuitBreaker(CircuitBreaker):
    """Circuit breaker that persists state in Redis so all replicas share it.

    Falls back to in-process state transparently if Redis is unavailable.
    Each Redis operation is fire-and-forget (errors are logged and ignored)
    so a Redis outage never breaks the protected service path.

    Usage::

        import redis
        r = redis.Redis.from_url(os.environ["MEDANON_REDIS_URL"])
        cb = RedisCircuitBreaker("gpas", redis_client=r)
    """

    _KEY_STATE = "state"
    _KEY_FAILURES = "failures"
    _KEY_TIMEOUTS = "timeouts"
    _KEY_LAST_FAIL = "last_fail"
    _KEY_WINDOW_START = "window_start"
    _KEY_TRIPS = "trips"

    def __init__(self, name: str, redis_client=None, redis_prefix: str = "medanon:cb", **kw) -> None:
        super().__init__(name, **kw)
        self._redis = redis_client
        self._prefix = f"{redis_prefix}:{name}"
        self._ttl = int(self._window * 4)  # keys expire well after window resets

    def _rkey(self, suffix: str) -> str:
        return f"{self._prefix}:{suffix}"

    def _redis_get(self, suffix: str, cast=str, default=None):
        try:
            val = self._redis.get(self._rkey(suffix))
            return cast(val) if val is not None else default
        except Exception:
            return default

    def _redis_incr(self, suffix: str, expire: bool = True) -> None:
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.incr(self._rkey(suffix))
            if expire:
                pipe.expire(self._rkey(suffix), self._ttl)
            pipe.execute()
        except Exception:
            pass

    def _redis_set(self, suffix: str, value, expire: bool = True) -> None:
        try:
            if expire:
                self._redis.setex(self._rkey(suffix), self._ttl, str(value))
            else:
                self._redis.set(self._rkey(suffix), str(value))
        except Exception:
            pass

    def _sync_from_redis(self) -> None:
        """Pull shared state from Redis into local cache (called under _lock)."""
        if self._redis is None:
            return
        try:
            state = self._redis_get(self._KEY_STATE, str, self.CLOSED)
            if state in (self.CLOSED, self.OPEN, self.HALF_OPEN):
                self._state = state
            self._failure_count = self._redis_get(self._KEY_FAILURES, int, 0)
            self._timeout_count = self._redis_get(self._KEY_TIMEOUTS, int, 0)
            self._last_failure_time = self._redis_get(self._KEY_LAST_FAIL, float, 0.0)
            self._window_start = self._redis_get(self._KEY_WINDOW_START, float, 0.0)
            self._total_trips = self._redis_get(self._KEY_TRIPS, int, 0)
        except Exception as exc:
            _log.debug("circuit_breaker[%s] redis_sync_error: %s", self._name, exc)

    def _push_state(self, state: str) -> None:
        """Persist state change to Redis (called under _lock)."""
        self._redis_set(self._KEY_STATE, state)

    def _push_counters(self) -> None:
        """Persist failure counters to Redis (called under _lock)."""
        try:
            pipe = self._redis.pipeline(transaction=False)
            pipe.set(self._rkey(self._KEY_FAILURES), self._failure_count)
            pipe.expire(self._rkey(self._KEY_FAILURES), self._ttl)
            pipe.set(self._rkey(self._KEY_TIMEOUTS), self._timeout_count)
            pipe.expire(self._rkey(self._KEY_TIMEOUTS), self._ttl)
            pipe.set(self._rkey(self._KEY_LAST_FAIL), self._last_failure_time)
            pipe.expire(self._rkey(self._KEY_LAST_FAIL), self._ttl)
            pipe.set(self._rkey(self._KEY_WINDOW_START), self._window_start)
            pipe.expire(self._rkey(self._KEY_WINDOW_START), self._ttl)
            pipe.execute()
        except Exception as exc:
            _log.debug("circuit_breaker[%s] redis_push_error: %s", self._name, exc)

    # Override the three mutating methods to sync with Redis after local update.

    def allow_request(self) -> bool:
        if self._redis is not None:
            with self._lock:
                self._sync_from_redis()
        return super().allow_request()

    def record_success(self) -> None:
        super().record_success()
        if self._redis is not None:
            with self._lock:
                self._push_state(self._state)
                self._push_counters()

    def record_failure(self) -> None:
        super().record_failure()
        if self._redis is not None:
            with self._lock:
                self._push_state(self._state)
                self._push_counters()
                if self._total_trips > 0:
                    self._redis_set(self._KEY_TRIPS, self._total_trips, expire=False)

    def record_timeout(self) -> None:
        super().record_timeout()
        if self._redis is not None:
            with self._lock:
                self._push_state(self._state)
                self._push_counters()
                if self._total_trips > 0:
                    self._redis_set(self._KEY_TRIPS, self._total_trips, expire=False)

    def reset(self) -> None:
        super().reset()
        if self._redis is not None:
            try:
                keys = [self._rkey(s) for s in (
                    self._KEY_STATE, self._KEY_FAILURES, self._KEY_TIMEOUTS,
                    self._KEY_LAST_FAIL, self._KEY_WINDOW_START,
                )]
                self._redis.delete(*keys)
            except Exception:
                pass
