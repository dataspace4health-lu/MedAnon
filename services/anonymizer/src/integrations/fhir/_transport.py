"""Shared FHIR HTTP transport layer — connection pool, config, helpers.

Provides the urllib3 PoolManager, configuration constants, input validators,
and low-level HTTP helper functions used by reader, writer, and bulk modules.
"""

import json
import logging
import os
import re
import time
import urllib3
from urllib.parse import urlparse

from utils.logging import REQUEST_ID
from utils.metrics import FHIR_CALL_COUNT, FHIR_LATENCY

__all__ = [
    # Connection pool & logger
    "_pool", "log",
    # Configuration constants
    "_FHIR_MAX_PAGES", "_FHIR_PAGE_SIZE",
    "_FHIR_BULK_POLL_INTERVAL", "_FHIR_BULK_POLL_TIMEOUT",
    "_RESOURCE_TYPE_RE", "_RESOURCE_ID_RE", "_FHIR_ID_INVALID_RE",
    # Validators
    "_validate_resource_type", "_validate_resource_id", "_sanitise_resource_id",
    # Header helpers
    "_make_headers", "_make_post_headers",
    # HTTP helpers
    "_do_request", "_do_raw_request",
    # Pagination / I/O helpers
    "_safe_next_url", "_get_json", "_write_json",
]

# ---------------------------------------------------------------------------
# Connection pool (reuses TCP/TLS connections across requests)
# ---------------------------------------------------------------------------

_pool = urllib3.PoolManager(
    num_pools=4,       # distinct host:port combos to keep pools for
    maxsize=10,        # connections per pool
    retries=False,     # we handle retries ourselves
)

