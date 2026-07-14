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
import re
import signal
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from domain.jobs import Job, JobStatus
from pipeline.jobs.checkpoint import save_checkpoint
from utils import audit
from utils.tracing import get_tracer as _get_tracer


def _tracer():
    return _get_tracer("medanon.worker")


_worker_log = logging.getLogger("medanon.worker")

# Patterns that may contain PHI in exception messages.
# UUIDs appear in FHIR resource IDs; ISO dates appear in generalization/perturb errors.
_PHI_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_PHI_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}[^\s]*)?\b")
_PHI_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHI_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-.]?)?\d{3,4}[\s\-.]?\d{4}(?!\d)"
)
_PHI_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_PHI_MRN_RE = re.compile(r"\b(?:MRN|mrn)[:\s#]?\d{4,}\b")


def _sanitize_error(exc: BaseException, max_len: int = 400) -> str:
    """Return a PHI-scrubbed, truncated string representation of *exc*.

    Replaces UUID-like strings (FHIR resource IDs), ISO date strings, email
    addresses, phone numbers, SSNs, and MRNs with placeholders so they are
    never persisted in the job error field or audit log.  Increments
    ``medanon_error_phi_scrubbed_total`` whenever a substitution actually fired
    so operators can detect handlers that leak PHI into exception messages.
    """
    raw = str(exc)
    text = _PHI_UUID_RE.sub("[ID]", raw)
    text = _PHI_DATE_RE.sub("[DATE]", text)
    text = _PHI_EMAIL_RE.sub("[EMAIL]", text)
    text = _PHI_PHONE_RE.sub("[PHONE]", text)
    text = _PHI_SSN_RE.sub("[SSN]", text)
    text = _PHI_MRN_RE.sub("[MRN]", text)
    if text != raw:
        try:
            from utils.metrics import ERROR_PHI_SCRUBBED

            ERROR_PHI_SCRUBBED.inc()
        except Exception:
            pass
    return text[:max_len]


_store = None
_staging = None  # StagingStore instance; set by init_staging() when MEDANON_STAGING_DB_URL is configured
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_HEARTBEAT_PATH = os.path.join(
    os.environ.get("MEDANON_OUTPUT_DIR", "/output"), "worker_healthy"
)


def _touch_heartbeat() -> None:
    """Refresh the liveness file. Never raises.

    The heartbeat is an *observability* signal. A failure to write it says
    nothing about whether this worker can process jobs, so it must never abort
    the consume loop -- staleness of the file is itself the signal, and
    ``worker_health`` surfaces it on ``/ready``.

    Two call sites already did this correctly (startup and ``_heartbeat_loop``);
    the two inside ``worker_loop`` did not. With ``/output`` bind-mounted from a
    host directory the container's uid cannot write, every iteration raised
    ``PermissionError`` here. In the idle branch that double-released the
    semaphore; in the dispatch branch it stranded a message that had already
    been claimed from the Redis stream, leaving it in the consumer group's
    pending list forever. The queue drained into the PEL and no job ever ran.
    """
    try:
        Path(_HEARTBEAT_PATH).touch()
    except OSError as exc:
        _worker_log.warning("heartbeat_touch_failed path=%s: %s", _HEARTBEAT_PATH, exc)


_max_concurrent: int = 3
_semaphore: asyncio.Semaphore | None = None
_shutdown_event: asyncio.Event | None = None
_active_tasks: set = set()
# Redis Stream message-ids for jobs currently executing on THIS worker. The
# claim-heartbeat loop refreshes their idle timers so long-running jobs are not
# reclaimed as stale (by the periodic loop or a sibling's startup recovery).
_inflight_messages: set[str] = set()
# How often to refresh in-flight claims. Must be comfortably below the stale
# timeout so a running job's idle timer never crosses it.
_CLAIM_HEARTBEAT_SEC: int = int(os.environ.get("MEDANON_CLAIM_HEARTBEAT_SEC", "30"))
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

