"""FHIR Bulk Data Export operations — kick-off, poll, download, cleanup.

Implements the FHIR Bulk Data Access IG protocol: ``$export`` kick-off,
status polling with Retry-After, NDJSON file download, and best-effort
server-side cleanup.
"""

import os
import urllib.parse as _urlparse
from utils.json_fast import loads as _json_loads
import queue as _queue
import time
from urllib.parse import urlencode

from integrations.fhir import _transport as _t
from ._transport import (
    _do_raw_request,
    _make_headers,
    _pool,
    _validate_resource_type,
    log,
)


def _validate_bulk_file_url(url: str) -> None:
    """Raise ValueError if *url* targets a private or loopback address.

    Prevents SSRF via malicious FHIR bulk export manifests that redirect
    file downloads to internal infrastructure (e.g. AWS metadata service).
    """
    from api.deps import check_hostname_ssrf

    parsed = _urlparse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Bulk file URL must use http(s) scheme, got: {parsed.scheme}")
    hostname = parsed.hostname or ""
    if not hostname:
        raise ValueError("Bulk file URL has no hostname")
    err = check_hostname_ssrf(hostname)
    if err:
        raise ValueError(f"Bulk file URL rejected (SSRF): {err}")


_BULK_DOWNLOAD_PARALLEL = int(os.environ.get("MEDANON_BULK_DOWNLOAD_PARALLEL", "4"))

# Timeout for queue.get() in parallel download consumer loops.
# Prevents permanent thread hangs when a producer thread crashes without
# pushing its sentinel.  5 minutes is generous — a single NDJSON file
# download that takes >5 min of zero output is effectively dead.
_QUEUE_GET_TIMEOUT_SEC = int(os.environ.get("MEDANON_QUEUE_GET_TIMEOUT_SEC", "300"))

__all__ = [
    "_poll_bulk_status",
    "_poll_bulk_status_single",
    "_download_bulk_ndjson",
    "bulk_export",
    "bulk_export_kick_off",
    "delete_bulk_export",
]


# ---------------------------------------------------------------------------
# Bulk Data Export ($export)
# ---------------------------------------------------------------------------


def _poll_bulk_status_single(status_url, token=None, timeout=30):
    """Execute a single poll request against a Bulk Data Export status URL.

    Returns a tuple ``(done, manifest_or_delay)``:
      - ``(True, manifest_dict)`` — export complete, manifest with ``output[]``
      - ``(False, delay_seconds)`` — still in progress, caller should wait
    Raises ``ValueError`` on unexpected HTTP status or connection errors.
    """
    headers = _make_headers(token)
    resp = _do_raw_request("GET", status_url, headers, timeout, operation="bulk_poll")

    if resp.status == 200:
        return True, _json_loads(resp.data.decode("utf-8"))

    if resp.status == 202:
        progress = resp.headers.get("X-Progress", "")
        if progress:
            log.info("bulk export in progress: %s", progress)
        # Honor Retry-After if present, clamp to [1, 10]
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(1, min(10, int(retry_after)))
            except (ValueError, TypeError):
                delay = _t._FHIR_BULK_POLL_INTERVAL
        else:
            delay = _t._FHIR_BULK_POLL_INTERVAL
        return False, delay

    raise ValueError(
        f"Unexpected status {resp.status} while polling bulk export at {status_url}"
    )


def _poll_bulk_status(status_url, token=None, timeout=30):
    """Poll a Bulk Data Export status URL until completion or timeout.

    Returns the completed export manifest dict (JSON with ``output[]`` etc.).
    Raises ``ValueError`` on HTTP errors or if ``_FHIR_BULK_POLL_TIMEOUT`` is exceeded.

    Note: This synchronous version uses ``time.sleep()`` and is suitable for
    thread-pool execution.  For async callers that need to avoid blocking a
    thread during long polls, use ``_poll_bulk_status_single()`` in a loop
    with ``asyncio.sleep()`` instead.
    """
    deadline = time.monotonic() + _t._FHIR_BULK_POLL_TIMEOUT

    while True:
        if time.monotonic() > deadline:
            raise ValueError(
                f"Bulk export poll timeout ({_t._FHIR_BULK_POLL_TIMEOUT}s) exceeded for {status_url}"
            )
        done, result = _poll_bulk_status_single(
            status_url, token=token, timeout=timeout
        )
        if done:
            return result
        time.sleep(result)