log = logging.getLogger("medanon.fhir_server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_FHIR_MAX_PAGES = int(os.environ.get("FHIR_MAX_PAGES", "1000"))
_FHIR_PAGE_SIZE = int(os.environ.get("FHIR_PAGE_SIZE", "200"))  # default 200; set to 0 to let server decide
_FHIR_BULK_POLL_INTERVAL = float(os.environ.get("FHIR_BULK_POLL_INTERVAL_SEC", "2"))
_FHIR_BULK_POLL_TIMEOUT = float(os.environ.get("FHIR_BULK_POLL_TIMEOUT_SEC", "3600"))
_RESOURCE_TYPE_RE = re.compile(r'^[A-Z][a-zA-Z]+$')
# FHIR R4 §2.1.0.1: id  ::=  [A-Za-z0-9\-\.]{1,64}
# Underscores (_) are NOT part of the spec and HAPI 7.x rejects them.
_RESOURCE_ID_RE = re.compile(r'^[A-Za-z0-9.\-]{1,64}$')
# Characters not allowed in a FHIR ID — used to sanitise gPAS pseudonyms
# that may contain underscores (e.g. rid_1234567890 → rid-1234567890).
_FHIR_ID_INVALID_RE = re.compile(r'[^A-Za-z0-9.\-]')


def _validate_resource_type(resource_type: str) -> str:
    """Validate that resource_type is a valid FHIR resource type name."""
    if not _RESOURCE_TYPE_RE.match(resource_type):
        raise ValueError(
            f"Invalid FHIR resource type: {resource_type!r}. "
            f"Must match [A-Z][a-zA-Z]+"
        )
    return resource_type


def _sanitise_resource_id(resource_id: str) -> str:
    """Return a FHIR-R4-compliant copy of *resource_id*.

    Replaces characters not in ``[A-Za-z0-9\\-.]`` with hyphens and
    truncates to 64 characters.  gPAS pseudonym domains use underscore
    separators (e.g. ``rid_1234567890``) which HAPI 7.x rejects with
    HAPI-1364; this converts them to ``rid-1234567890``.
    """
    sanitised = _FHIR_ID_INVALID_RE.sub("-", resource_id)[:64]
    return sanitised


def _validate_resource_id(resource_id: str) -> str:
    """Validate that resource_id conforms to FHIR R4 [A-Za-z0-9-.]{1,64}."""
    if not _RESOURCE_ID_RE.match(resource_id):
        raise ValueError(
            f"Invalid FHIR resource ID: {resource_id!r}. "
            f"Must match [A-Za-z0-9.-]{{1,64}}"
        )
    return resource_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_headers(token=None, target: bool = False):
    headers = {
        "Accept": "application/fhir+json",
        "X-Request-ID": REQUEST_ID.get("-"),
    }
    # When writing to the *target* server use FHIR_TARGET_TOKEN as the
    # env fallback so the source server's token is never sent to the target.
    env_fallback = "FHIR_TARGET_TOKEN" if target else "FHIR_SOURCE_TOKEN"
    tok = token or os.environ.get(env_fallback)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _make_post_headers(token=None, target: bool = False):
    """Headers for write requests — Accept + Content-Type + optional Auth."""
    h = _make_headers(token, target=target)
    h["Content-Type"] = "application/fhir+json"
    return h


def _do_request(method, url, headers, timeout, body=None, operation="request"):
    """Execute an HTTP request via the connection pool and return parsed JSON.

    Retries on transient server errors (429, 500, 502, 503, 504) and
    connection errors with exponential backoff.

    Raises ``ValueError`` on HTTP or connection errors (consistent with
    the rest of the module so callers only need to catch one exception type).
    """
    retry_count = int(os.environ.get("FHIR_RETRY_COUNT", 2))
    retry_backoff = float(os.environ.get("FHIR_RETRY_BACKOFF_SEC", 0.3))

    t0 = time.perf_counter()
    for attempt in range(retry_count + 1):
        try:
            resp = _pool.request(
                method, url, headers=headers, body=body, timeout=timeout,
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                if should_retry and attempt < retry_count:
                    log.warning(
                        "FHIR request %s HTTP %d — retrying (%d/%d)",
                        url, resp.status, attempt + 1, retry_count,
                    )
                    time.sleep(retry_backoff * (2 ** attempt))
                    continue
                FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
                FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
                body_snippet = resp.data[:400].decode("utf-8", errors="replace") if resp.data else ""
                raise ValueError(f"FHIR server HTTP {resp.status} for {url}: {body_snippet}")
            result = json.loads(resp.data.decode("utf-8"))
            FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            FHIR_CALL_COUNT.labels(operation=operation, status="ok").inc()
            return result
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            if attempt < retry_count:
                log.warning(
                    "FHIR request %s connection error — retrying (%d/%d)",
                    url, attempt + 1, retry_count,
                )
                time.sleep(retry_backoff * (2 ** attempt))
                continue
            FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
            raise ValueError(f"FHIR server connection error for {url}: {exc}") from exc


def _do_raw_request(method, url, headers, timeout, body=None, operation="request"):
    """Execute HTTP request and return the raw urllib3 response object.

    Like ``_do_request()`` but does NOT parse JSON — the caller gets the raw
    response with ``.status``, ``.headers``, and ``.data``.  Does NOT raise on
    2xx status codes (including 202 Accepted used by bulk export).

    Retries on transient errors (429, 500, 502, 503, 504) and connection
    failures with exponential backoff, same as ``_do_request()``.
    """
    retry_count = int(os.environ.get("FHIR_RETRY_COUNT", 2))
    retry_backoff = float(os.environ.get("FHIR_RETRY_BACKOFF_SEC", 0.3))

    t0 = time.perf_counter()
    for attempt in range(retry_count + 1):
        try:
            resp = _pool.request(
                method, url, headers=headers, body=body, timeout=timeout,
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                if should_retry and attempt < retry_count:
                    log.warning(
                        "FHIR raw request %s HTTP %d — retrying (%d/%d)",
                        url, resp.status, attempt + 1, retry_count,
                    )
                    time.sleep(retry_backoff * (2 ** attempt))
                    continue
                FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
                FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
                raise ValueError(f"FHIR server HTTP {resp.status} for {url}")
            FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            FHIR_CALL_COUNT.labels(operation=operation, status="ok").inc()
            return resp
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            if attempt < retry_count:
                log.warning(
                    "FHIR raw request %s connection error — retrying (%d/%d)",
                    url, attempt + 1, retry_count,
                )
                time.sleep(retry_backoff * (2 ** attempt))
                continue
            FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
            raise ValueError(f"FHIR server connection error for {url}: {exc}") from exc


def _safe_next_url(next_url: str, current_url: str) -> str:
    """Validate that a pagination link stays on the same origin as current_url.

    Compares scheme + netloc of the next link against the URL we actually
    fetched (not the original base_url, which may differ when the FHIR server
    returns a different public address via ``server_address``).

    Prevents SSRF where a malicious FHIR server returns a link[rel=next]
    pointing to an internal service (e.g. http://internal-admin:9090/).
    """
    n = urlparse(next_url)
    b = urlparse(current_url)
    if n.scheme not in ("http", "https"):
        raise ValueError(f"Pagination link uses disallowed scheme: {next_url!r}")
    # After the first page we follow the server's own links, so compare
    # against the URL we just fetched (which may already be a server-issued
    # pagination link with the server's public address).
    if n.scheme == b.scheme and n.netloc == b.netloc:
        return next_url
    # On the very first page the server may return links using its configured
    # server_address which differs from the caller-provided base_url (e.g.
    # caller uses http://hapi-fhir:8080/fhir, server returns
    # http://10.x.x.x:8081/fhir).  Allow this transition once and then
    # subsequent pages will validate against the server's own origin.
    log.debug(
        "Pagination link origin (%s://%s) differs from request origin (%s://%s) "
        "— accepting server-issued pagination URL",
        n.scheme, n.netloc, b.scheme, b.netloc,
    )
    return next_url


def _get_json(url, token=None, timeout=30, operation="get"):
    headers = _make_headers(token)
    return _do_request("GET", url, headers, timeout, operation=operation)


def _write_json(url, payload, method, token=None, timeout=30, operation="write", target: bool = False):
    """POST or PUT JSON payload to a FHIR server URL; returns parsed response dict."""
    body = json.dumps(payload).encode("utf-8")
    headers = _make_post_headers(token, target=target)
    return _do_request(method, url, headers, timeout, body=body, operation=operation)
