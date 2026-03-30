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

_worker_log = logging.getLogger("medanon.worker")
_store = None
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_max_concurrent: int = 3
_semaphore: asyncio.Semaphore | None = None


def init_worker(store, max_concurrent: int = 3) -> None:
    """Bind the job store and configure concurrency before the worker loop starts."""
    global _store, _max_concurrent
    _store = store
    _max_concurrent = max_concurrent


# ---------------------------------------------------------------------------
# Job executors (called via asyncio.to_thread — may block)
# ---------------------------------------------------------------------------

def _execute_bulk_export(job: Job) -> None:
    """Run a bulk-export job synchronously."""
    from integrations.fhir.client import bulk_export
    from pipeline.config_service import get_settings
    from pipeline.processor import process_data

    params = job.params
    server_url = params["server_url"]
    level = params.get("level", "system")
    resource_type = params.get("resource_type")
    type_filter = params.get("type_filter")
    since = params.get("since")
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    gen = bulk_export(
        server_url,
        level=level,
        resource_type=resource_type,
        type_filter=type_filter,
        since=since,
        token=token,
        timeout=timeout,
    )
    count = 0
    with open(output_path, "w", encoding="utf-8") as fh:
        for resource in gen:
            try:
                result = process_data(resource, settings)
                fh.write(json.dumps(result) + "\n")
                count += 1
            except Exception as exc:
                rtype = (
                    resource.get("resourceType", "Unknown")
                    if isinstance(resource, dict) else "Unknown"
                )
                _worker_log.error(
                    "bulk_export job=%s resource_type=%s error=%s",
                    job.id, rtype, exc,
                )
                fh.write(json.dumps({"error": "processing error", "resourceType": rtype}) + "\n")

    job.result_path = output_path
    _worker_log.info("bulk_export_done job=%s count=%d", job.id, count)


def _execute_cohort(job: Job) -> None:
    """Run a cohort export job synchronously."""
    from integrations.fhir.client import fetch_cohort
    from pipeline.config_service import get_settings
    from pipeline.processor import process_data

    params = job.params
    server_url = params["server_url"]
    search_type = params["search_type"]
    search_params = params.get("search_params", {})
    everything_params = params.get("everything_params", {})
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    gen = fetch_cohort(
        server_url,
        search_type=search_type,
        search_params=search_params,
        everything_params=everything_params,
        token=token,
        timeout=timeout,
    )
    count = 0
    with open(output_path, "w", encoding="utf-8") as fh:
        for resource in gen:
            try:
                result = process_data(resource, settings)
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

    job.result_path = output_path
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

async def _get_next_job(is_event_driven: bool) -> Job | None:
    """Retrieve the next job to execute."""
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
