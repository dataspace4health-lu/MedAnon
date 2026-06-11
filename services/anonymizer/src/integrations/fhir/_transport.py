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
from urllib.parse import urlparse, urlunparse

from utils.circuit_breaker import CircuitBreaker
from utils.logging import REQUEST_ID
from utils.metrics import FHIR_CALL_COUNT, FHIR_LATENCY
from utils.pool_budget import fhir_pool_budget


class FhirCircuitBreakerOpen(Exception):
    """Raised when the FHIR circuit breaker is OPEN and requests are rejected."""


__all__ = [
    # Connection pools & logger
    "_pool",  # back-compat alias → _pool_source
    "_pool_source",
    "_pool_target",
    "log",
    # Configuration constants
    "_FHIR_MAX_PAGES",
    "_FHIR_PAGE_SIZE",
    "_FHIR_BULK_POLL_INTERVAL",
    "_FHIR_BULK_POLL_TIMEOUT",
    "_RESOURCE_TYPE_RE",
    "_RESOURCE_ID_RE",
    "_FHIR_ID_INVALID_RE",
    # Validators
    "_validate_resource_type",
    "_validate_resource_id",
    "_sanitise_resource_id",
    # Header helpers
    "_make_headers",
    "_make_post_headers",
    # HTTP helpers
    "_do_request",
    "_do_raw_request",
    # Pagination / I/O helpers
    "_safe_next_url",
    "_get_json",
    "_write_json",
]

# ---------------------------------------------------------------------------
# Connection pools (reuse TCP/TLS connections across requests)
#
# Source and target FHIR servers get *separate* PoolManager instances so that
# heavy writes to the target (e.g. a 318k-resource bulk import) cannot starve
# concurrent reads from the source (e.g. a paginated $everything from another
# job, or the UI patient browser).  This is the bulkhead pattern: one failing
# or saturated downstream cannot drag down the other.
# ---------------------------------------------------------------------------

_FHIR_POOL_SIZE = fhir_pool_budget()

# Per-role timeouts.  Defaults preserve the previous behaviour (5s connect /
# 30s read) so nothing changes unless an operator opts in to tuning them.
_FHIR_SOURCE_CONNECT = float(os.environ.get("FHIR_SOURCE_TIMEOUT_CONNECT_SEC", "5"))
_FHIR_SOURCE_READ = float(os.environ.get("FHIR_SOURCE_TIMEOUT_READ_SEC", "30"))
_FHIR_TARGET_CONNECT = float(os.environ.get("FHIR_TARGET_TIMEOUT_CONNECT_SEC", "5"))
_FHIR_TARGET_READ = float(os.environ.get("FHIR_TARGET_TIMEOUT_READ_SEC", "30"))

_pool_source = urllib3.PoolManager(
    num_pools=4,
    maxsize=_FHIR_POOL_SIZE,
    retries=False,
    timeout=urllib3.Timeout(connect=_FHIR_SOURCE_CONNECT, read=_FHIR_SOURCE_READ),
)
_pool_target = urllib3.PoolManager(
    num_pools=4,
    maxsize=_FHIR_POOL_SIZE,
    retries=False,
    timeout=urllib3.Timeout(connect=_FHIR_TARGET_CONNECT, read=_FHIR_TARGET_READ),
)
# Back-compat alias: legacy callers / external imports keep working.
_pool = _pool_source

log = logging.getLogger("medanon.fhir_server")

# ---------------------------------------------------------------------------
# Circuit breaker for FHIR server
# ---------------------------------------------------------------------------

_fhir_cb = CircuitBreaker(
    name="fhir-source",
    failure_threshold=int(os.environ.get("FHIR_CB_FAILURE_THRESHOLD", "5")),
    recovery_timeout_sec=float(os.environ.get("FHIR_CB_RECOVERY_TIMEOUT_SEC", "30")),
    window_sec=float(os.environ.get("FHIR_CB_WINDOW_SEC", "60")),
    half_open_probes=int(os.environ.get("FHIR_CB_HALF_OPEN_PROBES", "3")),
)

