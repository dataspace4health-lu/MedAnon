"""gPAS HTTP transport layer  URL resolution, retry loop, cache helpers, domain listing.

Depends on:
  .circuit_breaker  _gpas_circuit_breaker singleton
  .protocol         FHIR Parameters builders and response parsers
"""

import hashlib
import hmac as _hmac_mod
import html
import logging
from utils.json_fast import loads as _json_loads, dumps_bytes as _json_dumps_bytes
import os
import random
import re
import time
import urllib3
from urllib.parse import urlsplit, urlunsplit

import utils.cache as _gpas_cache_mod
from utils.pool_budget import gpas_pool_budget
from utils.logging import REQUEST_ID
from utils.metrics import (
    GPAS_CACHE_HITS,
    GPAS_CACHE_MISSES,
    GPAS_CALL_COUNT,
    GPAS_LATENCY,
)

from .circuit_breaker import GpasUnavailableError, _gpas_circuit_breaker

gpas_log = logging.getLogger("medanon.gpas")

# ---------------------------------------------------------------------------
# Connection pool (reuses TCP/TLS connections across gPAS requests)
# ---------------------------------------------------------------------------

_JOB_WORKERS = int(os.environ.get("MEDANON_JOB_WORKERS", "3"))
_GPAS_POOL_SIZE = gpas_pool_budget()
_gpas_pool = urllib3.PoolManager(
    num_pools=2,
    maxsize=_GPAS_POOL_SIZE,
    retries=False,
    timeout=urllib3.Timeout(connect=5, read=30),
)

# ---------------------------------------------------------------------------
# Well-known path constants
# ---------------------------------------------------------------------------

GPAS_SYSTEM = "https://ths-greifswald.de/gpas"
GPAS_FHIR_BASE_PATH = "/ttp-fhir/fhir/gpas"
GPAS_EXPORT_DOMAINS_PATH = "/gpas-web/html/internal/admin/export.xhtml"


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def _normalize_gpas_base(url):
    """Normalize a gPAS URL to the FHIR base endpoint.

    Accepts either:
      - the FHIR base itself: http://host:port/ttp-fhir/fhir/gpas
      - a child path such as /ttp-fhir/fhir/gpas/metadata
      - a gPAS admin UI URL such as /gpas-web/html/internal/admin/export.xhtml
    """
    parts = urlsplit(str(url).strip())
    path = parts.path.rstrip("/")

    if GPAS_FHIR_BASE_PATH in path:
        normalized_path = path.split(GPAS_FHIR_BASE_PATH, 1)[0] + GPAS_FHIR_BASE_PATH
    elif "/gpas-web/" in path or path.endswith("/gpas-web"):
        normalized_path = GPAS_FHIR_BASE_PATH
    else:
        normalized_path = path or GPAS_FHIR_BASE_PATH

    return urlunsplit((parts.scheme, parts.netloc, normalized_path, "", ""))


def _resolve_gpas_base(params):
    """Return the gPAS base URL (``[base]``) from params or environment."""
    base = params.get("gpas_url") or os.environ.get("GPAS_URL")
    if not base:
        raise ValueError(
            "gPAS base URL is required. Set params.gpas_url or env GPAS_URL "
            "(e.g. https://<host>:<port>/ttp-fhir/fhir/gpas)"
        )
    return _normalize_gpas_base(base)


def _resolve_gpas_admin_url(params):
    """Return the admin export URL used to discover configured domains."""
    admin_url = params.get("gpas_admin_url") or os.environ.get("GPAS_ADMIN_URL")
    if admin_url:
        parts = urlsplit(str(admin_url).strip())
        if "/gpas-web/" in parts.path:
            path = GPAS_EXPORT_DOMAINS_PATH
        else:
            path = parts.path.rstrip("/") + GPAS_EXPORT_DOMAINS_PATH
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    base_url = _resolve_gpas_base(params)
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, GPAS_EXPORT_DOMAINS_PATH, "", ""))


