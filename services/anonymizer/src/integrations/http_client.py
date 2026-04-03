"""Shared HTTP client for microservice proxies (analytics, NLP).

Provides a pooled urllib3 client with retry + jitter, replacing
``urllib.request.urlopen`` which creates a new TCP connection per call.
"""

from __future__ import annotations

from utils.json_fast import loads as _json_loads
import logging
import os
import random
import time
import urllib3

_log = logging.getLogger("medanon.http_client")

_POOL_SIZE = int(os.environ.get("PROXY_POOL_SIZE", "16"))
_RETRY_COUNT = int(os.environ.get("PROXY_RETRY_COUNT", "2"))
_RETRY_BACKOFF = float(os.environ.get("PROXY_RETRY_BACKOFF_SEC", "0.3"))

_pool = urllib3.PoolManager(
    num_pools=8,
    maxsize=_POOL_SIZE,
    retries=False,
)


def proxy_request(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 60,
) -> urllib3.HTTPResponse:
    """Execute an HTTP request with retry + jitter on transient errors.

    Returns the raw :class:`urllib3.HTTPResponse`.
    Raises ``ValueError`` on non-transient HTTP errors or exhausted retries.
    """
    for attempt in range(_RETRY_COUNT + 1):
        try:
            resp = _pool.request(
                method, url,
                body=body,
                headers=headers or {},
                timeout=timeout,
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                if should_retry and attempt < _RETRY_COUNT:
                    _log.warning(
                        "proxy request %s HTTP %d — retrying (%d/%d)",
                        url, resp.status, attempt + 1, _RETRY_COUNT,
                    )
                    time.sleep(_RETRY_BACKOFF * (2 ** attempt) * (0.5 + random.random()))
                    continue
                detail = resp.data[:200].decode("utf-8", errors="replace") if resp.data else ""
                raise ValueError(f"Upstream service HTTP {resp.status} for {url}: {detail}")
            return resp
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            if attempt < _RETRY_COUNT:
                _log.warning(
                    "proxy request %s connection error — retrying (%d/%d)",
                    url, attempt + 1, _RETRY_COUNT,
                )
                time.sleep(_RETRY_BACKOFF * (2 ** attempt) * (0.5 + random.random()))
                continue
            raise ValueError(f"Upstream service unreachable for {url}: {exc}") from exc


def proxy_post_json(url: str, body: bytes, content_type: str = "application/json",
                    timeout: float = 60) -> dict:
    """POST body to URL, return parsed JSON response dict."""
    resp = proxy_request("POST", url, body=body, headers={"Content-Type": content_type}, timeout=timeout)
    return _json_loads(resp.data.decode("utf-8"))


def proxy_post_raw(url: str, body: bytes, content_type: str = "application/json",
                   timeout: float = 120) -> bytes:
    """POST body to URL, return raw response bytes."""
    resp = proxy_request("POST", url, body=body, headers={"Content-Type": content_type}, timeout=timeout)
    return resp.data
