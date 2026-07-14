"""Shared HTTP client for microservice proxies (analytics, NLP).

Provides a pooled urllib3 client with retry + jitter, replacing
``urllib.request.urlopen`` which creates a new TCP connection per call.
"""

from __future__ import annotations

from utils.json_fast import loads as _json_loads
from utils.logging import REQUEST_ID
from utils.pool_budget import proxy_pool_budget
import logging
import os
import random
import time
import urllib3

_log = logging.getLogger("medanon.http_client")

_POOL_SIZE = proxy_pool_budget()
_RETRY_COUNT = int(os.environ.get("PROXY_RETRY_COUNT", "2"))
_RETRY_BACKOFF = float(os.environ.get("PROXY_RETRY_BACKOFF_SEC", "0.3"))
_DEFAULT_CONNECT_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT_CONNECT_SEC", "5"))

_pool = urllib3.PoolManager(
    num_pools=8,
    maxsize=_POOL_SIZE,
    retries=False,
    timeout=urllib3.Timeout(connect=_DEFAULT_CONNECT_TIMEOUT, read=60),
)


def _upstream_label(url: str) -> str:
    """Best-effort upstream label for metrics  host only, no path/query."""
    try:
        from urllib.parse import urlparse

        host = urlparse(url).hostname or "unknown"
        # Strip docker-compose suffixes like "-lb" so a single label covers
        # all replicas of one logical service.
        return host
    except Exception:
        return "unknown"


def _record_retry(url: str, reason: str) -> None:
    try:
        from utils.metrics import PROXY_RETRIES

        PROXY_RETRIES.labels(upstream=_upstream_label(url), reason=reason).inc()
    except Exception:
        pass  # metrics must never break the data path


# Raised (instead of plain ValueError) when all retry attempts exhaust on a
# connect/read timeout so callers can distinguish timeout failures from other
# upstream errors and call circuit_breaker.record_timeout() accordingly.
class ProxyTimeoutError(ValueError):
    """Upstream did not respond within the timeout on all retry attempts."""


