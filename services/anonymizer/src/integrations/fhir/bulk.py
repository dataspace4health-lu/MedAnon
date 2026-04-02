"""FHIR Bulk Data Export operations — kick-off, poll, download, cleanup.

Implements the FHIR Bulk Data Access IG protocol: ``$export`` kick-off,
status polling with Retry-After, NDJSON file download, and best-effort
server-side cleanup.
"""

import json
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

__all__ = [
    "_poll_bulk_status",
    "_download_bulk_ndjson",
    "bulk_export",
    "delete_bulk_export",
]


# ---------------------------------------------------------------------------
# Bulk Data Export ($export)
# ---------------------------------------------------------------------------

def _poll_bulk_status(status_url, token=None, timeout=30):
    """Poll a Bulk Data Export status URL until completion or timeout.

    Returns the completed export manifest dict (JSON with ``output[]`` etc.).
    Raises ``ValueError`` on HTTP errors or if ``_FHIR_BULK_POLL_TIMEOUT`` is exceeded.
    """
    headers = _make_headers(token)
    deadline = time.monotonic() + _t._FHIR_BULK_POLL_TIMEOUT

    while True:
        if time.monotonic() > deadline:
            raise ValueError(
                f"Bulk export poll timeout ({_t._FHIR_BULK_POLL_TIMEOUT}s) exceeded for {status_url}"
            )
        resp = _do_raw_request("GET", status_url, headers, timeout, operation="bulk_poll")

        if resp.status == 200:
            return json.loads(resp.data.decode("utf-8"))

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
            time.sleep(delay)
            continue

        raise ValueError(
            f"Unexpected status {resp.status} while polling bulk export at {status_url}"
        )


def _download_bulk_ndjson(file_url, token=None, timeout=60):
    """Download a single NDJSON file from a bulk export output URL.

    Yields individual resource dicts (one per NDJSON line).
    Streams the response in 64 KB chunks to avoid buffering the entire file.
    """
    headers = _make_headers(token)
    headers["Accept"] = "application/fhir+ndjson"
    resp = _pool.urlopen(
        "GET", file_url, headers=headers, preload_content=False, timeout=timeout,
    )
    if resp.status >= 400:
        resp.release_conn()
        raise ValueError(f"FHIR server HTTP {resp.status} for {file_url}")
    try:
        buf = b""
        for chunk in resp.stream(65536):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"Malformed NDJSON line from {file_url}: {exc}"
                        ) from exc
        # process any remaining bytes (last line without trailing newline)
        line = buf.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Malformed NDJSON line from {file_url}: {exc}"
                ) from exc
    finally:
        resp.release_conn()


def bulk_export(base_url, level="system", resource_type=None, type_filter=None,
                since=None, token=None, timeout=30):
    """Generator: initiate a FHIR Bulk Data Export and yield resource dicts.

    Implements the full Bulk Data Export protocol:
    kick-off -> poll -> download NDJSON files -> cleanup.

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

    # -- Build kick-off URL --
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

    # -- Kick-off request --
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

    # -- Poll until complete --
    try:
        manifest = _poll_bulk_status(status_url, token=token, timeout=timeout)

        # Log any errors reported in the manifest
        for err_entry in manifest.get("error", []):
            err_url = err_entry.get("url", "")
            log.warning("bulk export reported error file: %s", err_url)

        # -- Download each output NDJSON file --
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