from pipeline.jobs.checkpoint import _truncate_to_lines  # noqa: E402,F401  (re-export)
from pipeline.jobs.executors import (  # noqa: E402
    _execute_batch_patient_export,
    _execute_bulk_export,
    _execute_bulk_import,
    _execute_cohort,
    _execute_patient_export,
    _execute_reprocess,
    _execute_risk_driven_export,
)
from pipeline.jobs.executor_tabular import (  # noqa: E402
    execute_tabular_batch as _execute_tabular_batch,
)
from pipeline.jobs.executor_sql import execute_sql_export as _execute_sql_export  # noqa: E402


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
    "batch-patient-export": lambda job: _execute_batch_patient_export(
        job, _store, _staging
    ),
    "risk-driven-export": lambda job: _execute_risk_driven_export(
        job, _store, _staging
    ),
    "tabular-batch": lambda job: _execute_tabular_batch(job, _store, _staging),
    "sql-export": lambda job: _execute_sql_export(job, _store, _staging),
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


# ---------------------------------------------------------------------------
# Stale `processing` row recovery (PR #7)
# ---------------------------------------------------------------------------

_STALE_RECOVERY_INTERVAL_SEC: int = int(
    os.environ.get("MEDANON_STALE_RECOVERY_INTERVAL_SEC", "300")
)
_STALE_RECOVERY_TIMEOUT_MIN: int = int(
    os.environ.get("MEDANON_STALE_RECOVERY_TIMEOUT_MIN", "10")
)


async def _claim_heartbeat_loop() -> None:
    """Refresh Redis Stream claims for jobs running on THIS worker.

    Every ``_CLAIM_HEARTBEAT_SEC`` seconds, re-claims each in-flight message to
    self (``refresh_claim`` → XCLAIM min_idle=0), resetting its idle timer. This
    keeps a genuinely-running long job from ever crossing the stale-recovery
    timeout, so neither the periodic loop nor a sibling's startup recovery
    reclaims and re-runs it. No-op for non-Redis stores (no ``refresh_claim``).
    """
    if _CLAIM_HEARTBEAT_SEC <= 0 or _store is None:
        return
    if not hasattr(_store, "refresh_claim"):
        return  # SQLite / Postgres stores don't use stream claims
    while True:
        await asyncio.sleep(_CLAIM_HEARTBEAT_SEC)
        if _shutdown_event is not None and _shutdown_event.is_set():
            return
        # Snapshot to avoid mutation during iteration.
        for message_id in list(_inflight_messages):
            try:
                await asyncio.to_thread(_store.refresh_claim, message_id)
            except Exception as exc:
                _worker_log.debug(
                    "claim_heartbeat_failed message_id=%s: %s", message_id, exc
                )


async def _stale_recovery_loop() -> None:
    """Periodically reclaim staged rows and partitions stuck in-progress.

    Two recovery paths:
    1. Rows stuck in ``processing`` — a worker that crashes between
       ``get_pending_batch`` and ``mark_done`` leaves rows invisible to all
       other workers.  ``recover_stale_processing`` resets them to ``pending``.
    2. Partitions stuck in ``claimed`` — a worker that crashes mid-shard
       without reaching its ``release_partition`` exception handler leaves the
       partition locked forever.  ``recover_stale_partitions`` resets those to
       ``unclaimed`` so another worker or Argo retry pod can reclaim them.

    Disabled when staging is not configured or the interval is ``<= 0``.
    """
    if _STALE_RECOVERY_INTERVAL_SEC <= 0:
        return
    while True:
        await asyncio.sleep(_STALE_RECOVERY_INTERVAL_SEC)
        if _staging is None:
            continue
        try:
            recovered = await asyncio.to_thread(
                _staging.recover_stale_processing,
                timeout_minutes=_STALE_RECOVERY_TIMEOUT_MIN,
            )
            if recovered:
                _worker_log.warning(
                    "stale_recovery reclaimed=%d timeout_min=%d "
                    "(likely a worker crash mid-batch)",
                    recovered,
                    _STALE_RECOVERY_TIMEOUT_MIN,
                )
        except Exception as exc:
            _worker_log.warning("stale_recovery_error: %s", type(exc).__name__)
        try:
            recovered_parts = await asyncio.to_thread(
                _staging.recover_stale_partitions,
                timeout_minutes=_STALE_RECOVERY_TIMEOUT_MIN,
            )
            if recovered_parts:
                _worker_log.warning(
                    "stale_partition_recovery reclaimed=%d timeout_min=%d "
                    "(likely a worker crash mid-shard)",
                    recovered_parts,
                    _STALE_RECOVERY_TIMEOUT_MIN,
                )
        except Exception as exc:
            _worker_log.warning(
                "stale_partition_recovery_error: %s", type(exc).__name__
            )


