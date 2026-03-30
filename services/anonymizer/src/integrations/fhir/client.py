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
_FHIR_PAGE_SIZE = int(os.environ.get("FHIR_PAGE_SIZE", "200"))  # default 200; set to 0 to let server decide
_FHIR_BULK_POLL_INTERVAL = float(os.environ.get("FHIR_BULK_POLL_INTERVAL_SEC", "5"))
_FHIR_BULK_POLL_TIMEOUT = float(os.environ.get("FHIR_BULK_POLL_TIMEOUT_SEC", "3600"))
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
    query = {}
    if _FHIR_PAGE_SIZE:
        query["_count"] = _FHIR_PAGE_SIZE
    if params:
        query.update(params)

    _validate_resource_type(resource_type)
    url = base_url.rstrip("/") + "/" + resource_type
    if query:
        url += "?" + urlencode(query)
    page = 0

    while url:
        page += 1
        if page > _FHIR_MAX_PAGES:
            log.warning(
                "fetch_resource_type %s: reached page limit (%d), stopping pagination",
                resource_type, _FHIR_MAX_PAGES,
            )
            break
        current_url = url
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
                    url = _safe_next_url(raw, current_url)
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
    query = {}
    if _FHIR_PAGE_SIZE:
        query["_count"] = _FHIR_PAGE_SIZE
    if params:
        query.update(params)
    url = f"{base}/{resource_type}/{resource_id}/$everything"
    if query:
        url += "?" + urlencode(query)

    page = 0
    while url:
        page += 1
        if page > _FHIR_MAX_PAGES:
            log.warning(
                "$everything %s/%s: reached page limit (%d), stopping pagination",
                resource_type, resource_id, _FHIR_MAX_PAGES,
            )
            break
        current_url = url
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
                    url = _safe_next_url(raw, current_url)
                break


def fetch_all_resource_types(base_url, resource_types, params=None, token=None, timeout=30):
    """Generator that yields (resource_type, resource_dict) for all given types."""
    for rt in resource_types:
        for resource in fetch_resource_type(base_url, rt, params=params, token=token, timeout=timeout):
            yield rt, resource


def fetch_cohort(base_url, search_type, search_params, everything_params=None,
                 token=None, timeout=30):
    """Generator: find patients by searching a resource type, then yield $everything for each.

    Two-phase cohort export:
      1. Search ``{search_type}`` with ``search_params`` (e.g. Condition?code=E11)
      2. Extract unique patient references from ``subject.reference``
      3. Call ``Patient/{id}/$everything`` for each unique patient

    Args:
        base_url:           FHIR base URL
        search_type:        Resource type to search (e.g. ``"Condition"``)
        search_params:      Search query params dict (e.g. ``{"code": "E11"}``)
        everything_params:  Extra params passed to ``$everything`` (optional)
        token:              Optional Bearer token
        timeout:            HTTP timeout per request

    Yields individual FHIR resource dicts from each patient's $everything.
    """
    _validate_resource_type(search_type)

    # Phase 1: Search and collect unique patient IDs
    patient_ids = set()
    log.info("cohort search: %s params=%s", search_type, search_params)
    for resource in fetch_resource_type(
        base_url, search_type, params=search_params, token=token, timeout=timeout,
    ):
        ref = resource.get("subject", {}).get("reference", "")
        if ref.startswith("Patient/"):
            patient_ids.add(ref.split("/", 1)[1])
    log.info("cohort search found %d unique patient(s)", len(patient_ids))

    if not patient_ids:
        return

    # Phase 2: $everything for each patient
    for i, pid in enumerate(sorted(patient_ids), 1):
        log.info("cohort $everything %d/%d: Patient/%s", i, len(patient_ids), pid)
        for resource in fetch_everything(
            base_url, "Patient", pid,
            params=everything_params, token=token, timeout=timeout,
        ):
            yield resource


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
        # HAPI FHIR rejects purely numeric IDs on PUT (HAPI-0960).
        # Prefix them so they contain at least one non-numeric character.
        if rid.isdigit():
            rid = f"p-{rid}"
            resource = {**resource, "id": rid}
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


# ---------------------------------------------------------------------------
# Bulk Data Export ($export)
# ---------------------------------------------------------------------------

def _poll_bulk_status(status_url, token=None, timeout=30):
    """Poll a Bulk Data Export status URL until completion or timeout.

    Returns the completed export manifest dict (JSON with ``output[]`` etc.).
    Raises ``ValueError`` on HTTP errors or if ``_FHIR_BULK_POLL_TIMEOUT`` is exceeded.
    """
    headers = _make_headers(token)
    deadline = time.monotonic() + _FHIR_BULK_POLL_TIMEOUT

    while True:
        if time.monotonic() > deadline:
            raise ValueError(
                f"Bulk export poll timeout ({_FHIR_BULK_POLL_TIMEOUT}s) exceeded for {status_url}"
            )
        resp = _do_raw_request("GET", status_url, headers, timeout, operation="bulk_poll")

        if resp.status == 200:
            return json.loads(resp.data.decode("utf-8"))

        if resp.status == 202:
            progress = resp.headers.get("X-Progress", "")
            if progress:
                log.info("bulk export in progress: %s", progress)
            # Honor Retry-After if present, clamp to [1, 120]
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(1, min(120, int(retry_after)))
                except (ValueError, TypeError):
                    delay = _FHIR_BULK_POLL_INTERVAL
            else:
                delay = _FHIR_BULK_POLL_INTERVAL
            time.sleep(delay)
            continue

        raise ValueError(
            f"Unexpected status {resp.status} while polling bulk export at {status_url}"
        )


