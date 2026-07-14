"""HTTP proxy client for the optional analytics microservice.

Used by api/routers/analytics.py and api/routers/synthetic.py when
ANALYTICS_SERVICE_URL is set. Falls back to local execution when unset.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode

import urllib3

from integrations.http_client import proxy_post_json, proxy_post_raw, ProxyTimeoutError
from utils.circuit_breaker import CircuitBreaker

_analytics_cb = CircuitBreaker(
    name="analytics",
    failure_threshold=int(os.environ.get("ANALYTICS_CB_FAILURE_THRESHOLD", "5")),
    recovery_timeout_sec=float(
        os.environ.get("ANALYTICS_CB_RECOVERY_TIMEOUT_SEC", "30")
    ),
    window_sec=float(os.environ.get("ANALYTICS_CB_WINDOW_SEC", "60")),
)

# Separate connect/read timeouts (operator-tunable). The previous scalar
# timeout coupled both; SLO-driven systems generally want a tight connect
# budget (DNS + TCP handshake) and a generous read budget (analysis time).
_CONNECT_TIMEOUT_SEC = float(os.environ.get("ANALYTICS_TIMEOUT_CONNECT_SEC", "5"))
_RISK_READ_TIMEOUT_SEC = float(os.environ.get("ANALYTICS_TIMEOUT_READ_SEC", "60"))
_SYNTHETIC_READ_TIMEOUT_SEC = float(
    os.environ.get("ANALYTICS_SYNTHETIC_TIMEOUT_READ_SEC", "120")
)

_RISK_TIMEOUT = urllib3.Timeout(
    connect=_CONNECT_TIMEOUT_SEC, read=_RISK_READ_TIMEOUT_SEC
)
_SYNTHETIC_TIMEOUT = urllib3.Timeout(
    connect=_CONNECT_TIMEOUT_SEC,
    read=_SYNTHETIC_READ_TIMEOUT_SEC,
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
        raise ValueError("Analytics service unavailable  circuit breaker OPEN")
    url = _analytics_url("/v1/analyse/risk")
    try:
        result = proxy_post_json(
            url, body, content_type=content_type, timeout=_RISK_TIMEOUT
        )
        if not isinstance(result, dict):
            raise ValueError(
                f"Analytics returned {type(result).__name__}, expected dict"
            )
        _analytics_cb.record_success()
        return result
    except ProxyTimeoutError:
        _analytics_cb.record_timeout()
        raise
    except Exception:
        _analytics_cb.record_failure()
        raise


def proxy_analyse_privacy_risk(body: bytes, content_type: str) -> dict:
    """Forward a /v1/analyse/privacy-risk request to the analytics service.

    Returns the parsed JSON privacy-risk report dict. Raises ValueError on HTTP
    errors or connection failures. Mirrors :func:`proxy_analyse_risk`.
    """
    if not _analytics_cb.allow_request():
        raise ValueError("Analytics service unavailable  circuit breaker OPEN")
    url = _analytics_url("/v1/analyse/privacy-risk")
    try:
        result = proxy_post_json(
            url, body, content_type=content_type, timeout=_RISK_TIMEOUT
        )
        if not isinstance(result, dict):
            raise ValueError(
                f"Analytics returned {type(result).__name__}, expected dict"
            )
        _analytics_cb.record_success()
        return result
    except ProxyTimeoutError:
        _analytics_cb.record_timeout()
        raise
    except Exception:
        _analytics_cb.record_failure()
        raise


def proxy_synthetic_passport(body: bytes, content_type: str) -> dict:
    """Forward a /v1/synthetic/passport request to the analytics service.

    Returns the parsed JSON Synthetic Data Passport dict. Raises ValueError on
    HTTP errors or connection failures. Mirrors :func:`proxy_analyse_risk`.
    """
    if not _analytics_cb.allow_request():
        raise ValueError("Analytics service unavailable  circuit breaker OPEN")
    url = _analytics_url("/v1/synthetic/passport")
    try:
        result = proxy_post_json(
            url, body, content_type=content_type, timeout=_SYNTHETIC_TIMEOUT
        )
        if not isinstance(result, dict):
            raise ValueError(
                f"Analytics returned {type(result).__name__}, expected dict"
            )
        _analytics_cb.record_success()
        return result
    except ProxyTimeoutError:
        _analytics_cb.record_timeout()
        raise
    except Exception:
        _analytics_cb.record_failure()
        raise


def proxy_generate_synthetic(body: bytes, content_type: str, params: dict) -> bytes:
    """Forward a /v1/generate/synthetic request to the analytics service.

    Returns the raw NDJSON response body as bytes (streaming passthrough).
    Raises ValueError on HTTP errors or connection failures.
    """
    if not _analytics_cb.allow_request():
        raise ValueError("Analytics service unavailable  circuit breaker OPEN")
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    url = _analytics_url(f"/v1/generate/synthetic?{qs}")
    try:
        result = proxy_post_raw(
            url,
            body,
            content_type=content_type,
            timeout=_SYNTHETIC_TIMEOUT,
        )
        _analytics_cb.record_success()
        return result
    except ProxyTimeoutError:
        _analytics_cb.record_timeout()
        raise
    except Exception:
        _analytics_cb.record_failure()
        raise