_WORKFLOW_RECONCILE_INTERVAL_SEC: int = int(
    os.environ.get("MEDANON_WORKFLOW_RECONCILE_INTERVAL_SEC", "30")
)


async def _workflow_reconcile_loop() -> None:
    """Periodically re-derive workflow step readiness (lost-enqueue recovery).

    Idempotent thanks to the engine's CAS guards. No-op when the workflow
    engine isn't configured or the interval is ``<= 0``.
    """
    if _WORKFLOW_RECONCILE_INTERVAL_SEC <= 0:
        return
    from pipeline.workflows import get_workflow_engine

    while True:
        await asyncio.sleep(_WORKFLOW_RECONCILE_INTERVAL_SEC)
        engine = get_workflow_engine()
        if engine is None:
            continue
        try:
            await asyncio.to_thread(engine.reconcile)
        except Exception as exc:
            _worker_log.warning("workflow_reconcile_error: %s", type(exc).__name__)


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
    # Keyset cursor: ISO string of the last job's created_at seen in the previous
    # page.  Avoids O(n) offset scans that grow with the job table size.
    cursor: str | None = None
    while True:
        jobs = _store.list_jobs(
            status="done", limit=200, offset=0, before_created_at=cursor
        )
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
        # Advance cursor to the created_at of the oldest job in this page.
        # Results are ordered newest-first so the last element is the oldest.
        cursor = jobs[-1].created_at
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


_REDIS_INDEX_SWEEP_INTERVAL_SEC = int(
    os.environ.get("MEDANON_REDIS_INDEX_SWEEP_SEC", "3600")
)


