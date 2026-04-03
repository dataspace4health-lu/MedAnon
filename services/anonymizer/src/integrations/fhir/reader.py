"""Read-side FHIR operations — search, $everything, cohort export.

All functions use the shared transport layer from ``_transport`` for HTTP
communication, pagination, and input validation.
"""

import os
import queue as _queue
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

from integrations.fhir import _transport as _t
from ._transport import (
    _get_json,
    _safe_next_url,
    _validate_resource_id,
    _validate_resource_type,
    log,
)

__all__ = [
    "get_capability_statement",
    "preflight_resource_count",
    "fetch_resource_type",
    "fetch_everything",
    "fetch_all_resource_types",
    "fetch_cohort",
]

_FHIR_FETCH_PARALLEL = int(os.environ.get("MEDANON_FHIR_FETCH_PARALLEL", "4"))
_COHORT_PARALLEL = int(os.environ.get("MEDANON_COHORT_PARALLEL", "4"))

# Timeout for queue.get() in parallel consumer loops.  Prevents permanent
# thread hangs when a producer thread crashes without pushing its sentinel.
_QUEUE_GET_TIMEOUT_SEC = int(os.environ.get("MEDANON_QUEUE_GET_TIMEOUT_SEC", "300"))


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


def preflight_resource_count(base_url, resource_type=None, token=None, timeout=10):
    """Quick count of resources on the server using ``_summary=count``.

    Returns the ``total`` from the Bundle response, or 0 when the server
    does not report one.  Used as a fast pre-check before expensive export
    operations so we can bail early on empty servers.
    """
    base = base_url.rstrip("/")
    if resource_type:
        _validate_resource_type(resource_type)
        url = f"{base}/{resource_type}?_summary=count&_count=0"
    else:
        # System-level: check Patient count as a proxy for "any data"
        url = f"{base}/Patient?_summary=count&_count=0"
    try:
        bundle = _get_json(url, token=token, timeout=timeout, operation="preflight")
        return bundle.get("total", 0)
    except Exception as exc:
        log.warning("preflight_resource_count failed (%s), skipping pre-check", exc)
        # If the pre-check fails, don't block — let the real export run.
        return -1


def fetch_resource_type(base_url, resource_type, params=None, token=None, timeout=30,
                        start_url=None, yield_cursors=False):
    """Generator that yields individual FHIR resource dicts for a given type.

    Follows Bundle pagination (link[rel=next]) until exhausted.

    Args:
        base_url: FHIR base URL, e.g. "http://host:8080/fhir"
        resource_type: e.g. "Patient"
        params: dict of extra query params, e.g. {"_since": "2024-01-01", "_tag": "cohort"}
        token: optional Bearer token (overrides FHIR_SOURCE_TOKEN env)
        timeout: HTTP timeout in seconds
        start_url: if given, resume pagination from this URL (cursor-based resume)
        yield_cursors: if True, yield ``(resource, next_page_url)`` tuples instead
                       of bare resource dicts.  Allows callers to checkpoint the
                       current pagination cursor after each resource.
    """
    if start_url:
        url = start_url
    else:
        query = {}
        if _t._FHIR_PAGE_SIZE:
            query["_count"] = _t._FHIR_PAGE_SIZE
        if params:
            query.update(params)

        _validate_resource_type(resource_type)
        url = base_url.rstrip("/") + "/" + resource_type
        if query:
            url += "?" + urlencode(query)

    page = 0

    while url:
        page += 1
        if page > _t._FHIR_MAX_PAGES:
            log.warning(
                "fetch_resource_type %s: reached page limit (%d), stopping pagination",
                resource_type, _t._FHIR_MAX_PAGES,
            )
            break
        current_url = url
        log.info("fetching %s page %d: %s", resource_type, page, url)
        bundle = _get_json(url, token=token, timeout=timeout, operation="search")

        if bundle.get("resourceType") != "Bundle":
            raise ValueError(
                f"Expected Bundle from {url}, got {bundle.get('resourceType')}"
            )

        # Resolve next-page URL before yielding so cursors are available immediately
        next_url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                raw = link.get("url")
                if raw:
                    next_url = _safe_next_url(raw, current_url)
                break

        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if resource:
                if yield_cursors:
                    yield resource, next_url
                else:
                    yield resource

        url = next_url


