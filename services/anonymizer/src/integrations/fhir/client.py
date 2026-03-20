"""FHIR server client — paginated resource fetch + write via FHIR REST API.

Uses only stdlib urllib (no new dependencies), consistent with gpas/service.py.
Auth: optional Bearer token via env FHIR_SOURCE_TOKEN or explicit token param.
"""

import json
import logging
import os
import time
from urllib import request, error as urlerror
from urllib.parse import urlencode, urlparse

from utils.logging import REQUEST_ID
from utils.metrics import FHIR_CALL_COUNT, FHIR_LATENCY

log = logging.getLogger("medanon.fhir_server")


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


def _do_request(req, url, timeout, operation="request"):
    """Execute a urllib Request and return the parsed JSON response.

    Raises ``ValueError`` on HTTP or connection errors (consistent with
    the rest of the module so callers only need to catch one exception type).
    """
    t0 = time.perf_counter()
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset("utf-8")
            result = json.loads(resp.read().decode(charset))
        FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
        FHIR_CALL_COUNT.labels(operation=operation, status="ok").inc()
        return result
    except urlerror.HTTPError as exc:
        FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
        FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        raise ValueError(f"FHIR server HTTP {exc.code} for {url}: {detail}") from exc
    except urlerror.URLError as exc:
        FHIR_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
        FHIR_CALL_COUNT.labels(operation=operation, status="error").inc()
        raise ValueError(f"FHIR server connection error for {url}: {exc.reason}") from exc


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
    req = request.Request(url=url, headers=_make_headers(token), method="GET")
    return _do_request(req, url, timeout, operation=operation)


def _write_json(url, payload, method, token=None, timeout=30, operation="write"):
    """POST or PUT JSON payload to a FHIR server URL; returns parsed response dict."""
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url=url, data=body, headers=_make_post_headers(token), method=method)
    return _do_request(req, url, timeout, operation=operation)


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

    url = base_url.rstrip("/") + "/" + resource_type + "?" + urlencode(query)
    page = 0

    while url:
        page += 1
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
    url = f"{base}/{resource_type}/{resource_id}/$everything"
    if params:
        url += "?" + urlencode(params)

    page = 0
    while url:
        page += 1
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
        url = f"{base_url.rstrip('/')}/{rt}/{rid}"
        method = "PUT"
        log.info("PUT %s/%s", rt, rid)
    else:
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
            log.warning("upload_resources error type=%s id=%s: %s", rt, source_id, exc)
            yield {"resourceType": rt, "source_id": source_id, "server_id": None,
                   "success": False, "error": str(exc)}

        count += 1
        if count % 100 == 0:
            log.info("upload_resources: %d resources uploaded so far", count)