def _resolve_gpas_headers(params):
    """Build HTTP headers for a gPAS FHIR request."""
    headers = {
        "Content-Type": "application/fhir+json",
        "Accept": "application/fhir+json",
        "X-Request-ID": REQUEST_ID.get("-"),
    }
    token = params.get("gpas_token") or os.environ.get("GPAS_TOKEN")
    basic_user = params.get("gpas_basic_user") or os.environ.get("GPAS_BASIC_USER")
    basic_pass = params.get("gpas_basic_pass") or os.environ.get("GPAS_BASIC_PASS")

    if token and basic_user and basic_pass:
        gpas_log.warning(
            "Both GPAS_TOKEN (Bearer) and GPAS_BASIC_USER/GPAS_BASIC_PASS are set. "
            "Bearer token takes precedence  Basic auth credentials will be ignored."
        )

    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif basic_user and basic_pass:
        import base64

        auth = base64.b64encode(f"{basic_user}:{basic_pass}".encode("utf-8")).decode(
            "ascii"
        )
        headers["Authorization"] = f"Basic {auth}"
    return headers


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

# HMAC-SHA256 key used to blind original identifiers in cache keys.
# Without MEDANON_HASH_KEY the blinding degrades to plain SHA-256, which is
# brute-forceable for low-entropy PHI (DOB, MRN, ZIP). We therefore refuse the
# plain fallback unless MEDANON_HASH_ALLOW_PLAIN=true (same gate as the
# ``cryptohash`` action); when refused, caching is disabled rather than storing
# weakly-blinded PHI  gPAS is still called, just without the cache layer.
_HASH_KEY_BYTES: bytes = os.environ.get("MEDANON_HASH_KEY", "").strip().encode()