async def _redis_index_sweep_loop() -> None:
    """Periodically remove orphan IDs from the Redis job index sorted set.

    Job hashes have a TTL (default 7 days) but the time-ordered sorted set
    ``medanon:jobs_by_time`` and the ``medanon:jobs:status:*`` /
    ``medanon:jobs:type:*`` index sets do not. Without this sweep, expired
    job IDs remain in those indexes forever.

    Also prunes dead stream consumers: every worker restart mints a new
    ``worker-<uuid>`` and never removes the old one, so the consumer group's
    membership list grows without bound.

    No-op when the store backend is not Redis.
    """
    while True:
        await asyncio.sleep(_REDIS_INDEX_SWEEP_INTERVAL_SEC)
        if _store is None:
            continue
        if hasattr(_store, "cleanup_orphan_index"):
            try:
                removed = await asyncio.to_thread(_store.cleanup_orphan_index)
                if removed:
                    _worker_log.info("redis_index_sweep removed=%d", removed)
            except Exception as exc:
                _worker_log.warning("redis_index_sweep_error: %s", type(exc).__name__)
        if hasattr(_store, "prune_dead_consumers"):
            try:
                pruned = await asyncio.to_thread(_store.prune_dead_consumers)
                if pruned:
                    _worker_log.info("dead_consumer_prune removed=%d", pruned)
            except Exception as exc:
                _worker_log.warning("dead_consumer_prune_error: %s", type(exc).__name__)


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

    # Re-fetch current state before starting: an external CANCELLED (or a
    # delete) between enqueue and execution must not be overwritten with
    # RUNNING — the stale queue snapshot would clobber the cancel and the
    # executor would run anyway. The refreshed record also carries the latest
    # retry count for the poison-job check below.
    try:
        refreshed = _store.get(job.id)
    except Exception as exc:
        # Store blip — proceed with the snapshot rather than dropping the job.
        _worker_log.warning("job_refresh_failed id=%s: %s", job.id, type(exc).__name__)
        refreshed = job
    if refreshed is None:
        _worker_log.info("job_deleted_before_start id=%s", job.id)
        return
    if refreshed.status == JobStatus.CANCELLED:
        from utils.metrics import WORKER_JOBS_TOTAL

        _worker_log.info("job_cancelled_before_start id=%s", job.id)
        WORKER_JOBS_TOTAL.labels(job_type=job.type, status="cancelled").inc()
        return
    job = refreshed

    # Poison job protection: check retry count from checkpoint data.
    # _retry_count is incremented only after a failed attempt (in the except
    # block below), so checking >= here gives exactly _MAX_JOB_RETRIES attempts.
    cp = (job.checkpoint_data or {}) if hasattr(job, "checkpoint_data") else {}
    retry_count = int(cp.get("_retry_count", 0))
    if retry_count >= _MAX_JOB_RETRIES:
        _route_to_dlq(job, retry_count)
        return

    job.status = JobStatus.RUNNING
    _store.update(job)
    _worker_log.info("job_start id=%s type=%s retry=%d", job.id, job.type, retry_count)
    import time as _time
    from utils.metrics import WORKER_JOBS_TOTAL, WORKER_JOB_DURATION, WORKER_ACTIVE_JOBS

    WORKER_ACTIVE_JOBS.inc()
    _t0 = _time.monotonic()
    _job_span = _tracer().start_as_current_span("job.execute")
    _span = _job_span.__enter__()
    try:
        _span.set_attribute("job.id", job.id)
        _span.set_attribute("job.type", job.type)
        _span.set_attribute("job.retry", retry_count)
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
        from pipeline.scoring.gate import ScoreGateBlocked

        gate_blocked = isinstance(exc, ScoreGateBlocked)

        job.status = JobStatus.ERROR
        job.error = str(exc) if gate_blocked else _sanitize_error(exc)

        if gate_blocked:
            # Quality gate block — output is intentionally deleted.  Do NOT
            # increment the retry counter: the input data hasn't changed, so
            # re-running without config changes would produce the same result.
            # Persist the structured block report so the job record can show the
            # UI *what leaked and what to fix* (download stays blocked, output
            # stays deleted — only the explanation is now machine-readable).
            report = getattr(exc, "report", None)
            if report:
                cp = (
                    (job.checkpoint_data or {})
                    if hasattr(job, "checkpoint_data")
                    else {}
                )
                cp["block_report"] = report
                save_checkpoint(_store, job, cp)
            _worker_log.warning("job_score_gate_blocked id=%s", job.id)
            WORKER_JOBS_TOTAL.labels(job_type=job.type, status="blocked").inc()
            audit.emit(
                "job.complete",
                resource_id=job.id,
                resource_type=job.type,
                outcome="blocked",
                detail={"reason": "score_gate"},
            )
        else:
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
                detail={"error": _sanitize_error(exc, max_len=200)},
            )
    finally:
        _job_span.__exit__(None, None, None)
        WORKER_JOB_DURATION.labels(job_type=job.type).observe(_time.monotonic() - _t0)
        WORKER_ACTIVE_JOBS.dec()
        # Always persist terminal status — even if the success/error handler above
        # raises, the job must not remain stuck in RUNNING indefinitely.
        try:
            _store.update(job)
        except Exception as update_exc:
            _worker_log.error("job_status_update_failed id=%s: %s", job.id, update_exc)
        # Workflow DAG hook: advance the owning workflow (no-op for non-workflow
        # jobs). Isolated so a workflow-store hiccup never fails the job.
        _notify_workflow_terminal(job)


