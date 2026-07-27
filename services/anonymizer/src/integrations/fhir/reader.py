"""Read-side FHIR operations  search, $everything, cohort export.

All functions use the shared transport layer from ``_transport`` for HTTP
communication, pagination, and input validation.
"""

import os
import queue as _queue
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
    "server_supports_bulk_export",
    "preflight_resource_count",
    "fetch_resource_type",
    "fetch_resources_by_ids",
    "fetch_everything",
    "fetch_all_resource_types",
    "fetch_cohort",
    "fetch_patients_everything",
]

_FHIR_FETCH_PARALLEL = int(os.environ.get("MEDANON_FHIR_FETCH_PARALLEL", "4"))
_COHORT_PARALLEL = int(os.environ.get("MEDANON_COHORT_PARALLEL", "4"))
# Phase-2 re-fetch ``_id`` batch size. Larger = fewer round-trips per group;
# bounded to keep the query string under server URL-length limits.
_REFETCH_CHUNK = max(1, int(os.environ.get("MEDANON_FHIR_REFETCH_CHUNK", "200")))

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


def server_supports_bulk_export(base_url, token=None, timeout=10):
    """Return True if the server advertises the FHIR Bulk Data ``$export`` op.

    Reads the CapabilityStatement and looks for a system-level operation named
    ``export`` (per the Bulk Data Access IG, HAPI and most R4 servers list it
    under ``rest[].operation[]``).  Detection is fail-safe: any error (server
    down, malformed statement, older server) returns False so the caller falls
    back to the legacy search-pagination path rather than erroring the job.
    """
    url = base_url.rstrip("/") + "/metadata"
    try:
        cs = _get_json(url, token=token, timeout=timeout, operation="metadata")
    except Exception as exc:
        log.info("bulk_export capability probe failed (%s), assuming unsupported", exc)
        return False
    for rest in cs.get("rest", []):
        for op in rest.get("operation", []):
            if str(op.get("name", "")).lower() == "export":
                return True
    return False


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
        # If the pre-check fails, don't block  let the real export run.
        return -1


def preflight_system_count(base_url, resource_types=None, token=None, timeout=10):
    """True total resource count by summing ``_summary=count`` across types.

    Many FHIR servers (incl. HAPI) reject a bare ``GET {base}?_summary=count``
    (system-wide) with HTTP 400, so we sum per-type counts instead. Unlike
    :func:`preflight_resource_count` (which proxies system size by Patient count ×
    a fixed factor), this returns the *actual* total  used to route bulk exports
    to the staged path correctly for dense datasets where ``patient_count × 15``
    badly under-counts (~70× on real data).

    *resource_types*: explicit list to count; when ``None``, discovers types from
    the server CapabilityStatement (minus infrastructure types).

    Returns the summed total, or ``-1`` if discovery/counting fails entirely
    (caller falls back to the Patient-proxy estimate).
    """
    base = base_url.rstrip("/")
    try:
        if resource_types is None:
            from domain.fhir import INFRA_RESOURCE_TYPES as _INFRA

            all_types = get_capability_statement(base, token=token, timeout=timeout)
            resource_types = [t for t in all_types if t not in _INFRA]
        total = 0
        counted = False
        for rt in resource_types:
            c = preflight_resource_count(
                base, resource_type=rt, token=token, timeout=timeout
            )
            if c > 0:
                total += c
                counted = True
        return total if counted else -1
    except Exception as exc:
        log.warning("preflight_system_count failed (%s), using fallback", exc)
        return -1


