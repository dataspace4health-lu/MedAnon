"""HTTP proxy client for the optional analytics microservice.

Used by api/routers/analytics.py and api/routers/synthetic.py when
ANALYTICS_SERVICE_URL is set. Falls back to local execution when unset.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode

from integrations.http_client import proxy_post_json, proxy_post_raw
from utils.circuit_breaker import CircuitBreaker

_analytics_cb = CircuitBreaker(
    name="analytics",
    failure_threshold=int(os.environ.get("ANALYTICS_CB_FAILURE_THRESHOLD", "5")),
    recovery_timeout_sec=float(os.environ.get("ANALYTICS_CB_RECOVERY_TIMEOUT_SEC", "30")),
    window_sec=float(os.environ.get("ANALYTICS_CB_WINDOW_SEC", "60")),
)


def _analytics_url(path: str) -> str:
    base = os.environ.get("ANALYTICS_SERVICE_URL", "").rstrip("/")
    return f"{base}{path}"


def proxy_analyse_risk(body: bytes, content_type: str) -> dict:
    """Forward a /v1/analyse/risk request to the analytics service.

    Returns the parsed JSON risk report dict.
    Raises ValueError on HTTP errors or connection failures.
    """
    if not _analytics_cb.allow_request():
        raise ValueError("Analytics service unavailable — circuit breaker OPEN")
    url = _analytics_url("/v1/analyse/risk")
    try:
        result = proxy_post_json(url, body, content_type=content_type, timeout=60)
        _analytics_cb.record_success()
        return result
    except Exception:
        _analytics_cb.record_failure()
        raise


def proxy_generate_synthetic(body: bytes, content_type: str, params: dict) -> bytes:
    """Forward a /v1/generate/synthetic request to the analytics service.

    Returns the raw NDJSON response body as bytes (streaming passthrough).
    Raises ValueError on HTTP errors or connection failures.
    """
    if not _analytics_cb.allow_request():
        raise ValueError("Analytics service unavailable — circuit breaker OPEN")
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    url = _analytics_url(f"/v1/generate/synthetic?{qs}")
    try:
        result = proxy_post_raw(url, body, content_type=content_type, timeout=120)
        _analytics_cb.record_success()
        return result
    except Exception:
        _analytics_cb.record_failure()
        raise