async def _run_and_release(job: Job, message_id: str | None = None) -> None:
    """Run a job, release the semaphore, and ACK the stream message when done."""
    task = asyncio.current_task()
    _active_tasks.add(task)
    # Track the message so the claim-heartbeat keeps its idle timer fresh while
    # this job runs (prevents stale-reclaim → double-run of long jobs).
    if message_id:
        _inflight_messages.add(message_id)
    try:
        await _run_job(job)
    finally:
        _active_tasks.discard(task)
        if message_id:
            _inflight_messages.discard(message_id)
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


# Dead Letter Queue stream name; consumers (alerting, manual triage tooling)
# can XREAD this stream independently of the regular job queue.
_DLQ_STREAM = "medanon:jobs:dlq"
_DLQ_STREAM_MAXLEN = int(os.environ.get("MEDANON_DLQ_STREAM_MAXLEN", "10000"))


def _emit_dlq_stream(job, retry_count: int) -> None:
    """Best-effort: append a poison-job marker to the DLQ Redis Stream.

    Silent on Redis unavailability — the job is already persisted with
    ``status=DEAD`` in the job store, so the stream is purely an
    observability aid.
    """
    try:
        from utils.audit import _get_redis  # type: ignore[attr-defined]

        r = _get_redis()
        if r is None:
            return
        r.xadd(
            _DLQ_STREAM,
            {
                "job_id": job.id,
                "type": job.type,
                "retry_count": str(retry_count),
                "error": (job.error or "")[:200],
            },
            maxlen=_DLQ_STREAM_MAXLEN,
            approximate=True,
        )
    except Exception:
        # Never let DLQ instrumentation block the worker.
        _worker_log.debug("dlq_stream_emit_failed id=%s", job.id, exc_info=True)


def _route_to_dlq(job, retry_count: int) -> None:
    """Mark *job* as DEAD, persist, audit, and append to the DLQ stream.

    Called from poison-detection branches in ``_run_job`` and
    ``_check_retry_limit`` so the routing logic stays in one place.
    """
    job.status = JobStatus.DEAD
    job.error = f"Exceeded max retries ({_MAX_JOB_RETRIES})"
    if _store is not None:
        _store.update(job)
    _worker_log.error(
        "job_poison id=%s retries=%d type=%s — routed to DLQ",
        job.id,
        retry_count,
        job.type,
    )
    audit.emit(
        "job.poison",
        resource_id=job.id,
        resource_type=job.type,
        outcome="error",
        detail={"retry_count": retry_count, "max_retries": _MAX_JOB_RETRIES},
    )
    _emit_dlq_stream(job, retry_count)
    # A DEAD step also fails its owning workflow.
    _notify_workflow_terminal(job)


def _notify_workflow_terminal(job) -> None:
    """Advance the owning workflow (no-op for non-workflow jobs).

    Isolated + fail-soft: a workflow-store error must never affect the job's
    own terminal handling.
    """
    try:
        from pipeline.workflows import get_workflow_engine

        engine = get_workflow_engine()
        if engine is not None:
            engine.on_job_terminal(job)
    except Exception as exc:
        _worker_log.warning("workflow_terminal_hook_failed id=%s: %s", job.id, exc)


def _check_retry_limit(job) -> bool:
    """Check if a job has exceeded its retry limit. Returns True if poisoned (should not retry)."""
    cp = (job.checkpoint_data or {}) if hasattr(job, "checkpoint_data") else {}
    retry_count = int(cp.get("_retry_count", 0))
    if retry_count >= _MAX_JOB_RETRIES:
        _route_to_dlq(job, retry_count)
        return True
    return False