def fetch_resource_type(
    base_url,
    resource_type,
    params=None,
    token=None,
    timeout=30,
    start_url=None,
    yield_cursors=False,
):
    """Generator that yields individual FHIR resource dicts for a given type.

    Follows Bundle pagination (link[rel=next]) until exhausted.

    Args:
        base_url: FHIR base URL, e.g. "http://host:8080/fhir"
        resource_type: e.g. "Patient"
        params: dict of extra query params, e.g. {"_since": "2024-01-01", "_tag": "cohort"}
        token: optional Bearer token (overrides FHIR_SOURCE_TOKEN env)
        timeout: HTTP timeout in seconds
        start_url: if given, resume pagination from this URL (cursor-based resume)
        yield_cursors: if True, yield ``(resource, current_page_url, page_offset)`` tuples
                       instead of bare resource dicts.  Allows callers to checkpoint the
                       exact pagination position: on resume, call with
                       ``start_url=current_page_url`` and skip the first ``page_offset``
                       yielded resources.
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

    # Track the pinned origin across pagination to prevent progressive SSRF.
    _pinned_origin = [None]

    while url:
        page += 1
        if page > _t._FHIR_MAX_PAGES:
            log.warning(
                "fetch_resource_type %s: reached page limit (%d), stopping pagination",
                resource_type,
                _t._FHIR_MAX_PAGES,
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
                    next_url = _safe_next_url(raw, current_url, _pinned_origin)
                break

        page_offset = 0
        for entry in bundle.get("entry", []):
            resource = entry.get("resource")
            if resource:
                if yield_cursors:
                    # Yield (resource, current_page_url, offset_in_page) so callers
                    # can resume from exactly this position: re-fetch current_page_url
                    # and skip the first `offset_in_page` entries.
                    yield resource, current_url, page_offset
                else:
                    yield resource
                page_offset += 1

        url = next_url


def fetch_resources_by_ids(
    base_url: str,
    resource_type: str,
    ids: list[str],
    token=None,
    timeout: float = 30,
) -> list[dict]:
    """Fetch a set of FHIR resources by their logical IDs in one or more HTTP calls.

    Issues ``GET /{resource_type}?_id=id1,id2,...&_count=N`` and follows
    pagination.  Returns a flat list of FHIR resource dicts.

    Used by the staged executor to re-fetch resources from the source FHIR server
    in Phase 2  this keeps patient data out of our databases by re-fetching on
    demand rather than caching raw FHIR resources in PostgreSQL.

    Args:
        base_url: FHIR base URL, e.g. ``http://hapi-fhir:8080/fhir``
        resource_type: e.g. ``"Patient"``
        ids: logical resource IDs (without resource type prefix)
        token: optional Bearer token
        timeout: HTTP timeout in seconds
    """
    if not ids:
        return []

    _validate_resource_type(resource_type)
    # Chunk to avoid oversized query strings; size via MEDANON_FHIR_REFETCH_CHUNK.
    _CHUNK_SIZE = _REFETCH_CHUNK
    resources: list[dict] = []
    _pinned_origin: list = [None]

    for i in range(0, len(ids), _CHUNK_SIZE):
        chunk = ids[i : i + _CHUNK_SIZE]
        query = {"_id": ",".join(chunk), "_count": str(len(chunk))}
        url: str | None = (
            base_url.rstrip("/") + "/" + resource_type + "?" + urlencode(query)
        )

        while url:
            bundle = _get_json(
                url, token=token, timeout=timeout, operation="fetch_by_ids"
            )
            for entry in bundle.get("entry", []):
                res = entry.get("resource")
                if isinstance(res, dict):
                    resources.append(res)
            # Follow next-page link (rare but possible for large chunks)
            next_url = None
            for link in bundle.get("link", []):
                if link.get("relation") == "next":
                    raw = link.get("url", "")
                    if raw:
                        next_url = _safe_next_url(raw, url, _pinned_origin)
            url = next_url

    return resources


def fetch_everything(
    base_url,
    resource_type,
    resource_id,
    params=None,
    token=None,
    timeout=30,
    start_url=None,
):
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
    _pinned_origin = [None]
    while url:
        page += 1
        if page > _t._FHIR_MAX_PAGES:
            log.warning(
                "$everything %s/%s: reached page limit (%d), stopping pagination",
                resource_type,
                resource_id,
                _t._FHIR_MAX_PAGES,
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

        # Follow next page link  validate same-origin to prevent SSRF
        url = None
        for link in bundle.get("link", []):
            if link.get("relation") == "next":
                raw = link.get("url")
                if raw:
                    url = _safe_next_url(raw, current_url, _pinned_origin)
                break


def fetch_all_resource_types(
    base_url,
    resource_types,
    params=None,
    token=None,
    timeout=30,
    completed_rts=None,
    current_rt=None,
    current_rt_start_url=None,
    yield_cursors=False,
):
    """Generator that yields (resource_type, resource_dict) for all given types.

    When ``MEDANON_FHIR_FETCH_PARALLEL`` > 1, resource types are fetched
    concurrently  each type runs in its own thread  and results are merged
    via a thread-safe queue.  Order across types is non-deterministic but all
    resources for each type are yielded in pagination order.

    B17 cursor-based resume (serial path only):
      - ``completed_rts``: set of resource types already fully written; skipped.
      - ``current_rt``: the resource type to resume mid-type.
      - ``current_rt_start_url``: FHIR page URL to resume from for *current_rt*.
      - ``yield_cursors``: if True, yields ``(rt, resource, page_url, page_offset)``
        4-tuples instead of ``(rt, resource)`` pairs.
    """
    _done = set(completed_rts or [])
    if _FHIR_FETCH_PARALLEL <= 1 or len(resource_types) <= 1 or yield_cursors:
        for rt in resource_types:
            if rt in _done:
                continue
            start_url = current_rt_start_url if rt == current_rt else None
            for resource, page_url, page_offset in fetch_resource_type(
                base_url,
                rt,
                params=params,
                token=token,
                timeout=timeout,
                start_url=start_url,
                yield_cursors=True,
            ):
                if yield_cursors:
                    yield rt, resource, page_url, page_offset
                else:
                    yield rt, resource
        return

    import logging as _logging

    _log = _logging.getLogger("medanon.fhir.reader")

    _SENTINEL = object()
    # Use an unbounded queue: bounded queues deadlock when the consumer raises
    # and stops draining while producer threads are blocked on put().
    result_q: _queue.Queue = _queue.Queue()
    failed_rts: list[str] = []

    # Honour checkpoint state: skip completed types, resume current_rt from its
    # last page URL.  The serial path already handles yield_cursors (gated above).
    pending_rts = [rt for rt in resource_types if rt not in _done]
    total = len(pending_rts)

    def _fetch_one(rt):
        start_url = current_rt_start_url if rt == current_rt else None
        try:
            for resource in fetch_resource_type(
                base_url,
                rt,
                params=params,
                token=token,
                timeout=timeout,
                start_url=start_url,
            ):
                result_q.put((rt, resource))
        except Exception as exc:
            # Log the failure but do NOT abort  other resource types continue.
            # The failed type is recorded in failed_rts and logged at the end
            # so operators can see exactly which types are missing.
            _log.warning(
                "fhir_fetch_partial_failure rt=%s  resources from this type "
                "will be missing from the export: %s",
                rt,
                exc,
            )
            result_q.put((_SENTINEL, rt, exc))
        finally:
            result_q.put(_SENTINEL)

    # Use a DEDICATED thread pool capped at _FHIR_FETCH_PARALLEL threads.
    # Critical: do NOT use get_executor() here  that is the global pipeline pool
    # shared with rule-evaluation threads.  Submitting 268 FHIR-fetch tasks to the
    # global pool starves rule evaluation and makes de-identification slower, not
    # faster.  A private pool with _FHIR_FETCH_PARALLEL threads saturates the FHIR
    # HTTP connection pool (FHIR_POOL_SIZE) without starving any other subsystem.
    from concurrent.futures import ThreadPoolExecutor as _TPE

    _fetch_pool = _TPE(
        max_workers=_FHIR_FETCH_PARALLEL,
        thread_name_prefix="medanon-fhir-parallel",
    )
    for rt in pending_rts:
        _fetch_pool.submit(_fetch_one, rt)
    # Do not call _fetch_pool.shutdown() here  the pool must remain alive until
    # all _fetch_one workers finish, which we detect via the sentinel counter below.
    finished = 0
    try:
        while finished < total:
            try:
                item = result_q.get(timeout=_QUEUE_GET_TIMEOUT_SEC)
            except _queue.Empty:
                raise ValueError(
                    f"FHIR fetch stalled: no data received for "
                    f"{_QUEUE_GET_TIMEOUT_SEC}s  possible thread crash"
                )
            if item is _SENTINEL:
                finished += 1
                continue
            # Error sentinel: (SENTINEL, rt, exc) tuple  log and skip this type.
            if isinstance(item, tuple) and len(item) == 3 and item[0] is _SENTINEL:
                _, failed_rt, exc = item
                failed_rts.append(failed_rt)
                _log.error(
                    "fhir_fetch_type_failed rt=%s error=%s  resources from "
                    "this type will be missing from the output",
                    failed_rt,
                    exc,
                )
                continue
            yield item
    finally:
        _fetch_pool.shutdown(wait=False, cancel_futures=True)

    if failed_rts:
        _log.error(
            "fhir_fetch_incomplete failed_types=%s  export is missing resources "
            "from %d type(s). Re-run with MEDANON_FHIR_FETCH_PARALLEL=1 to use "
            "serial fetch which is crash-safe.",
            failed_rts,
            len(failed_rts),
        )


def fetch_cohort(
    base_url, search_type, search_params, everything_params=None, token=None, timeout=30
):
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
        base_url,
        search_type,
        params=search_params,
        token=token,
        timeout=timeout,
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
                base_url,
                "Patient",
                pid,
                params=everything_params,
                token=token,
                timeout=timeout,
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
                    base_url,
                    "Patient",
                    pid,
                    params=everything_params,
                    token=token,
                    timeout=timeout,
                ):
                    result_q.put(resource)
            except Exception as exc:
                error_q.put(exc)
            finally:
                result_q.put(_SENTINEL)

        from concurrent.futures import ThreadPoolExecutor as _TPE

        _cohort_pool = _TPE(
            max_workers=_COHORT_PARALLEL, thread_name_prefix="medanon-cohort"
        )
        for pid in sorted_pids:
            _cohort_pool.submit(_fetch_patient, pid)
        finished = 0
        try:
            while finished < n_patients:
                try:
                    item = result_q.get(timeout=_QUEUE_GET_TIMEOUT_SEC)
                except _queue.Empty:
                    raise ValueError(
                        f"Cohort fetch stalled: no data received for "
                        f"{_QUEUE_GET_TIMEOUT_SEC}s  possible thread crash"
                    )
                if item is _SENTINEL:
                    finished += 1
                    continue
                if _dedup(item):
                    yield item
        finally:
            _cohort_pool.shutdown(wait=False, cancel_futures=True)

        if not error_q.empty():
            raise error_q.get_nowait()


def fetch_patients_everything(
    base_url, patient_ids, params=None, token=None, timeout=30
):
    """Generator: yield $everything for each patient in *patient_ids*, with deduplication.

    Like the second phase of :func:`fetch_cohort` but skips the search phase
    since patient IDs are already provided.  Shared resources (e.g. Practitioner)
    referenced by multiple patients are yielded only once.

    Args:
        base_url:     FHIR base URL
        patient_ids:  list of Patient logical IDs
        params:       extra query params for $everything (optional)
        token:        optional Bearer token
        timeout:      HTTP timeout per request

    Yields individual FHIR resource dicts from each patient's $everything.
    """
    pid_list = list(patient_ids)
    if not pid_list:
        return

    seen: set[tuple[str, str]] = set()

    def _dedup(resource):
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

    if _COHORT_PARALLEL <= 1 or len(pid_list) <= 1:
        for i, pid in enumerate(pid_list, 1):
            log.info(
                "batch_patient $everything %d/%d: Patient/%s", i, len(pid_list), pid
            )
            for resource in fetch_everything(
                base_url,
                "Patient",
                pid,
                params=params,
                token=token,
                timeout=timeout,
            ):
                if _dedup(resource):
                    yield resource
    else:
        _SENTINEL = object()
        result_q: _queue.Queue = _queue.Queue(maxsize=_COHORT_PARALLEL * 200)
        error_q: _queue.Queue = _queue.Queue()

        def _fetch_patient(pid):
            try:
                for resource in fetch_everything(
                    base_url,
                    "Patient",
                    pid,
                    params=params,
                    token=token,
                    timeout=timeout,
                ):
                    result_q.put(resource)
            except Exception as exc:
                error_q.put(exc)
            finally:
                result_q.put(_SENTINEL)

        from concurrent.futures import ThreadPoolExecutor as _TPE

        _batch_pool = _TPE(
            max_workers=_COHORT_PARALLEL, thread_name_prefix="medanon-batch-patient"
        )
        for pid in pid_list:
            _batch_pool.submit(_fetch_patient, pid)
        finished = 0
        try:
            while finished < len(pid_list):
                try:
                    item = result_q.get(timeout=_QUEUE_GET_TIMEOUT_SEC)
                except _queue.Empty:
                    raise ValueError(
                        f"Batch patient fetch stalled: no data received for "
                        f"{_QUEUE_GET_TIMEOUT_SEC}s  possible thread crash"
                    )
                if item is _SENTINEL:
                    finished += 1
                    continue
                if _dedup(item):
                    yield item
        finally:
            _batch_pool.shutdown(wait=False, cancel_futures=True)

        if not error_q.empty():
            raise error_q.get_nowait()
