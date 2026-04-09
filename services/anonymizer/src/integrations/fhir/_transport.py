"""Shared FHIR HTTP transport layer — connection pool, config, helpers.

Provides the urllib3 PoolManager, configuration constants, input validators,
and low-level HTTP helper functions used by reader, writer, and bulk modules.
"""

import logging
from utils.json_fast import loads as _json_loads, dumps_bytes as _json_dumps_bytes
import os
import random
import re
import time
import urllib3
from urllib.parse import urlparse

from utils.circuit_breaker import CircuitBreaker
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

_FHIR_POOL_SIZE = int(os.environ.get("FHIR_POOL_SIZE", "10"))
_pool = urllib3.PoolManager(
    num_pools=4,
    maxsize=_FHIR_POOL_SIZE,
    retries=False,
    timeout=urllib3.Timeout(connect=5, read=30),
)

log = logging.getLogger("medanon.fhir_server")

# ---------------------------------------------------------------------------
# Circuit breaker for FHIR server
# ---------------------------------------------------------------------------

_fhir_cb = CircuitBreaker(
    name="fhir",
    failure_threshold=int(os.environ.get("FHIR_CB_FAILURE_THRESHOLD", "5")),
    recovery_timeout_sec=float(os.environ.get("FHIR_CB_RECOVERY_TIMEOUT_SEC", "30")),
    window_sec=float(os.environ.get("FHIR_CB_WINDOW_SEC", "60")),
    half_open_probes=int(os.environ.get("FHIR_CB_HALF_OPEN_PROBES", "3")),
)

# ---------------------------------------------------------------------------
# Retry configuration (cached at module level)
# ---------------------------------------------------------------------------

_FHIR_RETRY_COUNT = int(os.environ.get("FHIR_RETRY_COUNT", 2))
_FHIR_RETRY_BACKOFF = float(os.environ.get("FHIR_RETRY_BACKOFF_SEC", 0.3))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_FHIR_MAX_PAGES = int(os.environ.get("FHIR_MAX_PAGES", "1000"))
_FHIR_PAGE_SIZE = int(os.environ.get("FHIR_PAGE_SIZE", "500"))  # default 500; set to 0 to let server decide
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


# ---------------------------------------------------------------------------
# Unified retry loop (DRY: shared by _do_request and _do_raw_request)
# ---------------------------------------------------------------------------

def _retry_request(method, url, headers, timeout, body, operation, parse_json):
    """Execute an HTTP request with retry, circuit breaker, and timeout separation.

    When *parse_json* is True, parses the response as JSON and raises on HTTP >= 400.
    When False, returns the raw urllib3 response (including 2xx non-200 like 202).
    """
    if not _fhir_cb.allow_request():
        FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
        raise ValueError(f"FHIR server unavailable — circuit breaker OPEN ({operation})")

    t0 = time.perf_counter()
    for attempt in range(_FHIR_RETRY_COUNT + 1):
        try:
            resp = _pool.request(
                method, url, headers=headers, body=body,
                timeout=urllib3.Timeout(connect=5, read=timeout),
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                if should_retry and attempt < _FHIR_RETRY_COUNT:
                    log.warning(
                        "FHIR request %s HTTP %d — retrying (%d/%d)",
                        url, resp.status, attempt + 1, _FHIR_RETRY_COUNT,
                    )
                    time.sleep(_FHIR_RETRY_BACKOFF * (2 ** attempt) * (0.5 + random.random()))
                    continue
                FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
                FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
                if should_retry:
                    _fhir_cb.record_failure()
                body_snippet = resp.data[:400].decode("utf-8", errors="replace") if resp.data else ""
                raise ValueError(f"FHIR server HTTP {resp.status} for {url}: {body_snippet}")

            FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            FHIR_CALL_COUNT.labels(operation=operation, status="ok").inc()
            _fhir_cb.record_success()

            if parse_json:
                return _json_loads(resp.data.decode("utf-8"))
            return resp
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            if attempt < _FHIR_RETRY_COUNT:
                log.warning(
                    "FHIR request %s connection error — retrying (%d/%d)",
                    url, attempt + 1, _FHIR_RETRY_COUNT,
                )
                time.sleep(_FHIR_RETRY_BACKOFF * (2 ** attempt) * (0.5 + random.random()))
                continue
            FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
            _fhir_cb.record_failure()
            raise ValueError(f"FHIR server connection error for {url}: {exc}") from exc


def _do_request(method, url, headers, timeout, body=None, operation="request"):
    """Execute an HTTP request and return parsed JSON. Raises ValueError on errors."""
    return _retry_request(method, url, headers, timeout, body, operation, parse_json=True)


def _do_raw_request(method, url, headers, timeout, body=None, operation="request"):
    """Execute HTTP request and return the raw urllib3 response object."""
    return _retry_request(method, url, headers, timeout, body, operation, parse_json=False)


def _safe_next_url(next_url: str, current_url: str, pinned_origin: list | None = None) -> str:
    """Validate that a pagination link stays on the same origin.

    On the first page, the FHIR server may return links using a different
    public address than the caller-provided base_url.  We allow this **once**
    and then pin all subsequent pages to the server's chosen origin.

    Pass a single-element list as *pinned_origin* to share state across pages.
    On first divergence, the accepted origin is stored in ``pinned_origin[0]``
    and all subsequent pages must match it.  If *pinned_origin* is ``None``
    the behaviour is the same but without cross-page pinning.

    Prevents SSRF where a malicious FHIR server progressively redirects
    pagination links to arbitrary internal services.
    """
    n = urlparse(next_url)
    b = urlparse(current_url)
    if n.scheme not in ("http", "https"):
        raise ValueError(f"Pagination link uses disallowed scheme: {next_url!r}")

    next_origin = (n.scheme, n.netloc)
    current_origin = (b.scheme, b.netloc)

    # If we already pinned an origin from a prior divergence, enforce it.
    if pinned_origin and pinned_origin[0] is not None:
        if next_origin != pinned_origin[0]:
            raise ValueError(
                f"Pagination link origin {n.scheme}://{n.netloc} does not match "
                f"pinned origin {pinned_origin[0][0]}://{pinned_origin[0][1]}"
            )
        return next_url

    # Same origin as the current page — always allowed.
    if next_origin == current_origin:
        return next_url

    # First divergence: server returned a different public address.  Allow it
    # once and pin so subsequent pages cannot hop to yet another origin.
    log.debug(
        "Pagination link origin (%s://%s) differs from request origin (%s://%s) "
        "— accepting and pinning server-issued origin",
        n.scheme, n.netloc, b.scheme, b.netloc,
    )
    if pinned_origin is not None:
        pinned_origin[0] = next_origin
    return next_url


def _get_json(url, token=None, timeout=30, operation="get"):
    headers = _make_headers(token)
    return _do_request("GET", url, headers, timeout, operation=operation)


def _write_json(url, payload, method, token=None, timeout=30, operation="write", target: bool = False):
    """POST or PUT JSON payload to a FHIR server URL; returns parsed response dict."""
    body = _json_dumps_bytes(payload)
    headers = _make_post_headers(token, target=target)
    return _do_request(method, url, headers, timeout, body=body, operation=operation)
