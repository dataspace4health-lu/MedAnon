"""HTTP proxy client for the optional analytics microservice.

Used by api/routers/analytics.py and api/routers/synthetic.py when
ANALYTICS_SERVICE_URL is set. Falls back to local execution when unset.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode

from integrations.http_client import proxy_post_json, proxy_post_raw


def _analytics_url(path: str) -> str:
    base = os.environ.get("ANALYTICS_SERVICE_URL", "").rstrip("/")
    return f"{base}{path}"


def proxy_analyse_risk(body: bytes, content_type: str) -> dict:
    """Forward a /v1/analyse/risk request to the analytics service.

    Returns the parsed JSON risk report dict.
    Raises ValueError on HTTP errors or connection failures.
    """
    url = _analytics_url("/v1/analyse/risk")
    return proxy_post_json(url, body, content_type=content_type, timeout=60)


def proxy_generate_synthetic(body: bytes, content_type: str, params: dict) -> bytes:
    """Forward a /v1/generate/synthetic request to the analytics service.

    Returns the raw NDJSON response body as bytes (streaming passthrough).
    Raises ValueError on HTTP errors or connection failures.
    """
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    url = _analytics_url(f"/v1/generate/synthetic?{qs}")
    return proxy_post_raw(url, body, content_type=content_type, timeout=120)