def proxy_request(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict | None = None,
    timeout: float | urllib3.Timeout = 60,
) -> urllib3.HTTPResponse:
    """Execute an HTTP request with retry + jitter on transient errors.

    ``timeout`` accepts either a scalar (interpreted as the read timeout, with
    the connect timeout taken from ``PROXY_TIMEOUT_CONNECT_SEC``) or a
    pre-built :class:`urllib3.Timeout` (use this when callers need separate
    connect/read tuning, e.g. analytics).

    Returns the raw :class:`urllib3.HTTPResponse`.
    Raises ``ValueError`` on non-transient HTTP errors or exhausted retries.
    """
    if isinstance(timeout, urllib3.Timeout):
        request_timeout = timeout
    else:
        request_timeout = urllib3.Timeout(
            connect=_DEFAULT_CONNECT_TIMEOUT,
            read=timeout,
        )
    for attempt in range(_RETRY_COUNT + 1):
        try:
            merged_headers = {"X-Request-ID": REQUEST_ID.get("-")}
            if headers:
                merged_headers.update(headers)
            resp = _pool.request(
                method,
                url,
                body=body,
                headers=merged_headers,
                timeout=request_timeout,
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                if should_retry and attempt < _RETRY_COUNT:
                    _log.warning(
                        "proxy request %s HTTP %d  retrying (%d/%d)",
                        url,
                        resp.status,
                        attempt + 1,
                        _RETRY_COUNT,
                    )
                    _record_retry(url, "http_429" if resp.status == 429 else "http_5xx")
                    time.sleep(_RETRY_BACKOFF * (2**attempt) * (0.5 + random.random()))
                    continue
                detail = (
                    resp.data[:200].decode("utf-8", errors="replace")
                    if resp.data
                    else ""
                )
                raise ValueError(
                    f"Upstream service HTTP {resp.status} for {url}: {detail}"
                )
            return resp
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            if attempt < _RETRY_COUNT:
                _log.warning(
                    "proxy request %s connection error  retrying (%d/%d)",
                    url,
                    attempt + 1,
                    _RETRY_COUNT,
                )
                _record_retry(url, "connection")
                time.sleep(_RETRY_BACKOFF * (2**attempt) * (0.5 + random.random()))
                continue
            if isinstance(
                exc,
                (
                    urllib3.exceptions.ConnectTimeoutError,
                    urllib3.exceptions.ReadTimeoutError,
                    urllib3.exceptions.TimeoutError,
                ),
            ):
                raise ProxyTimeoutError(
                    f"Upstream service timed out for {url}: {exc}"
                ) from exc
            raise ValueError(f"Upstream service unreachable for {url}: {exc}") from exc


def proxy_post_json(
    url: str, body: bytes, content_type: str = "application/json", timeout: float = 60
) -> dict:
    """POST body to URL, return parsed JSON response dict."""
    resp = proxy_request(
        "POST", url, body=body, headers={"Content-Type": content_type}, timeout=timeout
    )
    return _json_loads(resp.data.decode("utf-8"))


def proxy_post_raw(
    url: str, body: bytes, content_type: str = "application/json", timeout: float = 120
) -> bytes:
    """POST body to URL, return raw response bytes."""
    resp = proxy_request(
        "POST", url, body=body, headers={"Content-Type": content_type}, timeout=timeout
    )
    return resp.data


def proxy_post_stream(
    url: str,
    body: bytes,
    content_type: str = "application/json",
    timeout: float = 120,
    chunk_size: int = 65536,
):
    """POST body to URL and yield response bytes in chunks without a full-body buffer.

    Uses urllib3 ``preload_content=False`` so large NDJSON responses (e.g. synthetic
    data generation) are not materialized as a single ``bytes`` object in memory.

    Raises ``ValueError`` on HTTP errors (same semantics as ``proxy_post_raw``).
    Yields ``bytes`` chunks; caller is responsible for iterating to completion.
    """
    for attempt in range(_RETRY_COUNT + 1):
        try:
            merged_headers = {
                "X-Request-ID": REQUEST_ID.get("-"),
                "Content-Type": content_type,
            }
            resp = _pool.request(
                "POST",
                url,
                body=body,
                headers=merged_headers,
                timeout=urllib3.Timeout(connect=5, read=timeout),
                preload_content=False,
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                err_body = resp.read(200).decode("utf-8", errors="replace")
                resp.close()
                if should_retry and attempt < _RETRY_COUNT:
                    _log.warning(
                        "proxy stream %s HTTP %d  retrying (%d/%d)",
                        url,
                        resp.status,
                        attempt + 1,
                        _RETRY_COUNT,
                    )
                    _record_retry(url, "http_429" if resp.status == 429 else "http_5xx")
                    time.sleep(_RETRY_BACKOFF * (2**attempt) * (0.5 + random.random()))
                    continue
                raise ValueError(
                    f"Upstream service HTTP {resp.status} for {url}: {err_body}"
                )
            try:
                yield from resp.stream(chunk_size)
            finally:
                resp.close()
            return
        except ValueError:
            raise
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            if attempt < _RETRY_COUNT:
                _log.warning(
                    "proxy stream %s connection error  retrying (%d/%d)",
                    url,
                    attempt + 1,
                    _RETRY_COUNT,
                )
                _record_retry(url, "connection")
                time.sleep(_RETRY_BACKOFF * (2**attempt) * (0.5 + random.random()))
                continue
            if isinstance(
                exc,
                (
                    urllib3.exceptions.ConnectTimeoutError,
                    urllib3.exceptions.ReadTimeoutError,
                    urllib3.exceptions.TimeoutError,
                ),
            ):
                raise ProxyTimeoutError(
                    f"Upstream service timed out for {url}: {exc}"
                ) from exc
            raise ValueError(f"Upstream service unreachable for {url}: {exc}") from exc
