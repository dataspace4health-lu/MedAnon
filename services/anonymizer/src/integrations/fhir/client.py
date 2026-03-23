"""FHIR server client — paginated resource fetch + write via FHIR REST API.

Uses urllib3 PoolManager for HTTP connection reuse (persistent connections),
avoiding the cost of a new TCP/TLS handshake on every request.
Auth: optional Bearer token via env FHIR_SOURCE_TOKEN or explicit token param.
"""

import json
import logging
import os
import re
import time
import urllib3
from urllib.parse import urlencode, urlparse

from utils.logging import REQUEST_ID
from utils.metrics import FHIR_CALL_COUNT, FHIR_LATENCY

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
_RESOURCE_TYPE_RE = re.compile(r'^[A-Z][a-zA-Z]+$')
_RESOURCE_ID_RE = re.compile(r'^[A-Za-z0-9._\-]+$')


def _validate_resource_type(resource_type: str) -> str:
    """Validate that resource_type is a valid FHIR resource type name."""
    if not _RESOURCE_TYPE_RE.match(resource_type):
        raise ValueError(
            f"Invalid FHIR resource type: {resource_type!r}. "
            f"Must match [A-Z][a-zA-Z]+"
        )
    return resource_type


def _validate_resource_id(resource_id: str) -> str:
    """Validate that resource_id contains only safe characters."""
    if not _RESOURCE_ID_RE.match(resource_id):
        raise ValueError(
            f"Invalid FHIR resource ID format. "
            f"Must match [A-Za-z0-9._-]+"
        )
    return resource_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_headers(token=None):
    headers = {
        "Accept": "application/fhir+json",
        "X-Request-ID": REQUEST_ID.get("-"),
    }
    tok = token or os.environ.get("FHIR_SOURCE_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def _make_post_headers(token=None):
    """Headers for write requests — Accept + Content-Type + optional Auth."""
    h = _make_headers(token)
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
                raise ValueError(f"FHIR server HTTP {resp.status} for {url}")
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


def _safe_next_url(next_url: str, base_url: str) -> str:
    """Validate that a pagination link stays on the same origin as base_url.

    Prevents SSRF where a malicious FHIR server returns a link[rel=next]
    pointing to an internal service (e.g. http://internal-admin:8080/).
    """
    n = urlparse(next_url)
    b = urlparse(base_url)
    if n.scheme != b.scheme or n.netloc != b.netloc:
        raise ValueError(
            f"Pagination link leaves origin ({b.scheme}://{b.netloc}): {next_url!r}"
        )
    return next_url


def _get_json(url, token=None, timeout=30, operation="get"):
    headers = _make_headers(token)
    return _do_request("GET", url, headers, timeout, operation=operation)


def _write_json(url, payload, method, token=None, timeout=30, operation="write"):
    """POST or PUT JSON payload to a FHIR server URL; returns parsed response dict."""
    body = json.dumps(payload).encode("utf-8")
    headers = _make_post_headers(token)
    return _do_request(method, url, headers, timeout, body=body, operation=operation)


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def get_capability_statement(base_url, token=None, timeout=30):
    """Fetch /metadata and return the list of supported resource types."""
    url = base_url.rstrip("/") + "/metadata"
    log.info("fetching capability statement from %s", url)
    cs = _get_json(url, token=token, timeout=timeout, operation="metadata")
    resource_types = []
    for rest in cs.get("rest", []):
        for r in rest.get("resource", []):
            rt = r.get("type")
            if rt:
                resource_types.append(rt)
    return resource_types


def fetch_resource_type(base_url, resource_type, params=None, token=None, timeout=30):
    """Generator that yields individual FHIR resource dicts for a given type.

    Follows Bundle pagination (link[rel=next]) until exhausted.

    Args:
        base_url: FHIR base URL, e.g. "http://host:8080/fhir"
        resource_type: e.g. "Patient"
        params: dict of extra query params, e.g. {"_since": "2024-01-01", "_tag": "cohort"}
        token: optional Bearer token (overrides FHIR_SOURCE_TOKEN env)
        timeout: HTTP timeout in seconds
    """
    query = {"_count": 200}
    if params:
        query.update(params)

    _validate_resource_type(resource_type)
    url = base_url.rstrip("/") + "/" + resource_type + "?" + urlencode(query)
    page = 0

    while url:
        page += 1
        if page > _FHIR_MAX_PAGES:
            log.warning(
                "fetch_resource_type %s: reached page limit (%d), stopping pagination",
                resource_type, _FHIR_MAX_PAGES,
            )
            break
        log.info("fetching %s page %d: %s", resource_type, page, url)
        bundle = _get_json(url, token=token, timeout=timeout, operation="search")

        if bundle.get("resourceType") != "Bundle":
            raise ValueError(
                f"Expected Bundle from {url}, got {bundle.get('resourceType')}"
            )

        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if resource:
                yield resource

        # Follow next page link — validate same-origin to prevent SSRF
        url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                raw = link.get("url")
                if raw:
                    url = _safe_next_url(raw, base_url)
                break


def fetch_everything(base_url, resource_type, resource_id, params=None, token=None, timeout=30):
    """Generator that yields all resources from a FHIR $everything operation.

    Calls ``GET {base_url}/{resource_type}/{resource_id}/$everything`` and follows
    Bundle pagination (link[rel=next]) until exhausted.

    Args:
        base_url: FHIR base URL, e.g. "http://host:8080/fhir"
        resource_type: e.g. "Patient"
        resource_id: the resource logical ID, e.g. "DDME"
        params: dict of extra query params passed to $everything, e.g. {"_count": 50}
        token: optional Bearer token (overrides FHIR_SOURCE_TOKEN env)
        timeout: HTTP timeout in seconds

    Yields individual FHIR resource dicts (unwrapped from Bundle entries).
    """
    base = base_url.rstrip("/")
    _validate_resource_type(resource_type)
    _validate_resource_id(resource_id)
    url = f"{base}/{resource_type}/{resource_id}/$everything"
    if params:
        url += "?" + urlencode(params)

    page = 0
    while url:
        page += 1
        if page > _FHIR_MAX_PAGES:
            log.warning(
                "$everything %s/%s: reached page limit (%d), stopping pagination",
                resource_type, resource_id, _FHIR_MAX_PAGES,
            )
            break
        log.info("$everything %s/%s page %d: %s", resource_type, resource_id, page, url)
        bundle = _get_json(url, token=token, timeout=timeout, operation="everything")

        if bundle.get("resourceType") != "Bundle":
            raise ValueError(
                f"Expected Bundle from {url}, got {bundle.get('resourceType')}"
            )

        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if resource:
                yield resource

        # Follow next page link — validate same-origin to prevent SSRF
        url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                raw = link.get("url")
                if raw:
                    url = _safe_next_url(raw, base_url)
                break


def fetch_all_resource_types(base_url, resource_types, params=None, token=None, timeout=30):
    """Generator that yields (resource_type, resource_dict) for all given types."""
    for rt in resource_types:
        for resource in fetch_resource_type(base_url, rt, params=params, token=token, timeout=timeout):
            yield rt, resource


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def post_resource(base_url, resource, token=None, timeout=30):
    """Create or update a single FHIR resource on a FHIR server.

    - If the resource has an ``id`` field, issues a PUT to preserve that ID.
    - Otherwise issues a POST and lets the server assign an ID.

    Returns the server's response resource dict.
    Raises ``ValueError`` on network or HTTP errors.
    """
    rt = resource.get("resourceType")
    if not rt:
        raise ValueError("resource is missing resourceType")

    rid = resource.get("id")
    if rid:
        _validate_resource_type(rt)
        _validate_resource_id(rid)
        url = f"{base_url.rstrip('/')}/{rt}/{rid}"
        method = "PUT"
        log.info("PUT %s (resource)", rt)
    else:
        _validate_resource_type(rt)
        url = f"{base_url.rstrip('/')}/{rt}"
        method = "POST"
        log.info("POST %s (no id)", rt)

    return _write_json(url, resource, method, token=token, timeout=timeout, operation="write")


def post_bundle(base_url, bundle, token=None, timeout=30):
    """Submit a FHIR Bundle (transaction or batch) to the server's base URL.

    Returns the response Bundle dict.
    Raises ``ValueError`` on network or HTTP errors.
    """
    if bundle.get("resourceType") != "Bundle":
        raise ValueError(
            f"post_bundle expects a FHIR Bundle, got resourceType={bundle.get('resourceType')!r}"
        )
    url = base_url.rstrip("/") + "/"
    log.info("POST Bundle type=%s to %s", bundle.get("type"), url)
    return _write_json(url, bundle, "POST", token=token, timeout=timeout, operation="bundle")


def upload_resources(base_url, resources, token=None, timeout=30):
    """Upload an iterable of FHIR resource dicts to a FHIR server.

    Yields one result dict per resource:
        {
            "resourceType": str,
            "source_id":    str | None,   # id from the input resource
            "server_id":    str | None,   # id assigned/confirmed by server
            "success":      bool,
            "error":        str | None,
        }

    Never raises — errors are captured per-resource so the caller can
    continue processing the remaining resources.
    """
    count = 0
    for resource in resources:
        rt = resource.get("resourceType", "Unknown")
        source_id = resource.get("id")
        try:
            resp = post_resource(base_url, resource, token=token, timeout=timeout)
            server_id = resp.get("id")
            yield {"resourceType": rt, "source_id": source_id, "server_id": server_id,
                   "success": True, "error": None}
        except ValueError as exc:
            log.warning("upload_resources error type=%s error_type=%s", rt, type(exc).__name__)
            yield {"resourceType": rt, "source_id": source_id, "server_id": None,
                   "success": False, "error": f"FHIR server error ({type(exc).__name__})"}

        count += 1
        if count % 100 == 0:
            log.info("upload_resources: %d resources uploaded so far", count)