_fhir_target_cb = CircuitBreaker(
    name="fhir-target",
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
_FHIR_PAGE_SIZE = int(
    os.environ.get("FHIR_PAGE_SIZE", "500")
)  # default 500; set to 0 to let server decide
_FHIR_BULK_POLL_INTERVAL = float(os.environ.get("FHIR_BULK_POLL_INTERVAL_SEC", "2"))
_FHIR_BULK_POLL_TIMEOUT = float(os.environ.get("FHIR_BULK_POLL_TIMEOUT_SEC", "3600"))
_RESOURCE_TYPE_RE = re.compile(r"^[A-Z][a-zA-Z]+$")
# FHIR R4 §2.1.0.1: id  ::=  [A-Za-z0-9\-\.]{1,64}
# Underscores (_) are NOT part of the spec and HAPI 7.x rejects them.
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9.\-]{1,64}$")
# Characters not allowed in a FHIR ID — used to sanitise gPAS pseudonyms
# that may contain underscores (e.g. rid_1234567890 → rid-1234567890).
_FHIR_ID_INVALID_RE = re.compile(r"[^A-Za-z0-9.\-]")


def _validate_resource_type(resource_type: str) -> str:
    """Validate that resource_type is a valid FHIR resource type name."""
    if not _RESOURCE_TYPE_RE.match(resource_type):
        raise ValueError(
            f"Invalid FHIR resource type: {resource_type!r}. Must match [A-Z][a-zA-Z]+"
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


def _retry_request(
    method, url, headers, timeout, body, operation, parse_json, target=False
):
    """Execute an HTTP request with retry, circuit breaker, and timeout separation.

    When *parse_json* is True, parses the response as JSON and raises on HTTP >= 400.
    When False, returns the raw urllib3 response (including 2xx non-200 like 202).
    When *target* is True, the target-server circuit breaker is used instead of source.
    """
    cb = _fhir_target_cb if target else _fhir_cb
    pool = _pool_target if target else _pool_source
    role = "target" if target else "source"
    connect_timeout = _FHIR_TARGET_CONNECT if target else _FHIR_SOURCE_CONNECT
    if not cb.allow_request():
        FHIR_CALL_COUNT.labels(operation=operation, status="error", role=role).inc()
        raise FhirCircuitBreakerOpen(
            f"FHIR server unavailable — circuit breaker OPEN ({operation})"
        )

    t0 = time.perf_counter()
    for attempt in range(_FHIR_RETRY_COUNT + 1):
        try:
            resp = pool.request(
                method,
                url,
                headers=headers,
                body=body,
                timeout=urllib3.Timeout(connect=connect_timeout, read=timeout),
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                if should_retry and attempt < _FHIR_RETRY_COUNT:
                    log.warning(
                        "FHIR request %s HTTP %d — retrying (%d/%d)",
                        url,
                        resp.status,
                        attempt + 1,
                        _FHIR_RETRY_COUNT,
                    )
                    time.sleep(
                        _FHIR_RETRY_BACKOFF * (2**attempt) * (0.5 + random.random())
                    )
                    continue
                FHIR_LATENCY.labels(operation=operation, role=role).observe(
                    time.perf_counter() - t0
                )
                FHIR_CALL_COUNT.labels(
                    operation=operation, status="error", role=role
                ).inc()
                if should_retry:
                    cb.record_failure()
                if resp.data and log.isEnabledFor(logging.DEBUG):
                    # Log raw body only at DEBUG so it never surfaces in production
                    # logs or exception messages (response may contain PHI).
                    log.debug(
                        "fhir_error_body status=%s url=%s body=%s",
                        resp.status,
                        url,
                        resp.data[:400].decode("utf-8", errors="replace"),
                    )
                raise ValueError(
                    f"FHIR server HTTP {resp.status} for {url} (see DEBUG log for details)"
                )

            FHIR_LATENCY.labels(operation=operation, role=role).observe(
                time.perf_counter() - t0
            )
            FHIR_CALL_COUNT.labels(operation=operation, status="ok", role=role).inc()
            cb.record_success()

            if parse_json:
                return _json_loads(resp.data.decode("utf-8"))
            return resp
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            if attempt < _FHIR_RETRY_COUNT:
                log.warning(
                    "FHIR request %s connection error — retrying (%d/%d)",
                    url,
                    attempt + 1,
                    _FHIR_RETRY_COUNT,
                )
                time.sleep(_FHIR_RETRY_BACKOFF * (2**attempt) * (0.5 + random.random()))
                continue
            FHIR_LATENCY.labels(operation=operation, role=role).observe(
                time.perf_counter() - t0
            )
            FHIR_CALL_COUNT.labels(operation=operation, status="error", role=role).inc()
            cb.record_failure()
            raise ValueError(f"FHIR server connection error for {url}: {exc}") from exc


def _do_request(
    method, url, headers, timeout, body=None, operation="request", target=False
):
    """Execute an HTTP request and return parsed JSON. Raises ValueError on errors."""
    return _retry_request(
        method, url, headers, timeout, body, operation, parse_json=True, target=target
    )


def _do_raw_request(
    method, url, headers, timeout, body=None, operation="request", target=False
):
    """Execute HTTP request and return the raw urllib3 response object."""
    return _retry_request(
        method, url, headers, timeout, body, operation, parse_json=False, target=target
    )


def _safe_next_url(
    next_url: str, current_url: str, pinned_origin: list | None = None
) -> str:
    """Return *next_url* with its origin normalised to the working origin.

    HAPI FHIR embeds its own configured hostname in ``link[rel=next]`` URLs,
    which often differs from the address the anonymizer uses (e.g. the server
    returns ``http://10.168.192.22:8081/fhir?_getpages=...`` while we connect
    via ``http://fhir-server:8081/fhir``).  Following the server-reported URL
    fails with "Connection refused" inside Docker.

    We therefore *rewrite* any divergent origin back to the origin we know is
    reachable (the origin of *current_url* on the first call, then pinned).

    SSRF safety: the path/query from the server are accepted as-is, but the
    host is always clamped to the known-good pinned origin, so a malicious
    server cannot redirect us to an arbitrary internal address.
    """
    n = urlparse(next_url)
    b = urlparse(current_url)
    if n.scheme not in ("http", "https"):
        raise ValueError(f"Pagination link uses disallowed scheme: {next_url!r}")

    next_origin = (n.scheme, n.netloc)
    current_origin = (b.scheme, b.netloc)

    # Determine the authoritative (reachable) origin for this pagination sequence.
    # On the first call pinned_origin[0] is None; we pin it to the origin we are
    # already successfully talking to (current_url's origin).
    if pinned_origin is not None:
        if pinned_origin[0] is None:
            pinned_origin[0] = current_origin
        auth_origin = pinned_origin[0]
    else:
        auth_origin = current_origin

    # Same origin as authoritative — return unchanged.
    if next_origin == auth_origin:
        return next_url

    # Origin differs: HAPI returned its external hostname.  Rewrite to the
    # authoritative origin so the request reaches the reachable endpoint.
    rewritten = urlunparse(
        (auth_origin[0], auth_origin[1], n.path, n.params, n.query, n.fragment)
    )
    log.debug(
        "Pagination link origin rewritten: %s://%s → %s://%s",
        n.scheme,
        n.netloc,
        auth_origin[0],
        auth_origin[1],
    )
    return rewritten


def _get_json(url, token=None, timeout=30, operation="get"):
    headers = _make_headers(token)
    return _do_request("GET", url, headers, timeout, operation=operation)


def _write_json(
    url,
    payload,
    method,
    token=None,
    timeout=30,
    operation="write",
    target: bool = False,
):
    """POST or PUT JSON payload to a FHIR server URL; returns parsed response dict."""
    body = _json_dumps_bytes(payload)
    headers = _make_post_headers(token, target=target)
    return _do_request(
        method, url, headers, timeout, body=body, operation=operation, target=target
    )
