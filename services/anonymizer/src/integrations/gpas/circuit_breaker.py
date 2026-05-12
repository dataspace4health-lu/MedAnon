"""gPAS circuit breaker — prevents cascade failures when gPAS is down.

``GpasUnavailableError`` is raised (not ``ValueError``) whenever gPAS is
unreachable: circuit OPEN, connection refused, or timeout after all retries.
Callers distinguish this from ordinary validation errors and surface HTTP 503.
"""

import os

from utils.circuit_breaker import CircuitBreaker, RedisCircuitBreaker


class GpasUnavailableError(Exception):
    """Raised when gPAS is unreachable (circuit OPEN, connection failure, timeout).

    Distinct from ``ValueError`` so callers can return HTTP 503 and stop
    streaming immediately rather than treating this as a bad-input error.
    """


def _build_gpas_cb() -> CircuitBreaker:
    """Return a Redis-backed circuit breaker when MEDANON_REDIS_URL is set.

    Falls back to per-process CircuitBreaker when Redis is unavailable so
    single-instance deployments are unaffected.
    """
    kwargs = dict(
        name="gpas",
        failure_threshold=int(os.environ.get("GPAS_CB_FAILURE_THRESHOLD", 5)),
        recovery_timeout_sec=float(os.environ.get("GPAS_CB_RECOVERY_TIMEOUT_SEC", 30)),
        window_sec=float(os.environ.get("GPAS_CB_WINDOW_SEC", 60)),
        half_open_probes=int(os.environ.get("GPAS_CB_HALF_OPEN_PROBES", 3)),
    )
    redis_url = os.environ.get("MEDANON_REDIS_URL", "")
    if redis_url:
        try:
            # Share the process-wide pool (utils.redis_pool) so the
            # circuit-breaker doesn't open its own. socket_timeout=1 is the
            # default but actually configured via MEDANON_REDIS_SOCKET_TIMEOUT.
            from utils.redis_pool import get_redis

            r = get_redis(redis_url, decode_responses=True)
            if r is not None:
                r.ping()
                from utils.redis_pool import get_redis

            r = get_redis(redis_url, decode_responses=True)
            if r is not None:
                r.ping()
                return RedisCircuitBreaker(redis_client=r, **kwargs)
        except Exception:
            pass  # Redis unavailable — fall through to in-process breaker
    return CircuitBreaker(**kwargs)


_gpas_circuit_breaker = _build_gpas_cb()
