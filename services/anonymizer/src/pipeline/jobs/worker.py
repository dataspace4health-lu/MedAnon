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
import os
from pathlib import Path

from medanon_core.domain import Job, JobStatus
from pipeline.jobs.checkpoint import save_checkpoint
from utils import audit

_worker_log = logging.getLogger("medanon.worker")
_store = None
_staging = None  # StagingStore instance; set by init_staging() when MEDANON_STAGING_DB_URL is configured
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_HEARTBEAT_PATH = os.path.join(
    os.environ.get("MEDANON_OUTPUT_DIR", "/output"), "worker_healthy"
)
_max_concurrent: int = 3
_semaphore: asyncio.Semaphore | None = None
_shutdown_event: asyncio.Event | None = None
_active_tasks: set = set()
_DRAIN_TIMEOUT_SEC: int = int(os.environ.get("MEDANON_DRAIN_TIMEOUT_SEC", "300"))
_STAGING_CLEANUP_INTERVAL_SEC: int = int(
    os.environ.get("MEDANON_STAGING_CLEANUP_INTERVAL_SEC", "3600")
)
_MAX_JOB_RETRIES: int = int(os.environ.get("MEDANON_JOB_MAX_RETRIES", "3"))
# Default: keep results for 7 days then auto-delete.
# Set to 0 to disable cleanup.
_RESULT_TTL_SEC: int = int(os.environ.get("MEDANON_RESULT_TTL_SEC", "604800"))
_RESULT_CLEANUP_INTERVAL_SEC: int = int(
    os.environ.get("MEDANON_RESULT_CLEANUP_INTERVAL_SEC", "3600")
)


# ---------------------------------------------------------------------------
# Public helpers re-exported for callers that import from this module
# ---------------------------------------------------------------------------

from pipeline.jobs.checkpoint import _truncate_to_lines  # noqa: E402  (re-export)
from pipeline.jobs.executors import (  # noqa: E402
    _execute_batch_patient_export,
    _execute_bulk_export,
    _execute_bulk_import,
    _execute_cohort,
    _execute_patient_export,
    _execute_reprocess,
)


def store_result(job_id: str, local_path: str) -> str:
    """Upload *local_path* to the configured result storage and return the result key."""
    from integrations.storage import get_result_storage

    return get_result_storage().write_from_path(job_id, local_path)


# Dispatch table: job type → executor function.
# Lambdas close over the module-level _store/_staging so they resolve at call time.
_EXECUTORS = {
    "bulk-export": lambda job: _execute_bulk_export(job, _store, _staging),
    "cohort": lambda job: _execute_cohort(job, _store, _staging),
    "reprocess": lambda job: _execute_reprocess(job, _store, _staging),
    "patient-export": lambda job: _execute_patient_export(job, _store, _staging),
    "bulk-import": lambda job: _execute_bulk_import(job, _store, _staging),
    "batch-patient-export": lambda job: _execute_batch_patient_export(job, _store, _staging),
}


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
        _worker_log.error(
            "job_poison id=%s retries=%d — giving up", job.id, retry_count
        )
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
        audit.emit(
            "job.complete",
            resource_id=job.id,
            resource_type=job.type,
            outcome="success",
        )
    except Exception as exc:
        job.status = JobStatus.ERROR
        job.error = str(exc)[:500]
        # Increment retry count on failure so crash-recovered jobs are bounded
        cp = (job.checkpoint_data or {}) if hasattr(job, "checkpoint_data") else {}
        cp["_retry_count"] = int(cp.get("_retry_count", 0)) + 1
        save_checkpoint(_store, job, cp)
        _worker_log.error("job_error id=%s: %s", job.id, exc)
        WORKER_JOBS_TOTAL.labels(job_type=job.type, status="error").inc()
        audit.emit(
            "job.complete",
            resource_id=job.id,
            resource_type=job.type,
            outcome="error",
            detail={"error": str(exc)[:200]},
        )
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
                _worker_log.warning(
                    "stream_ack_failed message_id=%s: %s", message_id, exc
                )


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
        _worker_log.error(
            "job_poison id=%s retries=%d — giving up", job.id, retry_count
        )
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
                    _store.notify_new_job(job.id)
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
            _store.notify_new_job(job.id)
            recovered += 1
    except Exception as exc:
        _worker_log.warning("recovery_scan_failed: %s", type(exc).__name__)
    # Re-notify any PENDING jobs whose stream message was lost.  This happens
    # when the Redis stream consumer group was created with id="$" (skips
    # pre-existing messages) or the stream key was evicted between the API's
    # XADD and the worker's XREADGROUP.  Calling notify_new_job again is
    # idempotent: the worker validates job.status == PENDING before executing,
    # so duplicate messages are ACK-ed and discarded.
    try:
        orphaned = _store.list_jobs(status="pending", limit=200)
        for job in orphaned:
            try:
                _store.notify_new_job(job.id)
                recovered += 1
                _worker_log.info("recover_pending job=%s — re-queued", job.id)
            except Exception as exc:
                _worker_log.warning(
                    "recover_pending_notify_failed job=%s: %s", job.id, exc
                )
    except Exception as exc:
        _worker_log.warning("recover_pending_scan_failed: %s", type(exc).__name__)
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
        _worker_log.info("worker_mode=event-driven max_concurrent=%d", _max_concurrent)
    else:
        _worker_log.info(
            "worker_mode=polling interval=2s max_concurrent=%d", _max_concurrent
        )

    if _staging is not None:
        asyncio.create_task(_cleanup_loop())
        _worker_log.info(
            "staging_cleanup scheduled interval_sec=%d", _STAGING_CLEANUP_INTERVAL_SEC
        )

    if _RESULT_TTL_SEC > 0:
        asyncio.create_task(_result_cleanup_loop())
        _worker_log.info(
            "result_cleanup scheduled interval_sec=%d ttl_days=%.1f",
            _RESULT_CLEANUP_INTERVAL_SEC,
            _RESULT_TTL_SEC / 86400,
        )
    else:
        _worker_log.warning(
            "result_cleanup disabled (MEDANON_RESULT_TTL_SEC=0) — NDJSON files will accumulate"
        )

    _poll_count = 0
    # Touch heartbeat file to signal readiness
    Path(_HEARTBEAT_PATH).touch()

    # Background heartbeat task — runs independently of the semaphore so that
    # the heartbeat file stays fresh even when all job slots are occupied.
    async def _heartbeat_loop():
        while not _shutdown_event.is_set():
            try:
                Path(_HEARTBEAT_PATH).touch()
            except OSError:
                pass
            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    _heartbeat_task = asyncio.create_task(_heartbeat_loop())

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
    _heartbeat_task.cancel()
    if _active_tasks:
        _worker_log.info(
            "worker_draining active_jobs=%d timeout=%ds",
            len(_active_tasks),
            _DRAIN_TIMEOUT_SEC,
        )
        _, pending = await asyncio.wait(
            _active_tasks,
            timeout=_DRAIN_TIMEOUT_SEC,
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
