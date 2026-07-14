"""Per-upstream bulkheads  fast-fail isolation between integrations.

Complements the process-wide :mod:`utils.thread_pool` semaphore with a small
named-semaphore registry: each upstream service (gPAS, NLP, FHIR, analytics,
scoring) gets its own bounded concurrency budget so that one slow upstream
cannot drain capacity needed by the others.

Usage::

    from utils.bulkhead import bulkhead

    with bulkhead("gpas"):
        response = call_gpas(...)

When the bulkhead is saturated, :class:`UpstreamSaturated` is raised
immediately (no blocking).  Callers map this to HTTP 503 with ``Retry-After``.

Capacity is env-tunable per upstream via ``BULKHEAD_<NAME>_MAX_CONCURRENT``
(defaults below).  Metrics are exposed via :mod:`utils.metrics` so dashboards
can alert on sustained saturation long before the circuit breaker trips.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

_log = logging.getLogger("medanon.bulkhead")

# Conservative defaults sized to comfortably absorb the bundled compose
# stack (3 anonymizer replicas × 8 in-flight calls per upstream = 24 < cap)
# while leaving headroom for parallel job execution.  Operators tune via env.
_DEFAULT_CAPACITY = {
    "gpas": 32,
    "nlp": 16,
    "fhir": 16,
    "analytics": 8,
    "scoring": 16,
    "ai": 4,
}

_BULKHEADS: dict[str, threading.BoundedSemaphore] = {}
_CAPACITY: dict[str, int] = {}
_LOCK = threading.Lock()


class UpstreamSaturated(RuntimeError):
    """Raised when a bulkhead has no free slot and the caller did not wait."""


def _capacity_for(name: str) -> int:
    env_var = f"BULKHEAD_{name.upper()}_MAX_CONCURRENT"
    default = _DEFAULT_CAPACITY.get(name, 16)
    try:
        cap = int(os.environ.get(env_var, str(default)))
    except ValueError:
        cap = default
    return max(1, cap)


def _get(name: str) -> threading.BoundedSemaphore:
    sem = _BULKHEADS.get(name)
    if sem is not None:
        return sem
    with _LOCK:
        sem = _BULKHEADS.get(name)
        if sem is None:
            cap = _capacity_for(name)
            sem = threading.BoundedSemaphore(cap)
            _BULKHEADS[name] = sem
            _CAPACITY[name] = cap
    return sem


def _record_acquired(name: str) -> None:
    try:
        from utils.metrics import BULKHEAD_ACQUIRED

        BULKHEAD_ACQUIRED.labels(upstream=name).inc()
    except Exception:
        pass


def _record_rejected(name: str) -> None:
    try:
        from utils.metrics import BULKHEAD_REJECTED

        BULKHEAD_REJECTED.labels(upstream=name).inc()
    except Exception:
        pass


@contextmanager
def bulkhead(name: str, *, wait_sec: float = 0.0):
    """Acquire a slot in the named bulkhead.

    ``wait_sec=0`` (default) fails fast when saturated; pass a positive value
    to block briefly during transient bursts before giving up.
    """
    sem = _get(name)
    acquired = (
        sem.acquire(timeout=wait_sec) if wait_sec > 0 else sem.acquire(blocking=False)
    )
    if not acquired:
        _record_rejected(name)
        _log.warning(
            "bulkhead_saturated upstream=%s capacity=%d", name, _CAPACITY[name]
        )
        raise UpstreamSaturated(f"bulkhead {name} saturated")
    _record_acquired(name)
    try:
        yield
    finally:
        try:
            sem.release()
        except ValueError:
            # Defensive: should never happen, but a duplicate release would
            # corrupt the semaphore for the lifetime of the process.
            _log.error("bulkhead_double_release upstream=%s", name)


def stats() -> dict[str, dict[str, int]]:
    """Return a snapshot of bulkhead capacities  used by /ready and tests."""
    out: dict[str, dict[str, int]] = {}
    for name, cap in _CAPACITY.items():
        out[name] = {"capacity": cap}
    return out