def _plain_blind_allowed() -> bool:
    return os.environ.get("MEDANON_HASH_ALLOW_PLAIN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _blind_identifier(value: str) -> str:
    """Replace a raw patient identifier with a one-way keyed hash.

    Used as the final component of gPAS pseudonymize cache keys so that
    original identifiers (patient IDs, names, DOBs) never appear in Redis
    or the in-process LRU cache.

    The hash is HMAC-SHA256 keyed with ``MEDANON_HASH_KEY``  deterministic
    (same input always maps to the same key) but not reversible without the
    secret.  This preserves cache hit rates while ensuring no PHI is stored
    in any cache backend.  Without the key, blinding is only attempted when
    ``MEDANON_HASH_ALLOW_PLAIN=true``; otherwise ``_is_cache_enabled`` has
    already turned caching off and this function is not reached for storage.

    De-pseudonymize results are **never** cached because the cache value
    would be the original PHI value  see ``_cache_set`` usage.
    """
    raw = value.encode("utf-8")
    if _HASH_KEY_BYTES:
        digest = _hmac_mod.new(_HASH_KEY_BYTES, raw, hashlib.sha256).hexdigest()
    else:
        digest = hashlib.sha256(raw).hexdigest()
    return digest


def _is_cache_enabled(params):
    raw = params.get("gpas_cache_enabled", os.environ.get("GPAS_CACHE_ENABLED", "true"))
    if str(raw).strip().lower() in ("false", "0", "no", "off"):
        return False
    # Fail safe: never cache PHID-derived keys blinded with plain SHA-256
    # (brute-forceable for low-entropy identifiers) unless explicitly allowed.
    if not _HASH_KEY_BYTES and not _plain_blind_allowed():
        return False
    return True


def _cache_get(cache_key):
    value = _gpas_cache_mod._cache_backend.get(cache_key)
    if value is not None:
        GPAS_CACHE_HITS.inc()
    else:
        GPAS_CACHE_MISSES.inc()
    return value


def _cache_set(cache_key, value):
    _gpas_cache_mod._cache_backend.set(cache_key, value)


def _cache_get_many(cache_keys: list[tuple]) -> dict[tuple, str]:
    """Batch cache lookup. Returns {key: value} for hits only."""
    result = _gpas_cache_mod._cache_backend.get_many(cache_keys)
    hits = len(result)
    misses = len(cache_keys) - hits
    if hits:
        GPAS_CACHE_HITS.inc(hits)
    if misses:
        GPAS_CACHE_MISSES.inc(misses)
    return result


def _cache_set_many(items: dict[tuple, str]) -> None:
    """Batch cache write."""
    if items:
        _gpas_cache_mod._cache_backend.set_many(items)


# ---------------------------------------------------------------------------
# Domain listing
# ---------------------------------------------------------------------------


def _parse_gpas_domains_from_html(html_text):
    # Cap label length and result count to prevent memory exhaustion from
    # a malformed or malicious gPAS admin UI response.
    labels = re.findall(r'data-item-label="([^"]{0,200})"', html_text)
    return sorted(dict.fromkeys(html.unescape(label) for label in labels))[:1000]


def list_gpas_domains(params):
    """Fetch and return configured gPAS domain names from the admin UI."""
    if not _gpas_circuit_breaker.allow_request():
        raise GpasUnavailableError("gPAS circuit breaker OPEN  cannot list domains")
    url = _resolve_gpas_admin_url(params)
    auth_headers = _resolve_gpas_headers(params)
    headers = {"Accept": "text/html"}
    if "Authorization" in auth_headers:
        headers["Authorization"] = auth_headers["Authorization"]
    timeout = float(params.get("gpas_timeout_sec", 30))
    try:
        resp = _gpas_pool.request("GET", url, headers=headers, timeout=timeout)
        body = resp.data.decode("utf-8", errors="replace")
        if resp.status >= 500:
            _gpas_circuit_breaker.record_failure()
            raise ValueError(f"gPAS admin HTTP {resp.status} for {url}")
        _gpas_circuit_breaker.record_success()
        return _parse_gpas_domains_from_html(body)
    except Exception:
        _gpas_circuit_breaker.record_failure()
        raise


# ---------------------------------------------------------------------------
# HTTP transport  retry loop with exponential back-off
# ---------------------------------------------------------------------------


def _call_gpas_operation(base_url, operation, fhir_params, params):
    """POST a FHIR Parameters resource to a gPAS $operation endpoint.

    Args:
        base_url: gPAS [base] URL (e.g. https://host:port/ttp-fhir/fhir/gpas)
        operation: FHIR operation name without $ (e.g. "pseudonymizeAllowCreate")
        fhir_params: dict  the FHIR Parameters JSON body
        params: rule params (for headers / timeout config)
    Returns:
        Parsed JSON response (dict)
    """
    from utils.bulkhead import bulkhead, UpstreamSaturated

    if not _gpas_circuit_breaker.allow_request():
        GPAS_CALL_COUNT.labels(operation=operation, status="error").inc()
        raise GpasUnavailableError(
            f"gPAS is unavailable  circuit breaker OPEN for ${operation}. "
            f"Processing halted; retry after gPAS recovers."
        )

    try:
        with bulkhead(
            "gpas", wait_sec=float(os.environ.get("BULKHEAD_GPAS_WAIT_SEC", "2"))
        ):
            return _call_gpas_operation_impl(base_url, operation, fhir_params, params)
    except UpstreamSaturated as exc:
        GPAS_CALL_COUNT.labels(operation=operation, status="error").inc()
        raise GpasUnavailableError(
            f"gPAS bulkhead saturated for ${operation}  retry shortly"
        ) from exc


def _call_gpas_operation_impl(base_url, operation, fhir_params, params):
    """Inner implementation of _call_gpas_operation  actual HTTP loop.

    Split out so the bulkhead semaphore is not held while raising or while the
    caller serialises results; the slot is owned strictly for the HTTP round
    trip(s) of one chunk.
    """

    url = f"{base_url}/${operation}"
    timeout_sec = float(params.get("gpas_timeout_sec", 30))
    payload = _json_dumps_bytes(fhir_params)

    gpas_log.debug(
        "gpas_call operation=%s parameters=%d",
        operation,
        len(fhir_params.get("parameter", [])),
    )

    retry_count = int(
        params.get("gpas_retry_count", os.environ.get("GPAS_RETRY_COUNT", 2))
    )
    retry_backoff = float(
        params.get(
            "gpas_retry_backoff_sec", os.environ.get("GPAS_RETRY_BACKOFF_SEC", 0.2)
        )
    )
    # Build headers once  env-var and param lookups are redundant on every
    # retry attempt; X-Request-ID is fixed for the lifetime of this call.
    headers = _resolve_gpas_headers(params)

    t0 = time.perf_counter()
    for attempt in range(retry_count + 1):
        try:
            resp = _gpas_pool.request(
                "POST",
                url,
                body=payload,
                headers=headers,
                timeout=urllib3.Timeout(connect=5, read=timeout_sec),
            )
            if resp.status >= 400:
                should_retry = resp.status in (429, 500, 502, 503, 504)
                if should_retry and attempt < retry_count:
                    time.sleep(retry_backoff * (2**attempt) * (0.5 + random.random()))
                    continue

                detail = (
                    resp.data[:500].decode("utf-8", errors="replace")
                    if resp.data
                    else ""
                )
                # Extract diagnostics for internal logging only  never include
                # raw diagnostics in the raised exception because gPAS may echo
                # back the original value (e.g. "Value 'abc-123' not found") which
                # constitutes a PHI leak into API error responses and job error fields.
                try:
                    diagnostics = []
                    outcome = _json_loads(detail)
                    for issue in outcome.get("issue", []):
                        if issue.get("diagnostics"):
                            diagnostics.append(issue["diagnostics"][:200])
                    internal_detail = (
                        "; ".join(diagnostics)[:400] if diagnostics else detail[:200]
                    )
                except Exception:
                    internal_detail = detail[:200]

                gpas_log.debug(
                    "gpas_error_detail operation=%s status=%d detail=%s",
                    operation,
                    resp.status,
                    internal_detail,
                )

                # Build the safe public error message  operational info only.
                # Do not call list_gpas_domains() here: it adds a second HTTP round-trip
                # on every "Unknown domain" error and amplifies load when misconfigured.
                # The full diagnostic is available at DEBUG level above.
                safe_message = (
                    "Unknown domain" if "Unknown domain" in internal_detail else ""
                )

                GPAS_LATENCY.labels(operation=operation).observe(
                    time.perf_counter() - t0
                )
                GPAS_CALL_COUNT.labels(operation=operation, status="error").inc()
                if should_retry:
                    # Retryable server error (5xx/429) with retries exhausted
                    # raise GpasUnavailableError so callers' circuit-breaker
                    # handlers catch this the same way as network failures.
                    _gpas_circuit_breaker.record_failure()
                    raise GpasUnavailableError(
                        f"gPAS HTTP {resp.status} on ${operation}"
                        + (f": {safe_message}" if safe_message else "")
                    )
                raise ValueError(
                    f"gPAS HTTP {resp.status} on ${operation}"
                    + (f": {safe_message}" if safe_message else "")
                )
            body = resp.data.decode("utf-8")
            GPAS_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            GPAS_CALL_COUNT.labels(operation=operation, status="ok").inc()
            _gpas_circuit_breaker.record_success()
            return _json_loads(body)
        except (urllib3.exceptions.HTTPError, OSError) as exc:
            is_timeout = isinstance(
                exc,
                (
                    urllib3.exceptions.ConnectTimeoutError,
                    urllib3.exceptions.ReadTimeoutError,
                    urllib3.exceptions.TimeoutError,
                ),
            )
            if attempt < retry_count:
                time.sleep(retry_backoff * (2**attempt) * (0.5 + random.random()))
                continue
            GPAS_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            GPAS_CALL_COUNT.labels(operation=operation, status="error").inc()
            if is_timeout:
                _gpas_circuit_breaker.record_timeout()
            else:
                _gpas_circuit_breaker.record_failure()
            raise GpasUnavailableError(
                f"gPAS unreachable on ${operation}: {exc}"
            ) from exc

    raise GpasUnavailableError(f"gPAS request failed after retries on ${operation}")
