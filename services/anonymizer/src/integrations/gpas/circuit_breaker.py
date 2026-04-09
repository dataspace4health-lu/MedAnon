"""gPAS circuit breaker — prevents cascade failures when gPAS is down.

``GpasUnavailableError`` is raised (not ``ValueError``) whenever gPAS is
unreachable: circuit OPEN, connection refused, or timeout after all retries.
Callers distinguish this from ordinary validation errors and surface HTTP 503.
"""

import os

from utils.circuit_breaker import CircuitBreaker


class GpasUnavailableError(Exception):
    """Raised when gPAS is unreachable (circuit OPEN, connection failure, timeout).

    Distinct from ``ValueError`` so callers can return HTTP 503 and stop
    streaming immediately rather than treating this as a bad-input error.
    """


_gpas_circuit_breaker = CircuitBreaker(
    name="gpas",
    failure_threshold=int(os.environ.get("GPAS_CB_FAILURE_THRESHOLD", 5)),
    recovery_timeout_sec=float(os.environ.get("GPAS_CB_RECOVERY_TIMEOUT_SEC", 30)),
    window_sec=float(os.environ.get("GPAS_CB_WINDOW_SEC", 60)),
    half_open_probes=int(os.environ.get("GPAS_CB_HALF_OPEN_PROBES", 3)),
)