def _recover_running_jobs() -> int:
    """Reset abandoned RUNNING jobs back to PENDING after a crash.

    For Redis Streams, reclaims only PEL messages idle longer than the stale
    timeout. We must NOT use ``min_idle_ms=0`` here: this worker may be ONE of
    several replicas (``--scale worker=N`` / HPA), and a freshly-started worker
    grabbing every in-flight message would steal jobs actively running on
    siblings — re-queuing them and causing concurrent double-runs (observed: a
    long staged export reclaimed by a late-booting worker → racing shard writes
    → empty output). Messages idle < the timeout are presumed alive elsewhere
    and are left to their owner (whose heartbeat keeps refreshing the claim).

    Also scans the job-status index for any RUNNING jobs not covered by the
    stream (e.g. migrated from BLPOP).

    Jobs that have exceeded ``_MAX_JOB_RETRIES`` are moved to ERROR instead.

    Returns the count of recovered jobs.
    """
    if _store is None or not hasattr(_store, "list_jobs"):
        return 0
    recovered = 0
    # Redis Streams: at startup, reclaim only messages idle past the stale
    # timeout (genuinely abandoned), never min_idle_ms=0 (would steal siblings'
    # running jobs).
    _startup_min_idle_ms = max(1, _STALE_RECOVERY_TIMEOUT_MIN) * 60_000
    if hasattr(_store, "claim_stale_jobs"):
        try:
            stale_pairs = _store.claim_stale_jobs(min_idle_ms=_startup_min_idle_ms)
            _TERMINAL = frozenset(
                {
                    JobStatus.DONE,
                    JobStatus.ERROR,
                    JobStatus.CANCELLED,
                    JobStatus.DEAD,
                }
            )
            for job_id, message_id in stale_pairs:
                job = _store.get(job_id)
                if job is None or job.status in _TERMINAL:
                    # Already finished — the previous worker ACK'd the job record
                    # but crashed before ACKing the stream message. ACK now so this
                    # entry doesn't accumulate in the PEL across every restart.
                    try:
                        _store.ack_job(message_id)
                        _worker_log.debug(
                            "recovery_ack_terminal job=%s msg=%s status=%s",
                            job_id,
                            message_id,
                            job.status if job else "missing",
                        )
                    except Exception as ack_exc:
                        _worker_log.warning(
                            "recovery_ack_terminal_failed job=%s msg=%s: %s",
                            job_id,
                            message_id,
                            ack_exc,
                        )
                    continue
                if job.status == JobStatus.RUNNING:
                    if _check_retry_limit(job):
                        continue
                    job.status = JobStatus.PENDING
                    job.error = None
                    _store.update(job)
                    _store.notify_new_job(job.id)
                    recovered += 1
                # PENDING jobs are handled by notify_new_job below; no ACK here.
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
        from utils.tasks import retain_task

        retain_task(_cleanup_loop(), name="staging_cleanup")
        _worker_log.info(
            "staging_cleanup scheduled interval_sec=%d", _STAGING_CLEANUP_INTERVAL_SEC
        )
        # PR #7: Periodic stale-`processing` row recovery.  Mandatory for
        # multi-worker safety — without it, a crashed worker leaves staged
        # rows stuck in ``processing`` until manual intervention.
        if _STALE_RECOVERY_INTERVAL_SEC > 0:
            retain_task(_stale_recovery_loop(), name="stale_recovery")
            _worker_log.info(
                "stale_recovery scheduled interval_sec=%d timeout_min=%d",
                _STALE_RECOVERY_INTERVAL_SEC,
                _STALE_RECOVERY_TIMEOUT_MIN,
            )

    if _RESULT_TTL_SEC > 0:
        from utils.tasks import retain_task

        retain_task(_result_cleanup_loop(), name="result_cleanup")
        _worker_log.info(
            "result_cleanup scheduled interval_sec=%d ttl_days=%.1f",
            _RESULT_CLEANUP_INTERVAL_SEC,
            _RESULT_TTL_SEC / 86400,
        )
    else:
        _worker_log.warning(
            "result_cleanup disabled (MEDANON_RESULT_TTL_SEC=0) — NDJSON files will accumulate"
        )

    # Claim heartbeat — refresh in-flight stream claims so long jobs aren't
    # reclaimed as stale (no-op for non-Redis backends).
    if _store is not None and hasattr(_store, "refresh_claim"):
        from utils.tasks import retain_task

        retain_task(_claim_heartbeat_loop(), name="claim_heartbeat")
        _worker_log.info(
            "claim_heartbeat scheduled interval_sec=%d", _CLAIM_HEARTBEAT_SEC
        )

    # Periodic Redis index orphan sweep — no-op for non-Redis backends.
    if _store is not None and hasattr(_store, "cleanup_orphan_index"):
        from utils.tasks import retain_task

        retain_task(_redis_index_sweep_loop(), name="redis_index_sweep")
        _worker_log.info(
            "redis_index_sweep scheduled interval_sec=%d",
            _REDIS_INDEX_SWEEP_INTERVAL_SEC,
        )

    # Workflow reconciliation sweep — no-op when the engine isn't configured.
    if _WORKFLOW_RECONCILE_INTERVAL_SEC > 0:
        from pipeline.workflows import get_workflow_engine

        if get_workflow_engine() is not None:
            from utils.tasks import retain_task

            retain_task(_workflow_reconcile_loop(), name="workflow_reconcile")
            _worker_log.info(
                "workflow_reconcile scheduled interval_sec=%d",
                _WORKFLOW_RECONCILE_INTERVAL_SEC,
            )

    _poll_count = 0
    _touch_heartbeat()

    # Background heartbeat task — runs independently of the semaphore so that
    # the heartbeat file stays fresh even when all job slots are occupied.
    async def _heartbeat_loop():
        while not _shutdown_event.is_set():
            # Degraded state is logged, never raised: heartbeat staleness is
            # itself the signal (e.g. /output read-only or owned by another uid).
            _touch_heartbeat()
            try:
                await asyncio.wait_for(_shutdown_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    from utils.tasks import retain_task as _retain_task

    _heartbeat_task = _retain_task(_heartbeat_loop(), name="worker_heartbeat")

    while not _shutdown_event.is_set():
        await _semaphore.acquire()
        if _shutdown_event.is_set():
            _semaphore.release()
            break
        # Ownership of the permit acquired above. Once a job is dispatched,
        # ``_run_and_release`` releases it in its ``finally``; the error handler
        # below must not release it a second time. ``asyncio.Semaphore.release()``
        # has no owner check, so a double release silently raises the permit
        # count above ``_max_concurrent`` and stops bounding concurrency.
        released = False
        try:
            job, message_id = await _get_next_job(is_event_driven)
            if job is None:
                _semaphore.release()
                released = True
                _touch_heartbeat()
                if not is_event_driven:
                    # Use wait with timeout so shutdown signal can interrupt sleep
                    try:
                        await asyncio.wait_for(_shutdown_event.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
                continue

            # Dispatch FIRST. ``_get_next_job`` has already claimed the message
            # from the Redis consumer group, and anything that raises between the
            # claim and the dispatch strands it in the pending-entries list: no
            # task runs it, nothing ACKs it, and only a worker restart past the
            # stale-recovery timeout reclaims it. The heartbeat comes after, and
            # cannot raise.
            asyncio.create_task(_run_and_release(job, message_id))
            released = True  # the task owns the permit now
            _touch_heartbeat()

            # Periodically update the queue depth metric (every 50 jobs)
            _poll_count += 1
            if _poll_count % 50 == 0 and hasattr(_store, "get_queue_depth"):
                try:
                    from utils.metrics import JOB_QUEUE_DEPTH

                    JOB_QUEUE_DEPTH.set(_store.get_queue_depth())
                except Exception:
                    pass
        except Exception as exc:
            if not released:
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
