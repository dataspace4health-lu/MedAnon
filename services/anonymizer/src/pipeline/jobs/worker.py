"""Background worker — event-driven (Redis) or polling (SQLite) job executor.

Call ``init_worker(store, max_concurrent)`` then
``asyncio.create_task(worker_loop())`` from the FastAPI startup event.

Supported job types:
    ``bulk-export``    — fetch + de-identify via gPAS bulk export, write NDJSON.
    ``cohort``         — cohort export ($everything per matching patient), write NDJSON.
    ``patient-export`` — patient $everything export, write NDJSON.
    ``bulk-import``    — read a completed NDJSON and upload to a target FHIR server.
    ``reprocess``      — re-run de-identification on staged rows with a new config profile.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone
from utils.json_fast import loads as _json_loads, dumps as _json_dumps
import os
from pathlib import Path

from medanon_core.domain import Job, JobStatus
from pipeline.jobs.checkpoint import load_checkpoint, save_checkpoint
from utils import audit

from pipeline.processor import process_data_batch, _BATCH_SIZE

_worker_log = logging.getLogger("medanon.worker")
_store = None
_staging = None  # StagingStore instance; set by init_staging() when MEDANON_STAGING_DB_URL is configured
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_HEARTBEAT_PATH = os.path.join(os.environ.get("MEDANON_OUTPUT_DIR", "/output"), "worker_healthy")
_max_concurrent: int = 3
_semaphore: asyncio.Semaphore | None = None
_shutdown_event: asyncio.Event | None = None
_active_tasks: set = set()
_DRAIN_TIMEOUT_SEC: int = int(os.environ.get("MEDANON_DRAIN_TIMEOUT_SEC", "300"))
_PROGRESS_INTERVAL: int = int(os.environ.get("MEDANON_PROGRESS_INTERVAL", "100"))
_STAGING_CLEANUP_INTERVAL_SEC: int = int(os.environ.get("MEDANON_STAGING_CLEANUP_INTERVAL_SEC", "3600"))
_MAX_JOB_RETRIES: int = int(os.environ.get("MEDANON_JOB_MAX_RETRIES", "3"))
_COMPRESS_RESULTS: bool = os.environ.get("MEDANON_COMPRESS_RESULTS", "false").strip().lower() in ("1", "true", "yes")
# Default: keep results for 7 days then auto-delete. Set to 0 to disable cleanup.
_RESULT_TTL_SEC: int = int(os.environ.get("MEDANON_RESULT_TTL_SEC", "604800"))
_RESULT_CLEANUP_INTERVAL_SEC: int = int(os.environ.get("MEDANON_RESULT_CLEANUP_INTERVAL_SEC", "3600"))


def _compress_ndjson(path: str) -> str:
    """Gzip-compress *path* in place.  Returns the ``.ndjson.gz`` path.

    Used after processing completes (not during) so that
    ``_truncate_to_lines`` checkpoint/resume still works on the
    uncompressed file during the processing phase.
    """
    import gzip
    import shutil

    gz_path = path + ".gz"
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1 << 20)
    os.remove(path)
    _worker_log.info("ndjson_compressed src=%s gz=%s", path, gz_path)
    return gz_path


def store_result(job_id: str, local_path: str) -> str:
    """Upload *local_path* to the configured result storage and return the result key.

    For local storage (default) the key is the path unchanged.
    For S3 (``MEDANON_RESULT_STORAGE=s3``) the file is uploaded to MinIO and the
    key is ``s3://<bucket>/<job_id>.ndjson``.
    """
    from integrations.storage import get_result_storage
    return get_result_storage().write_from_path(job_id, local_path)


def init_worker(store, max_concurrent: int = 3) -> None:
    """Bind the job store and configure concurrency before the worker loop starts."""
    global _store, _max_concurrent
    _store = store
    _max_concurrent = max_concurrent


def init_staging(staging_store) -> None:
    """Bind the StagingStore.  When set, bulk-export and cohort jobs use the
    two-phase staged path instead of the streaming fallback."""
    global _staging
    _staging = staging_store


# ---------------------------------------------------------------------------
# Idempotent resume helper
# ---------------------------------------------------------------------------

def _truncate_to_lines(path: str, line_count: int) -> None:
    """Truncate *path* to exactly *line_count* newline-terminated lines.

    On resume, the file may contain more lines than the checkpoint recorded
    (crash after write but before checkpoint update).  Truncating prevents
    duplicate resources in the output.
    """
    if line_count <= 0 or not os.path.exists(path):
        return
    with open(path, "r+b") as f:
        for _ in range(line_count):
            line = f.readline()
            if not line:
                return  # file has fewer lines than expected — nothing to truncate
        f.truncate()


# ---------------------------------------------------------------------------
# Chunked batch processing helper
# ---------------------------------------------------------------------------

def _process_stream_chunked(gen, settings, pseudonymizer, fh, start_count, store, job, label, summary=None):
    """Buffer resources from *gen* into chunks and process each via
    :func:`process_data_batch`, writing results to *fh*.

    Returns ``(count, was_cancelled)`` where *count* is the total number of
    lines written (including *start_count*).

    When *summary* (:class:`~pipeline.jobs.summary.JobSummaryCollector`) is
    provided, each processed resource is recorded for the completion summary.
    """
    count = start_count
    chunk: list[dict] = []

    def _flush():
        nonlocal count
        try:
            results = process_data_batch(chunk, settings, pseudonymizer)
            for result in results:
                line = _json_dumps(result)
                fh.write(line + "\n")
                count += 1
                if summary is not None:
                    summary.record_resource(result)
        except Exception:
            # Per-resource fallback for the failed chunk
            for resource in chunk:
                try:
                    result = process_data_batch([resource], settings, pseudonymizer)[0]
                    line = _json_dumps(result)
                    fh.write(line + "\n")
                    if summary is not None:
                        summary.record_resource(result)
                except Exception as exc2:
                    rtype = resource.get("resourceType", "Unknown") if isinstance(resource, dict) else "Unknown"
                    _worker_log.error("%s job=%s resource_type=%s error=%s", label, job.id, rtype, exc2)
                    fh.write(_json_dumps({"error": "processing error", "resourceType": rtype}) + "\n")
                    if summary is not None:
                        summary.record_error(rtype)
                count += 1

        # Flush after every chunk to guarantee NDJSON integrity on crash
        fh.flush()

        if count % _PROGRESS_INTERVAL < len(chunk):
            save_checkpoint(store, job, {"phase": "processing", "lines_written": count})

        fresh = store.get(job.id)
        if fresh and fresh.status == JobStatus.CANCELLED:
            return True
        return False

    for resource in gen:
        chunk.append(resource)
        if len(chunk) >= _BATCH_SIZE:
            cancelled = _flush()
            chunk = []
            if cancelled:
                _worker_log.info("%s_cancelled job=%s at_line=%d", label, job.id, count)
                return count, True

    if chunk:
        cancelled = _flush()
        chunk = []
        if cancelled:
            return count, True

    return count, False


def _skip_to(gen, n: int):
    """Yield items from *gen*, skipping the first *n* entries (resume helper)."""
    for i, item in enumerate(gen):
        if i >= n:
            yield item


# ---------------------------------------------------------------------------
# Job executors (called via asyncio.to_thread — may block)
# ---------------------------------------------------------------------------

def _execute_bulk_export(job: Job) -> None:
    """Run a bulk-export job synchronously, resuming from checkpoint when available."""
    if _staging is not None:
        from pipeline.jobs.staged_worker import execute_bulk_export_staged
        return execute_bulk_export_staged(job, _store, _staging)

    from integrations.fhir.client import (
        fetch_all_resource_types,
        get_capability_statement,
    )
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    resource_type = params.get("resource_type")
    type_filter = params.get("type_filter")
    since = params.get("since")
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()

    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)
        _worker_log.info("bulk_export_resume job=%s from_line=%d", job.id, already_written)

    # Determine which resource types to fetch
    if resource_type:
        resource_types = [resource_type]
    elif type_filter:
        resource_types = [t.strip() for t in type_filter.split(",") if t.strip()]
    else:
        # Discover all non-infrastructure types from the server's CapabilityStatement
        _INFRA = frozenset({
            "CapabilityStatement", "OperationDefinition", "SearchParameter",
            "StructureDefinition", "CompartmentDefinition", "ImplementationGuide",
            "CodeSystem", "ValueSet", "ConceptMap", "NamingSystem",
            "OperationOutcome", "Bundle",
        })
        try:
            all_types = get_capability_statement(server_url, token=token, timeout=timeout)
            resource_types = [t for t in all_types if t not in _INFRA]
        except Exception as exc:
            _worker_log.warning(
                "bulk_export capability_statement_failed job=%s: %s", job.id, exc
            )
            resource_types = ["Patient", "Observation", "Condition", "Encounter", "Procedure"]

    if not resource_types:
        _worker_log.info("bulk_export_empty job=%s — no resource types to export", job.id)
        Path(output_path).write_text("")
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(_store, job, {"phase": "done", "lines_written": 0})
        return

    extra_params: dict = {}
    if since:
        extra_params["_lastUpdated"] = f"ge{since}"

    save_checkpoint(_store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_all_resource_types(
        server_url, resource_types,
        params=extra_params if extra_params else None,
        token=token, timeout=timeout,
    )

    from pipeline.jobs.summary import JobSummaryCollector
    collector = JobSummaryCollector(config_profile=profile)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = _process_stream_chunked(
        _skip_to((_rt_res[1] for _rt_res in gen), already_written),  # strip (rtype, resource) tuples
            settings, pseudonymizer, fh,
            already_written, _store, job, "bulk_export",
            summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = _compress_ndjson(output_path)
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(_store, job, {
            "phase": "done",
            "lines_written": count,
            "summary": collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            ),
        })
        _worker_log.info("bulk_export_done job=%s count=%d", job.id, count)


def _execute_cohort(job: Job) -> None:
    """Run a cohort export job synchronously, resuming from checkpoint when available."""
    if _staging is not None:
        from pipeline.jobs.staged_worker import execute_cohort_staged
        return execute_cohort_staged(job, _store, _staging)

    from integrations.fhir.client import fetch_cohort, preflight_resource_count
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    search_type = params["search_type"]
    search_params = params.get("search_params", {})
    everything_params = params.get("everything_params", {})
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()

    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)
        _worker_log.info("cohort_resume job=%s from_line=%d", job.id, already_written)

    # Preflight: quick count to bail early on empty servers
    if already_written == 0:
        count = preflight_resource_count(server_url, resource_type=search_type, token=token)
        if count == 0:
            _worker_log.info("cohort_empty job=%s — no %s resources found, skipping export", job.id, search_type)
            Path(output_path).write_text("")
            job.result_path = store_result(job.id, output_path)
            save_checkpoint(_store, job, {"phase": "done", "lines_written": 0})
            return

    save_checkpoint(_store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_cohort(
        server_url,
        search_type=search_type,
        search_params=search_params,
        everything_params=everything_params,
        token=token,
        timeout=timeout,
    )

    from pipeline.jobs.summary import JobSummaryCollector
    collector = JobSummaryCollector(config_profile=profile)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = _process_stream_chunked(
            _skip_to(gen, already_written),
            settings, pseudonymizer, fh,
            already_written, _store, job, "cohort",
            summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = _compress_ndjson(output_path)
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(_store, job, {
            "phase": "done",
            "lines_written": count,
            "summary": collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            ),
        })
        _worker_log.info("cohort_done job=%s count=%d", job.id, count)


def _execute_reprocess(job: Job) -> None:
    """Re-process staged rows with a (possibly different) config profile."""
    if _staging is None:
        job.error = "Staging not configured (MEDANON_STAGING_DB_URL not set)"
        raise RuntimeError(job.error)
    from pipeline.jobs.staged_worker import execute_reprocess_staged
    return execute_reprocess_staged(job, _store, _staging)


def _execute_patient_export(job: Job) -> None:
    """Run a patient $everything export + de-identify job."""
    if _staging is not None:
        from pipeline.jobs.staged_worker import execute_patient_export_staged
        return execute_patient_export_staged(job, _store, _staging)

    from integrations.fhir.client import fetch_everything
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    patient_id = params["patient_id"]
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()

    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)

    save_checkpoint(_store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_everything(server_url, "Patient", patient_id, token=token, timeout=timeout)

    from pipeline.jobs.summary import JobSummaryCollector
    collector = JobSummaryCollector(config_profile=profile)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = _process_stream_chunked(
            _skip_to(gen, already_written),
            settings, pseudonymizer, fh,
            already_written, _store, job, "patient_export",
            summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = _compress_ndjson(output_path)
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(_store, job, {
            "phase": "done",
            "lines_written": count,
            "summary": collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            ),
        })
        _worker_log.info("patient_export_done job=%s count=%d", job.id, count)


def _execute_bulk_import(job: Job) -> None:
    """Read a completed NDJSON result and upload resources to a target FHIR server.

    Accepts job params:
        ``job_id``       — source export Job whose result_path is used.
        ``ndjson_path``  — explicit NDJSON path (local or s3://); used when job_id absent.
        ``target_url``   — target FHIR server base URL (required).
        ``target_token`` — bearer token for the target (optional).
        ``timeout``      — per-request HTTP timeout in seconds (default 30).
        ``parallel``     — concurrent FHIR batch Bundle POSTs per tier (default 4).
        ``batch_size``   — resources per FHIR batch Bundle (default 500).

    Writes the source NDJSON path as the job result_path so the result endpoint
    can still serve the original de-identified file.
    """
    from integrations.fhir.writer import upload_resources
    from integrations.storage import get_result_storage

    params = job.params
    source_job_id = params.get("job_id")
    ndjson_path = params.get("ndjson_path")
    target_url = params["target_url"]
    target_token = params.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = float(params.get("timeout", 30))
    parallel = int(params.get("parallel", os.environ.get("MEDANON_UPLOAD_PARALLEL", "4")))
    batch_size = int(params.get("batch_size", os.environ.get("MEDANON_UPLOAD_BATCH_SIZE", "500")))

    # Resolve the NDJSON path from the source job if job_id was provided
    if source_job_id:
        src = _store.get(source_job_id)
        if src is None or not src.result_path:
            raise ValueError(f"Source job {source_job_id!r} not found or has no result")
        ndjson_path = src.result_path

    if not ndjson_path:
        raise ValueError("bulk-import job requires 'job_id' or 'ndjson_path' in params")

    save_checkpoint(_store, job, {"phase": "loading"})

    storage = get_result_storage()
    resources: list[dict] = []
    stream = storage.open_stream(ndjson_path)
    try:
        for raw_line in stream:
            line = raw_line.decode("utf-8") if isinstance(raw_line, (bytes, bytearray)) else raw_line
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json_loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and obj.get("resourceType") and "error" not in obj:
                resources.append(obj)
    finally:
        if hasattr(stream, "close"):
            stream.close()

    total = len(resources)
    _worker_log.info(
        "bulk_import_start job=%s source=%s total=%d parallel=%d batch_size=%d",
        job.id, source_job_id or ndjson_path, total, parallel, batch_size,
    )
    # Include staged_count in every checkpoint so the API response exposes it.
    # The service maps: processed = checkpoint["lines_written"], staged_count = checkpoint["staged_count"]
    save_checkpoint(_store, job, {"phase": "uploading", "lines_written": 0, "staged_count": total})

    uploaded = errors = 0
    for result in upload_resources(target_url, resources, token=target_token,
                                   timeout=timeout, parallel=parallel,
                                   batch_size=batch_size):
        if result.get("success"):
            uploaded += 1
        else:
            errors += 1
        done_count = uploaded + errors
        if done_count % 500 == 0:
            save_checkpoint(_store, job, {
                "phase": "uploading",
                "lines_written": done_count,
                "staged_count": total,
            })

    save_checkpoint(_store, job, {
        "phase": "done",
        "lines_written": uploaded + errors,
        "staged_count": total,
        "errors": errors,
    })
    job.result_path = ndjson_path  # re-use source NDJSON; no new file written
    _store.update(job)
    _worker_log.info(
        "bulk_import_done job=%s uploaded=%d errors=%d",
        job.id, uploaded, errors,
    )


def _execute_batch_patient_export(job: Job) -> None:
    """Run a batch patient $everything export + de-identify job.

    Fetches $everything for each patient ID in the job params, de-duplicates
    shared resources, processes in chunks, and writes a single combined NDJSON.
    """
    if _staging is not None:
        from pipeline.jobs.staged_worker import execute_batch_patient_export_staged
        return execute_batch_patient_export_staged(job, _store, _staging)

    from integrations.fhir.client import fetch_patients_everything
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    patient_ids = params["patient_ids"]
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()

    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)
        _worker_log.info("batch_patient_resume job=%s from_line=%d", job.id, already_written)

    save_checkpoint(_store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_patients_everything(
        server_url, patient_ids, token=token, timeout=timeout,
    )

    from pipeline.jobs.summary import JobSummaryCollector
    collector = JobSummaryCollector(config_profile=profile)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = _process_stream_chunked(
            _skip_to(gen, already_written),
            settings, pseudonymizer, fh,
            already_written, _store, job, "batch_patient_export",
            summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = _compress_ndjson(output_path)
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(_store, job, {
            "phase": "done",
            "lines_written": count,
            "summary": collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            ),
        })
        _worker_log.info(
            "batch_patient_export_done job=%s patients=%d count=%d",
            job.id, len(patient_ids), count,
        )


_EXECUTORS = {
    "bulk-export": _execute_bulk_export,
    "cohort": _execute_cohort,
    "reprocess": _execute_reprocess,
    "patient-export": _execute_patient_export,
    "bulk-import": _execute_bulk_import,
    "batch-patient-export": _execute_batch_patient_export,
}


# ---------------------------------------------------------------------------
# Staging cleanup loop (runs alongside the worker loop)
# ---------------------------------------------------------------------------

async def _cleanup_loop() -> None:
    """Periodically delete expired staging rows.  Runs only when staging is active."""
    while True:
        await asyncio.sleep(_STAGING_CLEANUP_INTERVAL_SEC)
        if _staging is None:
            continue
        try:
            deleted = await asyncio.to_thread(_staging.cleanup_expired)
            if deleted:
                _worker_log.info("staging_cleanup deleted=%d", deleted)
        except Exception as exc:
            _worker_log.warning("staging_cleanup_error: %s", type(exc).__name__)


def _cleanup_expired_results() -> int:
    """Delete result files for done jobs older than ``_RESULT_TTL_SEC``.

    Iterates all ``done`` jobs in pages of 200.  For each job whose
    ``updated_at`` is older than the TTL and whose result file still exists,
    the file is deleted via the configured storage backend.

    Returns the number of files deleted.
    """
    if _store is None or _RESULT_TTL_SEC <= 0:
        return 0
    from integrations.storage import get_result_storage
    storage = get_result_storage()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_RESULT_TTL_SEC)
    deleted = 0
    offset = 0
    while True:
        jobs = _store.list_jobs(status="done", limit=200, offset=offset)
        if not jobs:
            break
        for job in jobs:
            if not job.result_path:
                continue
            try:
                updated = datetime.fromisoformat(job.updated_at)
                # Ensure timezone-aware for comparison
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if updated >= cutoff:
                continue
            if storage.exists(job.result_path):
                storage.delete(job.result_path)
                deleted += 1
                _worker_log.info(
                    "result_expired_deleted job=%s age_days=%.1f path=%s",
                    job.id,
                    (datetime.now(timezone.utc) - updated).total_seconds() / 86400,
                    job.result_path,
                )
        if len(jobs) < 200:
            break
        offset += 200
    return deleted


async def _result_cleanup_loop() -> None:
    """Periodically delete result files for jobs older than ``_RESULT_TTL_SEC``."""
    while True:
        await asyncio.sleep(_RESULT_CLEANUP_INTERVAL_SEC)
        if _RESULT_TTL_SEC <= 0:
            continue
        try:
            deleted = await asyncio.to_thread(_cleanup_expired_results)
            if deleted:
                _worker_log.info("result_cleanup deleted=%d files", deleted)
        except Exception as exc:
            _worker_log.warning("result_cleanup_error: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Concurrent job runner
# ---------------------------------------------------------------------------

async def _run_job(job: Job) -> None:
    """Execute a single job within the semaphore-bounded pool."""
    if _store is None:
        return
    executor = _EXECUTORS.get(job.type)
    if executor is None:
        job.status = JobStatus.ERROR
        job.error = f"Unknown job type: {job.type!r}"
        _store.update(job)
        return

    # Poison job protection: check retry count from checkpoint data.
    # _retry_count is incremented only after a failed attempt (in the except
    # block below), so checking >= here gives exactly _MAX_JOB_RETRIES attempts.
    cp = (job.checkpoint_data or {}) if hasattr(job, "checkpoint_data") else {}
    retry_count = int(cp.get("_retry_count", 0))
    if retry_count >= _MAX_JOB_RETRIES:
        job.status = JobStatus.ERROR
        job.error = f"Exceeded max retries ({_MAX_JOB_RETRIES})"
        _store.update(job)
        _worker_log.error("job_poison id=%s retries=%d — giving up", job.id, retry_count)
        return

    job.status = JobStatus.RUNNING
    _store.update(job)
    _worker_log.info("job_start id=%s type=%s retry=%d", job.id, job.type, retry_count)
    import time as _time
    from utils.metrics import WORKER_JOBS_TOTAL, WORKER_JOB_DURATION, WORKER_ACTIVE_JOBS
    WORKER_ACTIVE_JOBS.inc()
    _t0 = _time.monotonic()
    try:
        await asyncio.to_thread(executor, job)
        # Don't overwrite a CANCELLED status set externally while we were running
        refreshed = _store.get(job.id)
        if refreshed and refreshed.status == JobStatus.CANCELLED:
            _worker_log.info("job_cancelled id=%s", job.id)
            WORKER_JOBS_TOTAL.labels(job_type=job.type, status="cancelled").inc()
            return
        job.status = JobStatus.DONE
        _worker_log.info("job_done id=%s", job.id)
        WORKER_JOBS_TOTAL.labels(job_type=job.type, status="done").inc()
        audit.emit("job.complete", resource_id=job.id, resource_type=job.type, outcome="success")
    except Exception as exc:
        job.status = JobStatus.ERROR
        job.error = str(exc)[:500]
        # Increment retry count on failure so crash-recovered jobs are bounded
        cp = (job.checkpoint_data or {}) if hasattr(job, "checkpoint_data") else {}
        cp["_retry_count"] = int(cp.get("_retry_count", 0)) + 1
        save_checkpoint(_store, job, cp)
        _worker_log.error("job_error id=%s: %s", job.id, exc)
        WORKER_JOBS_TOTAL.labels(job_type=job.type, status="error").inc()
        audit.emit("job.complete", resource_id=job.id, resource_type=job.type, outcome="error", detail={"error": str(exc)[:200]})
    finally:
        WORKER_JOB_DURATION.labels(job_type=job.type).observe(_time.monotonic() - _t0)
        WORKER_ACTIVE_JOBS.dec()
    _store.update(job)


async def _run_and_release(job: Job, message_id: str | None = None) -> None:
    """Run a job, release the semaphore, and ACK the stream message when done."""
    task = asyncio.current_task()
    _active_tasks.add(task)
    try:
        await _run_job(job)
    finally:
        _active_tasks.discard(task)
        if _semaphore is not None:
            _semaphore.release()
        # ACK the Redis Streams message so it's removed from the Pending Entry List.
        # Called after _run_job so a crash before this point causes redelivery (at-least-once).
        if message_id and _store is not None and hasattr(_store, "ack_job"):
            try:
                await asyncio.to_thread(_store.ack_job, message_id)
            except Exception as exc:
                _worker_log.warning("stream_ack_failed message_id=%s: %s", message_id, exc)


# ---------------------------------------------------------------------------
# Worker loop — event-driven for Redis, polling for SQLite
# ---------------------------------------------------------------------------

def _check_retry_limit(job) -> bool:
    """Check if a job has exceeded its retry limit. Returns True if poisoned (should not retry)."""
    cp = (job.checkpoint_data or {}) if hasattr(job, "checkpoint_data") else {}
    retry_count = int(cp.get("_retry_count", 0))
    if retry_count >= _MAX_JOB_RETRIES:
        job.status = JobStatus.ERROR
        job.error = f"Exceeded max retries ({_MAX_JOB_RETRIES})"
        _store.update(job)
        _worker_log.error("job_poison id=%s retries=%d — giving up", job.id, retry_count)
        return True
    return False


def _recover_running_jobs() -> int:
    """Reset RUNNING jobs from a crashed process back to PENDING.

    For Redis Streams, claims ALL pending PEL messages at startup (min_idle_ms=0
    is safe here because no other worker is running — we just started).  Also
    scans the job-status index for any RUNNING jobs not covered by the stream
    (e.g. migrated from BLPOP).

    Jobs that have exceeded ``_MAX_JOB_RETRIES`` are moved to ERROR instead.

    Returns the count of recovered jobs.
    """
    if _store is None or not hasattr(_store, "list_jobs"):
        return 0
    recovered = 0
    # Redis Streams: at startup, claim ALL pending messages (idle ≥ 0 ms).
    # This reclaims work from any previous worker that crashed without ACKing.
    if hasattr(_store, "claim_stale_jobs"):
        try:
            stale_ids = _store.claim_stale_jobs(min_idle_ms=0)
            for job_id in stale_ids:
                job = _store.get(job_id)
                if job and job.status == JobStatus.RUNNING:
                    if _check_retry_limit(job):
                        continue
                    job.status = JobStatus.PENDING
                    job.error = None
                    _store.update(job)
                    recovered += 1
        except Exception as exc:
            _worker_log.warning("recovery_claim_failed: %s", type(exc).__name__)
    # Also scan for RUNNING jobs in the status index — covers jobs queued via BLPOP
    # before the Streams migration, and edge cases where the stream was reset.
    try:
        stuck = _store.list_jobs(status="running", limit=100)
        for job in stuck:
            if _check_retry_limit(job):
                continue
            job.status = JobStatus.PENDING
            job.error = None
            _store.update(job)
            recovered += 1
    except Exception as exc:
        _worker_log.warning("recovery_scan_failed: %s", type(exc).__name__)
    return recovered


async def _get_next_job(is_event_driven: bool) -> tuple[Job | None, str | None]:
    """Fetch the next pending job.

    Returns ``(job, message_id)`` where *message_id* is the Redis Stream
    message ID (for ACKing) or ``None`` for SQLite / BLPOP stores.
    """
    if is_event_driven:
        result = await asyncio.to_thread(_store.wait_for_job, 5)
        if result is None:
            return None, None
        # Redis Streams returns (job_id, message_id); legacy BLPOP returns just job_id
        if isinstance(result, tuple):
            job_id, message_id = result
        else:
            job_id, message_id = result, None
        job = _store.get(job_id)
        if job is None or job.status != JobStatus.PENDING:
            # Phantom / stale message — ACK it immediately so it doesn't block the queue
            if message_id and hasattr(_store, "ack_job"):
                try:
                    _store.ack_job(message_id)
                except Exception:
                    pass
            return None, None
        return job, message_id
    else:
        return await asyncio.to_thread(_store.next_pending), None


async def worker_loop() -> None:
    """Continuously consume jobs, executing up to max_concurrent in parallel.

    Handles SIGTERM/SIGINT for graceful drain: stops accepting new jobs,
    waits for in-flight jobs to complete (up to MEDANON_DRAIN_TIMEOUT_SEC),
    then exits cleanly.
    """
    global _semaphore, _shutdown_event
    if _store is None:
        _worker_log.error("worker_loop called before init_worker()")
        return

    _semaphore = asyncio.Semaphore(_max_concurrent)
    _shutdown_event = asyncio.Event()

    # Register signal handlers for graceful drain
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _shutdown_event.set)
        except (NotImplementedError, OSError):
            # Windows or non-main thread — signals won't be caught
            pass

    is_event_driven = hasattr(_store, "wait_for_job")

    # Recover jobs left in RUNNING state by a previous process that crashed.
    recovered = _recover_running_jobs()
    if recovered:
        _worker_log.warning("worker_startup_recovered jobs=%d", recovered)

    if is_event_driven:
        _worker_log.info(
            "worker_mode=event-driven max_concurrent=%d", _max_concurrent
        )
    else:
        _worker_log.info(
            "worker_mode=polling interval=2s max_concurrent=%d", _max_concurrent
        )

    if _staging is not None:
        asyncio.create_task(_cleanup_loop())
        _worker_log.info("staging_cleanup scheduled interval_sec=%d", _STAGING_CLEANUP_INTERVAL_SEC)

    if _RESULT_TTL_SEC > 0:
        asyncio.create_task(_result_cleanup_loop())
        _worker_log.info(
            "result_cleanup scheduled interval_sec=%d ttl_days=%.1f",
            _RESULT_CLEANUP_INTERVAL_SEC, _RESULT_TTL_SEC / 86400,
        )
    else:
        _worker_log.warning(
            "result_cleanup disabled (MEDANON_RESULT_TTL_SEC=0) — NDJSON files will accumulate"
        )

    _poll_count = 0
    # Touch heartbeat file to signal readiness
    Path(_HEARTBEAT_PATH).touch()
    while not _shutdown_event.is_set():
        await _semaphore.acquire()
        if _shutdown_event.is_set():
            _semaphore.release()
            break
        try:
            job, message_id = await _get_next_job(is_event_driven)
            if job is None:
                _semaphore.release()
                # Touch heartbeat even on idle loops
                Path(_HEARTBEAT_PATH).touch()
                if not is_event_driven:
                    # Use wait with timeout so shutdown signal can interrupt sleep
                    try:
                        await asyncio.wait_for(_shutdown_event.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
                continue
            # Touch heartbeat on every job dispatch
            Path(_HEARTBEAT_PATH).touch()
            asyncio.create_task(_run_and_release(job, message_id))
            # Periodically update the queue depth metric (every 50 jobs)
            _poll_count += 1
            if _poll_count % 50 == 0 and hasattr(_store, "get_queue_depth"):
                try:
                    from utils.metrics import JOB_QUEUE_DEPTH
                    JOB_QUEUE_DEPTH.set(_store.get_queue_depth())
                except Exception:
                    pass
        except Exception as exc:
            _semaphore.release()
            _worker_log.error("worker_loop_error: %s", exc)
            await asyncio.sleep(2)

    # Graceful drain: wait for in-flight jobs to finish
    if _active_tasks:
        _worker_log.info(
            "worker_draining active_jobs=%d timeout=%ds",
            len(_active_tasks), _DRAIN_TIMEOUT_SEC,
        )
        _, pending = await asyncio.wait(
            _active_tasks, timeout=_DRAIN_TIMEOUT_SEC,
        )
        if pending:
            _worker_log.warning(
                "worker_drain_timeout remaining=%d — exiting with jobs still running",
                len(pending),
            )
        else:
            _worker_log.info("worker_drain_complete — all jobs finished")
    else:
        _worker_log.info("worker_shutdown — no active jobs")