def fetch_everything(base_url, resource_type, resource_id, params=None, token=None, timeout=30,
                     start_url=None):
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
        start_url: if given, resume pagination from this URL (cursor-based resume)

    Yields individual FHIR resource dicts (unwrapped from Bundle entries).
    """
    if start_url:
        url = start_url
    else:
        base = base_url.rstrip("/")
        _validate_resource_type(resource_type)
        _validate_resource_id(resource_id)
        query = {}
        if _t._FHIR_PAGE_SIZE:
            query["_count"] = _t._FHIR_PAGE_SIZE
        if params:
            query.update(params)
        url = f"{base}/{resource_type}/{resource_id}/$everything"
        if query:
            url += "?" + urlencode(query)

    page = 0
    while url:
        page += 1
        if page > _t._FHIR_MAX_PAGES:
            log.warning(
                "$everything %s/%s: reached page limit (%d), stopping pagination",
                resource_type, resource_id, _t._FHIR_MAX_PAGES,
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
    """Generator that yields (resource_type, resource_dict) for all given types.

    When ``MEDANON_FHIR_FETCH_PARALLEL`` > 1, resource types are fetched
    concurrently — each type runs in its own thread — and results are merged
    via a thread-safe queue.  Order across types is non-deterministic but all
    resources for each type are yielded in pagination order.
    """
    if _FHIR_FETCH_PARALLEL <= 1 or len(resource_types) <= 1:
        for rt in resource_types:
            for resource in fetch_resource_type(base_url, rt, params=params, token=token, timeout=timeout):
                yield rt, resource
        return

    _SENTINEL = object()
    result_q: _queue.Queue = _queue.Queue(maxsize=_FHIR_FETCH_PARALLEL * 200)
    error_q: _queue.Queue = _queue.Queue()
    total = len(resource_types)

    def _fetch_one(rt):
        try:
            for resource in fetch_resource_type(base_url, rt, params=params, token=token, timeout=timeout):
                result_q.put((rt, resource))
        except Exception as exc:
            error_q.put(exc)
        finally:
            result_q.put(_SENTINEL)

    with ThreadPoolExecutor(max_workers=_FHIR_FETCH_PARALLEL) as pool:
        for rt in resource_types:
            pool.submit(_fetch_one, rt)
        finished = 0
        while finished < total:
            try:
                item = result_q.get(timeout=_QUEUE_GET_TIMEOUT_SEC)
            except _queue.Empty:
                raise ValueError(
                    f"FHIR fetch stalled: no data received for "
                    f"{_QUEUE_GET_TIMEOUT_SEC}s — possible thread crash"
                )
            if item is _SENTINEL:
                finished += 1
                continue
            yield item

    if not error_q.empty():
        raise error_q.get_nowait()


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
    # De-duplicate resources shared across patients (e.g. Practitioner referenced
    # by multiple patients appears once instead of N times).
    seen: set[tuple[str, str]] = set()

    def _dedup(resource):
        """Return True if this resource should be yielded (not a duplicate)."""
        if not isinstance(resource, dict):
            return True
        rt = resource.get("resourceType", "")
        rid = resource.get("id", "")
        if rt and rid:
            key = (rt, rid)
            if key in seen:
                return False
            seen.add(key)
        return True

    if _COHORT_PARALLEL <= 1 or len(patient_ids) <= 1:
        for i, pid in enumerate(sorted(patient_ids), 1):
            log.info("cohort $everything %d/%d: Patient/%s", i, len(patient_ids), pid)
            for resource in fetch_everything(
                base_url, "Patient", pid,
                params=everything_params, token=token, timeout=timeout,
            ):
                if _dedup(resource):
                    yield resource
    else:
        _SENTINEL = object()
        result_q: _queue.Queue = _queue.Queue(maxsize=_COHORT_PARALLEL * 200)
        sorted_pids = sorted(patient_ids)
        n_patients = len(sorted_pids)
        error_q: _queue.Queue = _queue.Queue()

        def _fetch_patient(pid):
            try:
                for resource in fetch_everything(
                    base_url, "Patient", pid,
                    params=everything_params, token=token, timeout=timeout,
                ):
                    result_q.put(resource)
            except Exception as exc:
                error_q.put(exc)
            finally:
                result_q.put(_SENTINEL)

        with ThreadPoolExecutor(max_workers=_COHORT_PARALLEL) as pool:
            for pid in sorted_pids:
                pool.submit(_fetch_patient, pid)
            finished = 0
            while finished < n_patients:
                try:
                    item = result_q.get(timeout=_QUEUE_GET_TIMEOUT_SEC)
                except _queue.Empty:
                    raise ValueError(
                        f"Cohort fetch stalled: no data received for "
                        f"{_QUEUE_GET_TIMEOUT_SEC}s — possible thread crash"
                    )
                if item is _SENTINEL:
                    finished += 1
                    continue
                if _dedup(item):
                    yield item

        if not error_q.empty():
            raise error_q.get_nowait()
