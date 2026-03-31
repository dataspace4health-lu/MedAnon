"""Background worker — event-driven (Redis) or polling (SQLite) job executor.

Call ``init_worker(store, max_concurrent)`` then
``asyncio.create_task(worker_loop())`` from the FastAPI startup event.

Supported job types:
    ``bulk-export`` — fetch + de-identify via gPAS bulk export, write NDJSON.
    ``cohort``      — cohort export ($everything per matching patient), write NDJSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from medanon_core.domain import Job, JobStatus
from pipeline.checkpoint import load_checkpoint, save_checkpoint

_worker_log = logging.getLogger("medanon.worker")
_store = None
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_max_concurrent: int = 3
_semaphore: asyncio.Semaphore | None = None
_PROGRESS_INTERVAL: int = int(os.environ.get("MEDANON_PROGRESS_INTERVAL", "100"))


def init_worker(store, max_concurrent: int = 3) -> None:
    """Bind the job store and configure concurrency before the worker loop starts."""
    global _store, _max_concurrent
    _store = store
    _max_concurrent = max_concurrent


# ---------------------------------------------------------------------------
# Job executors (called via asyncio.to_thread — may block)
# ---------------------------------------------------------------------------

def _execute_bulk_export(job: Job) -> None:
    """Run a bulk-export job synchronously, resuming from checkpoint when available."""
    from integrations.fhir.client import (
        fetch_all_resource_types,
        get_capability_statement,
    )
    from pipeline.config_service import get_settings
    from pipeline.processor import process_data, _get_default_pseudonymizer

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
        job.result_path = output_path
        save_checkpoint(_store, job, {"phase": "done", "lines_written": 0})
        return

    extra_params: dict = {}
    if since:
        extra_params["_lastUpdated"] = f"ge{since}"

    # Phase 1: fetching + Phase 2: processing (streaming — no $export round-trip)
    save_checkpoint(_store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_all_resource_types(
        server_url, resource_types,
        params=extra_params if extra_params else None,
        token=token, timeout=timeout,
    )

    count = already_written
    with open(output_path, open_mode, encoding="utf-8") as fh:
        for i, (rt, resource) in enumerate(gen):
            if i < already_written:
                continue  # skip resources already written in a previous run
            try:
                result = process_data(resource, settings, pseudonymizer)
                fh.write(json.dumps(result) + "\n")
                count += 1
            except Exception as exc:
                _worker_log.error(
                    "bulk_export job=%s resource_type=%s error=%s",
                    job.id, rt, exc,
                )
                fh.write(json.dumps({"error": "processing error", "resourceType": rt}) + "\n")
                count += 1
            if count % _PROGRESS_INTERVAL == 0:
                fh.flush()
                save_checkpoint(_store, job, {"phase": "processing", "lines_written": count})
                fresh = _store.get(job.id)
                if fresh and fresh.status == JobStatus.CANCELLED:
                    _worker_log.info("bulk_export_cancelled job=%s at_line=%d", job.id, count)
                    return

    job.result_path = output_path
    save_checkpoint(_store, job, {"phase": "done", "lines_written": count})
    _worker_log.info("bulk_export_done job=%s count=%d", job.id, count)


def _execute_cohort(job: Job) -> None:
    """Run a cohort export job synchronously, resuming from checkpoint when available."""
    from integrations.fhir.client import fetch_cohort, preflight_resource_count
    from pipeline.config_service import get_settings
    from pipeline.processor import process_data, _get_default_pseudonymizer

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
        _worker_log.info("cohort_resume job=%s from_line=%d", job.id, already_written)

    # Preflight: quick count to bail early on empty servers
    if already_written == 0:
        count = preflight_resource_count(server_url, resource_type=search_type, token=token)
        if count == 0:
            _worker_log.info("cohort_empty job=%s — no %s resources found, skipping export", job.id, search_type)
            Path(output_path).write_text("")
            job.result_path = output_path
            save_checkpoint(_store, job, {"phase": "done", "lines_written": 0})
            return

    # Phase 1: fetching data from FHIR server
    save_checkpoint(_store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_cohort(
        server_url,
        search_type=search_type,
        search_params=search_params,
        everything_params=everything_params,
        token=token,
        timeout=timeout,
    )
    # Phase 2: processing resources
    count = already_written
    with open(output_path, open_mode, encoding="utf-8") as fh:
        for i, resource in enumerate(gen):
            if i < already_written:
                continue  # skip resources already written in a previous run
            try:
                result = process_data(resource, settings, pseudonymizer)
                fh.write(json.dumps(result) + "\n")
                count += 1
            except Exception as exc:
                rtype = (
                    resource.get("resourceType", "Unknown")
                    if isinstance(resource, dict) else "Unknown"
                )
                _worker_log.error(
                    "cohort job=%s resource_type=%s error=%s",
                    job.id, rtype, exc,
                )
                fh.write(json.dumps({"error": "processing error", "resourceType": rtype}) + "\n")
                count += 1
            if count % _PROGRESS_INTERVAL == 0:
                fh.flush()
                save_checkpoint(_store, job, {"phase": "processing", "lines_written": count})
                fresh = _store.get(job.id)
                if fresh and fresh.status == JobStatus.CANCELLED:
                    _worker_log.info("cohort_cancelled job=%s at_line=%d", job.id, count)
                    return

    job.result_path = output_path
    save_checkpoint(_store, job, {"phase": "done", "lines_written": count})
    _worker_log.info("cohort_done job=%s count=%d", job.id, count)


_EXECUTORS = {
    "bulk-export": _execute_bulk_export,
    "cohort": _execute_cohort,
}


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

    job.status = JobStatus.RUNNING
    _store.update(job)
    _worker_log.info("job_start id=%s type=%s", job.id, job.type)
    try:
        await asyncio.to_thread(executor, job)
        # Don't overwrite a CANCELLED status set externally while we were running
        refreshed = _store.get(job.id)
        if refreshed and refreshed.status == JobStatus.CANCELLED:
            _worker_log.info("job_cancelled id=%s", job.id)
            return
        job.status = JobStatus.DONE
        _worker_log.info("job_done id=%s", job.id)
    except Exception as exc:
        job.status = JobStatus.ERROR
        job.error = str(exc)[:500]
        _worker_log.error("job_error id=%s: %s", job.id, exc)
    _store.update(job)


async def _run_and_release(job: Job) -> None:
    """Run a job and release the semaphore when done."""
    try:
        await _run_job(job)
    finally:
        if _semaphore is not None:
            _semaphore.release()


# ---------------------------------------------------------------------------
# Worker loop — event-driven for Redis, polling for SQLite
# ---------------------------------------------------------------------------

def _recover_running_jobs() -> int:
    """Reset jobs left in RUNNING state from a previous crashed process to PENDING.

    Returns the count of recovered jobs.
    """
    if _store is None or not hasattr(_store, "list_jobs"):
        return 0
    recovered = 0
    try:
        stuck = _store.list_jobs(status="running", limit=100)
        for job in stuck:
            job.status = JobStatus.PENDING
            job.error = None
            _store.update(job)
            recovered += 1
    except Exception as exc:
        _worker_log.warning("recovery_scan_failed: %s", type(exc).__name__)
    return recovered


async def _get_next_job(is_event_driven: bool) -> Job | None:
    if is_event_driven:
        job_id = await asyncio.to_thread(_store.wait_for_job, 5)
        if job_id is None:
            return None
        job = _store.get(job_id)
        if job is None or job.status != JobStatus.PENDING:
            return None
        return job
    else:
        return await asyncio.to_thread(_store.next_pending)


async def worker_loop() -> None:
    """Continuously consume jobs, executing up to max_concurrent in parallel."""
    global _semaphore
    if _store is None:
        _worker_log.error("worker_loop called before init_worker()")
        return

    _semaphore = asyncio.Semaphore(_max_concurrent)
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

    while True:
        await _semaphore.acquire()
        try:
            job = await _get_next_job(is_event_driven)
            if job is None:
                _semaphore.release()
                if not is_event_driven:
                    await asyncio.sleep(2)
                continue
            asyncio.create_task(_run_and_release(job))
        except Exception as exc:
            _semaphore.release()
            _worker_log.error("worker_loop_error: %s", exc)
            await asyncio.sleep(2)