def _download_bulk_ndjson(file_url, token=None, timeout=60):
    """Download a single NDJSON file from a bulk export output URL.

    Yields individual resource dicts (one per NDJSON line).
    """
    headers = _make_headers(token)
    headers["Accept"] = "application/fhir+ndjson"
    resp = _do_raw_request("GET", file_url, headers, timeout, operation="bulk_download")
    text = resp.data.decode("utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed NDJSON line from {file_url}: {exc}") from exc


def bulk_export(base_url, level="system", resource_type=None, type_filter=None,
                since=None, token=None, timeout=30):
    """Generator: initiate a FHIR Bulk Data Export and yield resource dicts.

    Implements the full Bulk Data Export protocol:
    kick-off → poll → download NDJSON files → cleanup.

    Args:
        base_url:      FHIR server base URL, e.g. ``http://host:8080/fhir``
        level:         ``"system"`` for ``/$export`` or ``"type"`` for ``/{Type}/$export``
        resource_type: Required when ``level="type"``.  For system-level, used as
                       the ``_type`` param when ``type_filter`` is not set.
        type_filter:   Comma-separated resource types for the ``_type`` param
                       (system-level only; overrides ``resource_type``).
        since:         ``_since`` instant, e.g. ``"2024-01-01T00:00:00Z"``
        token:         Optional Bearer token (overrides ``FHIR_SOURCE_TOKEN`` env).
        timeout:       HTTP timeout per individual request in seconds.

    Yields individual FHIR resource dicts from exported NDJSON files.
    """
    base = base_url.rstrip("/")

    # ── Build kick-off URL ──────────────────────────────────────────
    params = {"_outputFormat": "application/fhir+ndjson"}
    if level == "type":
        if not resource_type:
            raise ValueError("resource_type is required for type-level bulk export")
        _validate_resource_type(resource_type)
        kickoff_url = f"{base}/{resource_type}/$export"
    else:
        kickoff_url = f"{base}/$export"
        _type = type_filter or resource_type
        if _type:
            params["_type"] = _type
    if since:
        params["_since"] = since
    kickoff_url += "?" + urlencode(params)

    # ── Kick-off request ────────────────────────────────────────────
    headers = _make_headers(token)
    headers["Prefer"] = "respond-async"
    log.info("bulk export kick-off: %s", kickoff_url)
    resp = _do_raw_request("GET", kickoff_url, headers, timeout, operation="bulk_kickoff")

    if resp.status != 202:
        raise ValueError(
            f"Bulk export kick-off expected 202, got {resp.status} from {kickoff_url}"
        )
    status_url = resp.headers.get("Content-Location")
    if not status_url:
        raise ValueError("Bulk export kick-off missing Content-Location header")

    log.info("bulk export status URL: %s", status_url)

    # ── Poll until complete ─────────────────────────────────────────
    try:
        manifest = _poll_bulk_status(status_url, token=token, timeout=timeout)

        # Log any errors reported in the manifest
        for err_entry in manifest.get("error", []):
            err_url = err_entry.get("url", "")
            log.warning("bulk export reported error file: %s", err_url)

        # ── Download each output NDJSON file ────────────────────────
        output_files = manifest.get("output", [])
        log.info("bulk export complete: %d output file(s)", len(output_files))
        total = 0
        for file_entry in output_files:
            file_url = file_entry.get("url")
            if not file_url:
                continue
            file_type = file_entry.get("type", "Unknown")
            log.info("downloading bulk export file: type=%s url=%s", file_type, file_url)
            for resource in _download_bulk_ndjson(file_url, token=token, timeout=timeout):
                total += 1
                yield resource
        log.info("bulk export: yielded %d resource(s) total", total)
    finally:
        # ── Cleanup: best-effort DELETE ─────────────────────────────
        delete_bulk_export(status_url, token=token, timeout=timeout)


def delete_bulk_export(status_url, token=None, timeout=30):
    """Send DELETE to a bulk export status URL to clean up server-side data.

    Best-effort: logs warnings on failure but does not raise.
    """
    try:
        headers = _make_headers(token)
        _do_raw_request("DELETE", status_url, headers, timeout, operation="bulk_delete")
        log.info("bulk export cleanup: deleted %s", status_url)
    except ValueError:
        log.warning("bulk export cleanup failed for %s (non-critical)", status_url)