def _download_bulk_ndjson(file_url, token=None, timeout=60):
    """Download a single NDJSON file from a bulk export output URL.

    Yields individual resource dicts (one per NDJSON line).
    Streams the response in 64 KB chunks to avoid buffering the entire file.
    """
    from ._transport import _fhir_cb, FhirCircuitBreakerOpen

    if not _fhir_cb.allow_request():
        raise FhirCircuitBreakerOpen(
            "FHIR server unavailable — circuit breaker OPEN (bulk_download)"
        )
    headers = _make_headers(token)
    headers["Accept"] = "application/fhir+ndjson"
    try:
        resp = _pool.urlopen(
            "GET",
            file_url,
            headers=headers,
            preload_content=False,
            timeout=timeout,
        )
    except (Exception, OSError) as exc:
        _fhir_cb.record_failure()
        raise ValueError(f"FHIR server connection error for {file_url}: {exc}") from exc
    if resp.status >= 400:
        resp.release_conn()
        if resp.status in (500, 502, 503, 504):
            _fhir_cb.record_failure()
        raise ValueError(f"FHIR server HTTP {resp.status} for {file_url}")
    _fhir_cb.record_success()
    try:
        buf = b""
        for chunk in resp.stream(65536):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    try:
                        yield _json_loads(line)
                    except (ValueError, TypeError) as exc:
                        raise ValueError(
                            f"Malformed NDJSON line from {file_url}: {exc}"
                        ) from exc
        # process any remaining bytes (last line without trailing newline)
        line = buf.strip()
        if line:
            try:
                yield _json_loads(line)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Malformed NDJSON line from {file_url}: {exc}"
                ) from exc
    finally:
        resp.release_conn()


def bulk_export_kick_off(
    base_url,
    level="system",
    resource_type=None,
    group_id=None,
    type_filter=None,
    since=None,
    token=None,
    timeout=30,
):
    """Initiate a FHIR Bulk Data Export and return the status URL.

    This is the first phase of the bulk export protocol.  Returns the
    ``Content-Location`` URL that should be polled for completion.

    Supported levels:
    - ``"system"``  → ``GET /$export``
    - ``"type"``    → ``GET /{ResourceType}/$export``  (requires *resource_type*)
    - ``"group"``   → ``GET /Group/{id}/$export``      (requires *group_id*)

    Raises ``ValueError`` on kick-off failure or missing headers.
    """
    base = base_url.rstrip("/")

    # -- Build kick-off URL --
    params = {"_outputFormat": "application/fhir+ndjson"}
    if level == "type":
        if not resource_type:
            raise ValueError("resource_type is required for type-level bulk export")
        _validate_resource_type(resource_type)
        kickoff_url = f"{base}/{resource_type}/$export"
    elif level == "group":
        if not group_id:
            raise ValueError("group_id is required for group-level bulk export")
        kickoff_url = f"{base}/Group/{group_id}/$export"
        if type_filter:
            params["_type"] = type_filter
    else:
        kickoff_url = f"{base}/$export"
        _type = type_filter or resource_type
        if _type:
            params["_type"] = _type
    if since:
        params["_since"] = since
    kickoff_url += "?" + urlencode(params)

    # -- Kick-off request --
    headers = _make_headers(token)
    headers["Prefer"] = "respond-async"
    log.info("bulk export kick-off: %s", kickoff_url)
    resp = _do_raw_request(
        "GET", kickoff_url, headers, timeout, operation="bulk_kickoff"
    )

    if resp.status != 202:
        raise ValueError(
            f"Bulk export kick-off expected 202, got {resp.status} from {kickoff_url}"
        )
    status_url = resp.headers.get("Content-Location")
    if not status_url:
        raise ValueError("Bulk export kick-off missing Content-Location header")

    log.info("bulk export status URL: %s", status_url)
    return status_url


