"""HTTP proxy client for the optional analytics microservice.

Used by api/routers/analytics.py and api/routers/synthetic.py when
ANALYTICS_SERVICE_URL is set. Falls back to local execution when unset.
"""

from __future__ import annotations

import json
import os
from urllib import error as _uerr
from urllib import request as _ureq
from urllib.parse import urlencode


def _analytics_url(path: str) -> str:
    base = os.environ.get("ANALYTICS_SERVICE_URL", "").rstrip("/")
    return f"{base}{path}"


def proxy_analyse_risk(body: bytes, content_type: str) -> dict:
    """Forward a /v1/analyse/risk request to the analytics service.

    Returns the parsed JSON risk report dict.
    Raises ValueError on HTTP errors or connection failures.
    """
    url = _analytics_url("/v1/analyse/risk")
    req = _ureq.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with _ureq.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except _uerr.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200] if exc.fp else ""
        raise ValueError(f"Analytics service returned {exc.code}: {detail}") from exc
    except _uerr.URLError as exc:
        raise ValueError(f"Analytics service unreachable: {exc.reason}") from exc


def proxy_generate_synthetic(body: bytes, content_type: str, params: dict) -> bytes:
    """Forward a /v1/generate/synthetic request to the analytics service.

    Returns the raw NDJSON response body as bytes (streaming passthrough).
    Raises ValueError on HTTP errors or connection failures.
    """
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    url = _analytics_url(f"/v1/generate/synthetic?{qs}")
    req = _ureq.Request(
        url,
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with _ureq.urlopen(req, timeout=120) as resp:
            return resp.read()
    except _uerr.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200] if exc.fp else ""
        raise ValueError(f"Analytics service returned {exc.code}: {detail}") from exc
    except _uerr.URLError as exc:
        raise ValueError(f"Analytics service unreachable: {exc.reason}") from exc