def _download_manifest_files(manifest, token=None, timeout=60):
    """Generator: download NDJSON files from a completed bulk export manifest.

    Yields individual FHIR resource dicts from each output file.
    """
    # Log any errors reported in the manifest
    for err_entry in manifest.get("error", []):
        err_url = err_entry.get("url", "")
        log.warning("bulk export reported error file: %s", err_url)

    output_files = manifest.get("output", [])
    log.info("bulk export complete: %d output file(s)", len(output_files))
    total = 0

    # SSRF guard: validate every file URL before starting any downloads.
    # A malicious FHIR server could return manifest URLs pointing to internal
    # infrastructure (e.g. cloud metadata services, Redis, admin panels).
    for file_entry in output_files:
        file_url = file_entry.get("url")
        if file_url:
            _validate_bulk_file_url(file_url)

    if _BULK_DOWNLOAD_PARALLEL <= 1 or len(output_files) <= 1:
        # Sequential download
        for file_entry in output_files:
            file_url = file_entry.get("url")
            if not file_url:
                continue
            file_type = file_entry.get("type", "Unknown")
            log.info(
                "downloading bulk export file: type=%s url=%s", file_type, file_url
            )
            for resource in _download_bulk_ndjson(
                file_url, token=token, timeout=timeout
            ):
                total += 1
                yield resource
    else:
        # Parallel download: each file downloaded in its own thread
        _SENTINEL = object()
        result_q: _queue.Queue = _queue.Queue(maxsize=_BULK_DOWNLOAD_PARALLEL * 200)
        valid_files = [fe for fe in output_files if fe.get("url")]
        n_files = len(valid_files)
        error_q: _queue.Queue = _queue.Queue()

        def _download_one(file_entry):
            try:
                for resource in _download_bulk_ndjson(
                    file_entry["url"], token=token, timeout=timeout
                ):
                    result_q.put(resource)
            except Exception as exc:
                error_q.put(exc)
            finally:
                result_q.put(_SENTINEL)

        from utils.thread_pool import get_executor

        pool = get_executor()
        for fe in valid_files:
            pool.submit(_download_one, fe)
        finished = 0
        while finished < n_files:
            try:
                item = result_q.get(timeout=_QUEUE_GET_TIMEOUT_SEC)
            except _queue.Empty:
                raise ValueError(
                    f"Bulk download stalled: no data received for "
                    f"{_QUEUE_GET_TIMEOUT_SEC}s — possible thread crash"
                )
            if item is _SENTINEL:
                finished += 1
                continue
            total += 1
            yield item

        if not error_q.empty():
            raise error_q.get_nowait()

    log.info("bulk export: yielded %d resource(s) total", total)


def bulk_export(
    base_url,
    level="system",
    resource_type=None,
    group_id=None,
    type_filter=None,
    since=None,
    token=None,
    timeout=30,
):
    """Generator: initiate a FHIR Bulk Data Export and yield resource dicts.

    Implements the full Bulk Data Export protocol:
    kick-off -> poll -> download NDJSON files -> cleanup.

    Args:
        base_url:      FHIR server base URL, e.g. ``http://host:8080/fhir``
        level:         ``"system"``, ``"type"``, or ``"group"``
        resource_type: Required when ``level="type"``.  For system-level, used as
                       the ``_type`` param when ``type_filter`` is not set.
        group_id:      Required when ``level="group"`` — FHIR Group.id.
        type_filter:   Comma-separated resource types for the ``_type`` param
                       (overrides ``resource_type`` for system-level).
        since:         ``_since`` instant, e.g. ``"2024-01-01T00:00:00Z"``
        token:         Optional Bearer token (overrides ``FHIR_SOURCE_TOKEN`` env).
        timeout:       HTTP timeout per individual request in seconds.

    Yields individual FHIR resource dicts from exported NDJSON files.
    """
    status_url = bulk_export_kick_off(
        base_url,
        level=level,
        resource_type=resource_type,
        group_id=group_id,
        type_filter=type_filter,
        since=since,
        token=token,
        timeout=timeout,
    )

    # -- Poll until complete --
    try:
        manifest = _poll_bulk_status(status_url, token=token, timeout=timeout)
        yield from _download_manifest_files(manifest, token=token, timeout=timeout)
    finally:
        # -- Cleanup: best-effort DELETE --
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
