"""staged_worker._core  shared constants, imports, and private helpers.

Consumed by all executor modules in this package.  Nothing in this module
is part of the public API  use ``staged_worker.__init__`` re-exports.
"""

from __future__ import annotations

import logging
import time
from utils.json_fast import dumps as _json_dumps
import os

import queue
import threading

from domain.jobs import JobStatus
from pipeline.jobs.checkpoint import save_checkpoint

_log = logging.getLogger("medanon.staged_worker")


class AmqpHandoffPending(RuntimeError):
    """Partitions were published to the broker; this process must not finalise.

    Signals that ownership of the job's remaining work has moved to the AMQP
    stage consumers.  The producer raises it instead of returning so no caller
    can proceed to merge shards and publish a result for work that has not
    happened yet  a silent empty-export release.  Job completion for the AMQP
    path is driven by the consumers advancing the workflow, not by this call.
    """


_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_BATCH_SIZE = int(os.environ.get("MEDANON_STAGING_BATCH_SIZE", "1000"))

# Parallelism for the Phase-2 re-fetch of staged references (B1). Groups by
# (source_url, resource_type) are fetched concurrently against the source FHIR
# server. Reuses MEDANON_FHIR_FETCH_PARALLEL so re-fetch and Phase-1 fetch share
# one tuning knob (default 4).
_REFETCH_PARALLEL = max(1, int(os.environ.get("MEDANON_FHIR_FETCH_PARALLEL", "4")))

# Depth of the PostgreSQL prefetch queue  mirrors MEDANON_PIPELINE_QUEUE_SIZE used by
# the non-staged executor_stream path for consistent fetch-ahead behaviour.
_PREFETCH_QUEUE_SIZE: int = int(os.environ.get("MEDANON_PIPELINE_QUEUE_SIZE", "4"))

# Number of parallel compute threads in Phase 2.
# When > 1, ``_run_staged_phase2`` runs gPAS + NLP I/O for up to N batches
# concurrently via a ThreadPoolExecutor while file writes and DB mark_done
# remain serialised on the calling (writer) thread, preserving NDJSON order.
# Recommended N \u2264 MEDANON_JOB_WORKERS to stay within the global thread budget.
#
# Default raised 1 -> 4 based on tests/perf/bench_pipeline.py --scaling: the
# match stage (FHIRPath rule_evaluation, ~99% of engine CPU) is only partially
# GIL-bound and the thread driver scales ~3x before plateauing, even with
# gPAS/NLP excluded. With real gPAS/NLP each batch also spends >50% of its time
# waiting on HTTP, so overlap reclaims that too. 4 captures most of the measured
# win while staying near MEDANON_JOB_WORKERS (default 3). For CPU-heavy jobs on
# many-core hosts, set MEDANON_STAGING_EXECUTOR=process (see B2) to exceed the
# thread plateau.
_PROCESS_WORKERS: int = max(
    1, int(os.environ.get("MEDANON_STAGING_PROCESS_WORKERS", "4"))
)

# Phase-2 executor for the shards path: "thread" (default) overlaps gPAS/NLP I/O
# and the partially-GIL-free FHIRPath work across threads in one process;
# "process" runs partition-claim workers in separate processes to bypass the GIL
# plateau entirely (measured ~3x higher absolute throughput on a 12-core box).
# Processes pay startup + per-child FHIRPath compile, so they win on large,
# CPU-heavy jobs and lose on tiny ones (see _PROCESS_MIN_PARTITIONS).
_STAGING_EXECUTOR: str = os.environ.get("MEDANON_STAGING_EXECUTOR", "thread").lower()

# Below this partition count the process executor's startup cost outweighs its
# benefit, so the shards path falls back to threads even when executor=process.
_PROCESS_MIN_PARTITIONS: int = max(
    1, int(os.environ.get("MEDANON_STAGING_PROCESS_MIN_PARTITIONS", "4"))
)

# Output mode: "stream" (default) writes a single NDJSON file; "shards" writes
# one {job_id}_p{NNNNNN}.ndjson per partition so multiple workers can claim
# and write partitions concurrently without file-level conflicts.  The join
# step concatenates shards into the final result.
_OUTPUT_MODE: str = os.environ.get("MEDANON_OUTPUT_MODE", "stream").lower()

# Infrastructure resource types excluded from auto-discovery
from domain.fhir import INFRA_RESOURCE_TYPES as _INFRA  # noqa: F401,E402  re-exported for _risk/_executors


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _checkpoint_or_cancel(
    store,
    job,
    processed: int,
    label: str,
    phase1_done: "threading.Event | None",
    phase1_state: dict | None,
    staged_count: int,
) -> bool:
    """Save a Phase-2 checkpoint and check for cancellation.

    Returns True if the job has been cancelled and the caller should stop.
    Extracted from the original inline block in :func:`_run_staged_phase2`
    so both the sequential and parallel paths share one source of truth.
    """
    fresh = store.get(job.id)
    if fresh and fresh.status == JobStatus.CANCELLED:
        _log.info("%s_cancelled job=%s at=%d", label, job.id, processed)
        return True

    if phase1_done is not None and not phase1_done.is_set():
        chk: dict = {"phase": "fetching", "processed": processed}
        if phase1_state:
            chk.update(phase1_state)
        else:
            chk["staged_count"] = staged_count
    else:
        _sc = (
            phase1_state.get("staged_count", staged_count)
            if phase1_state
            else staged_count
        )
        chk = {
            "phase": "processing",
            "staged_count": _sc,
            "processed": processed,
        }
    save_checkpoint(store, job, chk)
    return False


def _fetch_staged_resources(batch_rows: list[dict]) -> list[dict]:
    """Re-fetch FHIR resources from the source server for a batch of staging refs.

    Each staging row contains only a reference (resource_id, resource_type,
    fhir_source_url)  no patient data is stored in the staging table.  This
    function re-fetches the actual resources from the source FHIR server so
    that Phase 2 can de-identify them.

    Rows are grouped by (fhir_source_url, resource_type) for efficient batch
    fetching via ``_id`` search (one HTTP call per group, not one per resource).
    Resources that the FHIR server no longer returns (deleted, not found) are
    silently omitted.
    """
    from integrations.fhir.reader import fetch_resources_by_ids
    from integrations.staging.blob_crypto import decrypt_resource

    # A1: rows that carry an encrypted body are decrypted in-process  NO
    # re-fetch. Only rows WITHOUT a blob fall through to the FHIR re-fetch path
    # (the default refs-only behaviour). Plaintext exists only here, in memory.
    resources_from_blob: list[dict] = []
    rows_needing_refetch: list[dict] = []
    for row in batch_rows:
        blob = row.get("resource_blob")
        if blob is not None:
            try:
                resources_from_blob.append(decrypt_resource(bytes(blob)))
                continue
            except Exception as exc:
                # Fail-soft: fall back to re-fetch for this row rather than drop it.
                _log.warning("staged_blob_decrypt_failed: %s  re-fetching", exc)
        rows_needing_refetch.append(row)

    # Group remaining refs by (fhir_source_url, resource_type) for batch HTTP calls.
    groups: dict[tuple[str, str], list[str]] = {}
    for row in rows_needing_refetch:
        url = row.get("fhir_source_url", "")
        rtype = row.get("resource_type", "Unknown")
        rid = row.get("resource_id", "")
        # resource_id may be stored as "Patient/abc123" or just "abc123"
        logical_id = rid.split("/")[-1] if "/" in rid else rid
        if url and logical_id:
            groups.setdefault((url, rtype), []).append(logical_id)

    def _fetch_group(item):
        (url, rtype), ids = item
        try:
            return fetch_resources_by_ids(url, rtype, ids)
        except Exception as exc:
            # Per-group failure must not sink the batch: log and omit (same
            # fail-soft behaviour as before, now per parallel task).
            _log.warning(
                "staged_refetch_failed resource_type=%s count=%d: %s",
                rtype,
                len(ids),
                exc,
            )
            return []

    items = list(groups.items())
    # Start with bodies decrypted from staging (A1); add any re-fetched rows.
    resources: list[dict] = list(resources_from_blob)
    if not items:
        return resources
    # Parallelise across (url, type) groups  they hit disjoint FHIR endpoints
    # and `fetch_resources_by_ids` uses the shared thread-safe transport pool.
    if _REFETCH_PARALLEL <= 1 or len(items) <= 1:
        for it in items:
            resources.extend(_fetch_group(it))
        return resources

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(
        max_workers=min(_REFETCH_PARALLEL, len(items)),
        thread_name_prefix="staged-refetch",
    ) as pool:
        for fetched in pool.map(_fetch_group, items):
            resources.extend(fetched)
    return resources


def _process_batch(
    batch_rows: list[dict],
    settings,
    pseudonymizer,
    processing_mode: str,
) -> list[dict]:
    """Re-fetch and de-identify a batch of staged resource references.

    Staging rows contain only references (resource_id, resource_type,
    fhir_source_url)  no patient data.  This function re-fetches from the
    source FHIR server and delegates to
    :func:`pipeline.processor.process_data_batch` for de-identification.
    """
    from pipeline.processor import process_data_batch

    resources = _fetch_staged_resources(batch_rows)
    if not resources:
        return []

    return process_data_batch(
        resources,
        settings,
        pseudonymizer,
        attach_manifest=True,
    )


def _process_batch_with_fallback(
    batch_rows: list[dict],
    settings,
    pseudonymizer,
    processing_mode: str,
    fh,
    staging,
    job_id: str,
    label: str,
    summary=None,
) -> tuple[int, int]:
    """Re-fetch, de-identify, and write a staged batch with per-resource fallback.

    Staging rows contain only references (resource_id, resource_type,
    fhir_source_url)  no patient data.  Resources are re-fetched from the
    source FHIR server for each attempt so the fallback always operates on
    a fresh, unmutated copy from the authoritative source.

    Returns ``(succeeded, failed)`` counts.
    """
    from pipeline.processor import process_data_batch

    succeeded = 0
    failed = 0

    try:
        results = _process_batch(batch_rows, settings, pseudonymizer, processing_mode)
        done_ids: list[int] = []
        for row, result in zip(batch_rows, results):
            fh.write(_json_dumps(result) + "\n")
            done_ids.append(row["id"])
            succeeded += 1
            if summary is not None:
                summary.record_resource(result)
        fh.flush()
        staging.mark_done(job_id, done_ids)
    except Exception:
        # Per-resource fallback: re-fetch and process each resource individually.
        # Re-fetching from FHIR guarantees a clean, unmutated copy  no risk of
        # double-pseudonymization from a partially-mutated batch dict.
        for row in batch_rows:
            rtype = row.get("resource_type", "Unknown")
            try:
                resources = _fetch_staged_resources([row])
                if not resources:
                    raise ValueError(
                        f"Resource not found on FHIR server: {row.get('resource_id')}"
                    )
                result = process_data_batch(
                    resources, settings, pseudonymizer, attach_manifest=True
                )[0]
                fh.write(_json_dumps(result) + "\n")
                staging.mark_done(job_id, [row["id"]])
                succeeded += 1
                if summary is not None:
                    summary.record_resource(result)
            except Exception as exc:
                _log.error(
                    "%s job=%s resource_type=%s row_id=%d error=%s",
                    label,
                    job_id,
                    rtype,
                    row["id"],
                    exc,
                )
                fh.write(
                    _json_dumps({"error": "processing error", "resourceType": rtype})
                    + "\n"
                )
                try:
                    staging.mark_error(job_id, row["id"], str(exc))
                except Exception:
                    _log.warning(
                        "%s job=%s mark_error failed row_id=%d",
                        label,
                        job_id,
                        row["id"],
                    )
                failed += 1
                if summary is not None:
                    summary.record_error(rtype)
        fh.flush()

    return succeeded, failed


def _compute_batch_fallback_parallel(
    batch_rows: list[dict],
    settings,
    pseudonymizer,
    processing_mode: str,
    label: str,
    job_id: str,
) -> list[tuple[dict, int, bool]]:
    """Compute phase for the parallel staged-worker path.

    Re-fetches resources from the source FHIR server (staging rows contain only
    references, no patient data), runs de-identification, and returns
    ``(result_dict, row_id, succeeded)`` per row.  No file writes or DB calls.

    Thread-safe: re-fetch is stateless; de-identification is thread-local.
    """
    from pipeline.processor import process_data_batch

    try:
        results = _process_batch(batch_rows, settings, pseudonymizer, processing_mode)
        return [(result, row["id"], True) for result, row in zip(results, batch_rows)]
    except Exception:
        output: list[tuple[dict, int, bool]] = []
        for row in batch_rows:
            rtype = row.get("resource_type", "Unknown")
            try:
                resources = _fetch_staged_resources([row])
                if not resources:
                    raise ValueError(f"Resource not found: {row.get('resource_id')}")
                result = process_data_batch(
                    resources, settings, pseudonymizer, attach_manifest=True
                )[0]
                output.append((result, row["id"], True))
            except Exception as exc:
                _log.error(
                    "%s job=%s resource_type=%s row_id=%d error=%s",
                    label,
                    job_id,
                    rtype,
                    row["id"],
                    exc,
                )
                output.append(
                    (
                        {"error": "processing error", "resourceType": rtype},
                        row["id"],
                        False,
                    )
                )
        return output


def _write_computed_results(
    computed: list[tuple[dict, int, bool]],
    fh,
    staging,
    job_id: str,
    label: str,
    summary=None,
) -> tuple[int, int]:
    """Writer phase for the parallel staged-worker path.

    Serialises file writes and staging DB updates for one pre-computed batch.
    Must be called only from the single writer thread.
    Returns ``(succeeded, failed)``.
    """
    succeeded = failed = 0
    done_ids: list[int] = []

    for result, row_id, ok in computed:
        fh.write(_json_dumps(result) + "\n")
        if ok:
            done_ids.append(row_id)
            succeeded += 1
            if summary is not None:
                summary.record_resource(result)
        else:
            try:
                staging.mark_error(
                    job_id, row_id, result.get("error", "processing error")
                )
            except Exception:
                _log.warning(
                    "%s job=%s mark_error failed row_id=%d", label, job_id, row_id
                )
            failed += 1
            if summary is not None:
                summary.record_error(result.get("resourceType", "Unknown"))

    if done_ids:
        staging.mark_done(job_id, done_ids)
    fh.flush()
    return succeeded, failed


def _run_staged_phase2(
    job,
    store,
    staging,
    settings,
    pseudonymizer,
    processing_mode: str,
    output_path: str,
    open_mode: str,
    staged_count: int,
    processed: int,
    label: str,
    collector,
    staging_job_id: str | None = None,
    phase1_done: threading.Event | None = None,
    phase1_state: dict | None = None,
) -> int:
    """Run Phase 2 of any staged executor: consume pending rows → NDJSON.

    Provides a ``_PREFETCH_QUEUE_SIZE``-batch lookahead pipeline so PostgreSQL
    row fetches overlap with gPAS HTTP calls + NLP inference.

    When *phase1_done* is provided, the prefetcher polls for new rows while
    Phase 1 is still staging them  enabling Phase 1 and Phase 2 to run
    concurrently.  *phase1_state* is a dict updated in-place by the Phase 1
    thread; its cursor fields are merged into every checkpoint so crash-resume
    can restart Phase 1 from its last known position.

    ``staging_job_id`` selects which job's rows to read from the staging table.
    Defaults to ``job.id``; pass ``source_job_id`` for reprocess jobs that read
    another job's rows but write output under the current job's ID.

    Returns the total number of resources processed (succeeded + failed).
    """
    _staging_id = staging_job_id or job.id

    # D7.2 §4.4 permit-scoped pseudonymisation: bind the whole Phase-2 processing
    # to the job's data permit so gPAS domains / derived keys are scoped per
    # permit. The compute-pool path copies this context into its worker threads
    # via ``submit_with_context`` (a raw ``pool.submit`` would reset the
    # contextvar to its default in the worker thread). No-op when unset.
    from utils.permit_context import permit_scope
    from utils.thread_pool import submit_with_context

    _permit_id = (getattr(job, "params", None) or {}).get("permit_id")

    # Use the actual DB row count as the denominator.  On a crash-resume,
    # staged_count (from checkpoint) only reflects rows inserted in the
    # current session; previously-staged rows are skipped by ON CONFLICT DO
    # NOTHING and are never counted again.  count_by_status always returns
    # the true total regardless of how many sessions contributed rows.
    db_total = staging.count_by_status(_staging_id).get("total", 0)
    if db_total > staged_count:
        staged_count = db_total

    _SENTINEL = object()
    _prefetch_q: queue.Queue = queue.Queue(maxsize=_PREFETCH_QUEUE_SIZE)
    _prefetch_exc: list = []

    def _batch_prefetcher():
        try:
            while True:
                batch = staging.get_pending_batch(_staging_id, _BATCH_SIZE)
                if batch:
                    _prefetch_q.put(batch)
                else:
                    if phase1_done is None or phase1_done.is_set():
                        # Phase 1 is done (or there is no concurrent Phase 1).
                        _prefetch_q.put(_SENTINEL)
                        break
                    # Phase 1 is still running  more rows may arrive shortly.
                    time.sleep(0.1)
        except Exception as exc:
            _prefetch_exc.append(exc)
            _prefetch_q.put(_SENTINEL)

    prefetch_thread = threading.Thread(target=_batch_prefetcher, daemon=True)
    prefetch_thread.start()

    _batch_num = 0
    # When _PROCESS_WORKERS > 1, run gPAS/NLP I/O for up to N batches in
    # parallel (compute pool) while the file writes / staging.mark_done
    # remain on the calling thread (writer).  Output line order is preserved
    # because we drain futures in submission order.
    from concurrent.futures import ThreadPoolExecutor

    _compute_pool: ThreadPoolExecutor | None = (
        ThreadPoolExecutor(
            max_workers=_PROCESS_WORKERS, thread_name_prefix="staged-compute"
        )
        if _PROCESS_WORKERS > 1
        else None
    )
    try:
        with (
            permit_scope(_permit_id),
            open(output_path, open_mode, encoding="utf-8") as fh,
        ):
            if _compute_pool is None:
                # ── Sequential path (default) ──────────────────────────
                while True:
                    # Use a timeout so cancellation and checkpoints are
                    # processed even while Phase 1 is still staging rows.
                    # Without a timeout, _prefetch_q.get() blocks forever
                    # if Phase 1 is slow (134 resource types × HTTP round-
                    # trips), leaving the job at "phase=queued" indefinitely.
                    try:
                        batch_rows = _prefetch_q.get(timeout=30)
                    except Exception:
                        # Timeout: Phase 1 is still running  save a checkpoint
                        # so the UI shows "fetching" and check for cancellation.
                        if _checkpoint_or_cancel(
                            store,
                            job,
                            processed,
                            label,
                            phase1_done,
                            phase1_state,
                            staged_count,
                        ):
                            return processed
                        continue
                    if batch_rows is _SENTINEL:
                        break

                    _batch_num += 1

                    ok, bad = _process_batch_with_fallback(
                        batch_rows,
                        settings,
                        pseudonymizer,
                        processing_mode,
                        fh,
                        staging,
                        _staging_id,
                        label,
                        summary=collector,
                    )
                    processed += ok + bad

                    if _batch_num % 5 == 0:
                        if _checkpoint_or_cancel(
                            store,
                            job,
                            processed,
                            label,
                            phase1_done,
                            phase1_state,
                            staged_count,
                        ):
                            return processed
            else:
                # ── Parallel compute, sequential write ─────────────────
                from collections import deque

                inflight: deque = deque()

                def _drain_one() -> int:
                    nonlocal processed, _batch_num
                    head = inflight.popleft()
                    computed = head.result()
                    ok, bad = _write_computed_results(
                        computed,
                        fh,
                        staging,
                        _staging_id,
                        label,
                        summary=collector,
                    )
                    processed += ok + bad
                    _batch_num += 1
                    if _batch_num % 5 == 0:
                        return (
                            1
                            if _checkpoint_or_cancel(
                                store,
                                job,
                                processed,
                                label,
                                phase1_done,
                                phase1_state,
                                staged_count,
                            )
                            else 0
                        )
                    return 0

                while True:
                    batch_rows = _prefetch_q.get()
                    if batch_rows is _SENTINEL:
                        break

                    # submit_with_context  the compute worker must inherit the
                    # active permit_scope so its process_data_batch pseudonymises
                    # under the same permit (D7.2 §4.4).
                    fut = submit_with_context(
                        _compute_pool,
                        _compute_batch_fallback_parallel,
                        batch_rows,
                        settings,
                        pseudonymizer,
                        processing_mode,
                        label,
                        _staging_id,
                    )
                    inflight.append(fut)

                    if len(inflight) >= _PROCESS_WORKERS:
                        if _drain_one() == 1:
                            return processed

                # Drain the remaining in-flight batches.
                while inflight:
                    if _drain_one() == 1:
                        return processed
    finally:
        if _compute_pool is not None:
            _compute_pool.shutdown(wait=True)
        prefetch_thread.join(timeout=5.0)

    if _prefetch_exc:
        raise _prefetch_exc[0]
    return processed


def process_one_partition(
    job,
    staging,
    settings,
    pseudonymizer,
    processing_mode: str,
    output_dir: str,
    label: str,
    collector,
    resource_type: str,
    partition_id: int,
) -> tuple[int, str]:
    """De-identify ONE already-claimed partition into a shard file.

    Shared by the in-process partition loop and the RabbitMQ stage consumer so
    both paths run identical processing. The caller is responsible for the
    claim (``claim_next_partition`` or targeted ``claim_partition``) and for the
    terminal status transition (``mark_partition_done`` / ``complete_partition``
    on success; ``release_partition`` / ``record_partition_error`` on failure).

    Returns ``(resources_processed, shard_path)``. Raises on processing failure
    after unlinking the partial shard  the caller decides retry vs dead-letter.
    """
    from contextlib import suppress

    shard_path = os.path.join(output_dir, f"{job.id}_p{partition_id:06d}.ndjson")
    shard_ok = shard_bad = 0
    chunk: list[dict] = []
    try:
        with open(shard_path, "w", encoding="utf-8") as fh:
            for row in staging.iter_partition(job.id, resource_type, partition_id):
                chunk.append(row)
                if len(chunk) >= _BATCH_SIZE:
                    ok, bad = _process_batch_with_fallback(
                        chunk,
                        settings,
                        pseudonymizer,
                        processing_mode,
                        fh,
                        staging,
                        job.id,
                        label,
                        summary=collector,
                    )
                    shard_ok += ok
                    shard_bad += bad
                    chunk = []
            if chunk:
                ok, bad = _process_batch_with_fallback(
                    chunk,
                    settings,
                    pseudonymizer,
                    processing_mode,
                    fh,
                    staging,
                    job.id,
                    label,
                    summary=collector,
                )
                shard_ok += ok
                shard_bad += bad
        return shard_ok + shard_bad, shard_path
    except Exception:
        with suppress(Exception):
            os.unlink(shard_path)
        raise


def _run_staged_phase2_partition_claim(
    job,
    store,
    staging,
    settings,
    pseudonymizer,
    processing_mode: str,
    output_dir: str,
    label: str,
    collector,
) -> int:
    """Run Phase 2 using the partition-claim API (``MEDANON_OUTPUT_MODE=shards``).

    Calls ``plan_partitions`` once (idempotent).  When ``MEDANON_AMQP_URL`` is
    set, publishes one ``wf.deid`` message per partition to RabbitMQ and returns
    immediately  the stage consumers handle processing asynchronously.  Without
    AMQP, falls through to the synchronous in-process partition-claim loop
    (default, unchanged behaviour).

    This function is safe to call concurrently from multiple worker threads or
    pods: the ``FOR UPDATE SKIP LOCKED`` in ``claim_next_partition`` ensures
    each partition is processed by exactly one caller at a time.

    Returns the total number of resources processed by *this* call (0 when the
    work is handed off to AMQP consumers).
    """
    from contextlib import suppress

    partition_count = staging.plan_partitions(job.id)
    _log.info(
        "%s_partition_plan job=%s partitions=%d",
        label,
        job.id,
        partition_count,
    )
    _handed_off = False

    # ── AMQP producer path ────────────────────────────────────────────────
    # When the broker is configured, publish one work-pointer per partition
    # to the "deid" stage queue.  Stage consumers claim + process each
    # partition independently.  No PHI leaves the Postgres ledger.
    try:
        from integrations.rabbitmq.client import amqp_enabled, get_amqp_client

        if amqp_enabled() and partition_count > 0:
            broker = get_amqp_client()
            if broker is not None:
                import asyncio

                from integrations.rabbitmq.client import publish_partitions

                workflow_id = (job.params or {}).get("__workflow", {}) or {}
                if isinstance(workflow_id, dict):
                    workflow_id = workflow_id.get("workflow_id", "")

                loop = asyncio.new_event_loop()
                try:
                    published = loop.run_until_complete(
                        publish_partitions(
                            broker,
                            workflow_id=str(workflow_id),
                            job_id=job.id,
                            partition_ids=list(range(partition_count)),
                            stage="deid",
                        )
                    )
                finally:
                    loop.close()

                _log.info(
                    "%s_amqp_published job=%s partitions=%d published=%d",
                    label,
                    job.id,
                    partition_count,
                    published,
                )
                _handed_off = True
    except Exception as exc:
        # AMQP path failed  fall through to in-process loop as a safety net.
        _log.warning(
            "%s_amqp_publish_failed job=%s: %s  falling back to in-process loop",
            label,
            job.id,
            exc,
        )

    # Raised OUTSIDE the try above: the fallback handler must not swallow it and
    # start processing partitions this process no longer owns.
    #
    # Work is handed off to the stage consumers, so the shards do not exist yet
    # and this process holds no score state. Returning normally would let the
    # caller merge an empty shard set, aggregate to ``computed=False``, sail
    # through the score gate (which treats "not computed" as "nothing to check")
    # and publish an empty file as a successful export while the real work is
    # still in flight on other pods.
    if _handed_off:
        raise AmqpHandoffPending(
            f"job={job.id}: {partition_count} partition(s) published to the "
            "'deid' stage queue; completion is driven by the stage consumers, "
            "not by this process"
        )

    processed = 0
    _partitions_done = 0
    _LOG_EVERY = max(1, partition_count // 20)  # ~5% progress intervals
    while True:
        claim = staging.claim_next_partition(job.id)
        if claim is None:
            _log.info(
                "%s_partitions_exhausted job=%s processed=%d", label, job.id, processed
            )
            break

        resource_type, partition_id = claim

        # Stream the partition in _BATCH_SIZE chunks (RAM + incremental progress)
        # via the shared per-partition processor.
        try:
            part_processed, shard_path = process_one_partition(
                job,
                staging,
                settings,
                pseudonymizer,
                processing_mode,
                output_dir,
                label,
                collector,
                resource_type,
                partition_id,
            )
            staging.mark_partition_done(job.id, resource_type, partition_id)
            processed += part_processed
            _partitions_done += 1
            # Log at INFO every ~5% to give visibility without flooding.
            if _partitions_done % _LOG_EVERY == 0:
                _log.info(
                    "%s_partition_progress job=%s done=%d/%d processed=%d",
                    label,
                    job.id,
                    _partitions_done,
                    partition_count,
                    processed,
                )
            else:
                _log.debug(
                    "%s_partition_done job=%s partition=%d processed=%d",
                    label,
                    job.id,
                    partition_id,
                    part_processed,
                )
        except Exception as exc:
            _log.error(
                "%s_partition_failed job=%s partition=%d: %s",
                label,
                job.id,
                partition_id,
                exc,
                exc_info=True,
            )
            # process_one_partition already unlinked the partial shard; just
            # release the claim so a sibling/retry pod reclaims it.
            with suppress(Exception):
                staging.release_partition(job.id, resource_type, partition_id)
            raise

    return processed


def _child_bulkhead_env(parallelism: int, environ: "dict | None" = None) -> dict:
    """Per-upstream bulkhead caps for ONE child of a *parallelism*-wide pool.

    Bulkheads are per-process semaphores, so N spawned children each build their
    own and the real fleet-wide concurrency is ``N x capacity``.  Measured on a
    130,772-resource staged run with 4 workers: sustained
    ``bulkhead_saturated upstream=nlp capacity=16`` (4 x 16 = 64 in flight
    against one NLP replica) and a stream of
    ``nlp_bulkhead_saturated  returning redacted placeholder``.

    Those placeholders are fail-closed, so nothing leaks  but they are
    over-redaction: whole clinical text fields are replaced instead of scrubbed.
    Left unsized, the fleet traded OUTPUT QUALITY for parallelism, and it
    degraded further with every extra worker  precisely the knob used to make
    the job faster.

    Dividing keeps the fleet-wide in-flight total equal to the single-process
    budget, so ``MEDANON_STAGING_PROCESS_WORKERS`` can be raised for throughput
    without pushing the shared NLP/gPAS services into saturation.  Operator
    overrides are divided too  the env var means "budget for this job", not
    "budget per child".
    """
    from utils.bulkhead import _DEFAULT_CAPACITY

    src = os.environ if environ is None else environ
    workers = max(1, int(parallelism))
    out: dict[str, str] = {}
    for name, default in _DEFAULT_CAPACITY.items():
        var = f"BULKHEAD_{name.upper()}_MAX_CONCURRENT"
        try:
            total = int(src.get(var, default))
        except (TypeError, ValueError):
            total = default
        out[var] = str(max(1, total // workers))
    return out


def _partition_process_worker(
    job,
    config_profile: str,
    processing_mode: str,
    output_dir: str,
    label: str,
    staging_db_url: str,
    bulkhead_env: "dict | None" = None,
) -> "tuple[int, dict]":
    """Top-level ProcessPoolExecutor worker: drain partitions in a child process.

    Runs in a freshly ``spawn``-ed process, so it rebuilds every resource that
    holds a socket or is otherwise unsafe to inherit across the fork boundary:
    the staging store (its own DB pool), the pseudonymizer (gPAS HTTP client),
    the loaded Settings, and a private ``JobSummaryCollector``.

    Partition claims are serialised across all children by ``FOR UPDATE SKIP
    LOCKED`` in ``claim_next_partition``.

    Returns ``(resources_processed, score_state)``.  The score state MUST travel
    back: scoring is job-granular, so a private per-child collector that is
    discarded leaves the parent with nothing to aggregate, and the score gate
    treats an uncomputed aggregate as "nothing to check" rather than as a
    failure  publishing data whose k-anonymity and identifier coverage were
    never evaluated.
    """
    # MUST run before anything can touch a bulkhead: the semaphores are built
    # lazily on first use and their size is frozen at that moment, so applying
    # the per-child caps later would be a no-op. Safe to mutate os.environ here
    # because ``spawn`` gives each child a private interpreter.
    if bulkhead_env:
        os.environ.update(bulkhead_env)

    from integrations.staging.store import StagingStore
    from pipeline.config.service import get_settings
    from pipeline.jobs.summary import JobSummaryCollector
    from pipeline.processor import _get_default_pseudonymizer

    settings = get_settings(config_profile)
    pseudonymizer = _get_default_pseudonymizer()
    staging = StagingStore(staging_db_url)
    # Child workers must NOT re-run schema DDL  the parent already did, and N
    # concurrent ALTER TABLE/CREATE INDEX deadlock on staged_resources. Just
    # open the pool (+ A1 fail-closed key check).
    staging.ensure_pool()
    collector = JobSummaryCollector(
        config_profile=config_profile, settings=settings, job_id=job.id
    )
    # store is unused by the partition-claim loop (it only touches staging);
    # pass None to avoid pickling a DB-backed job store into the child.
    processed = _run_staged_phase2_partition_claim(
        job,
        None,
        staging,
        settings,
        pseudonymizer,
        processing_mode,
        output_dir,
        label,
        collector,
    )
    return processed, collector.export_score_state()


def _drain_partitions_in_processes(
    job,
    staging,
    processing_mode: str,
    output_dir: str,
    label: str,
    parallelism: int,
    collector=None,
) -> int:
    """Fan partition-claim workers across ``parallelism`` child processes.

    Uses a ``spawn`` context so children start with clean interpreter state (no
    inherited sockets/locks). Each child runs :func:`_partition_process_worker`,
    which rebuilds its own staging store + pseudonymizer + Settings and drains
    partitions until the shared ``FOR UPDATE SKIP LOCKED`` queue is exhausted.
    Returns the summed processed count.

    Each child also returns its scoring accumulators, which are merged into
    *collector*.  Without that merge the parent  the process that runs the
    score gate at publish time  observes zero scored resources, ``aggregate()``
    returns ``computed=False``, and ``check_score_gate`` short-circuits: the
    batch k-anonymity and identifier-coverage checks never run and unchecked
    data is published.
    """
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, as_completed

    config_profile = (job.params or {}).get("config_profile", "auto")
    staging_db_url = getattr(staging, "_db_url", "")
    ctx = mp.get_context("spawn")
    # Split the per-upstream bulkhead budget across the children so the fleet
    # does not oversubscribe NLP/gPAS by a factor of `parallelism`.
    bulkhead_env = _child_bulkhead_env(parallelism)
    _log.info(
        "%s_shards_process_pool job=%s workers=%d bulkheads=%s",
        label,
        job.id,
        parallelism,
        bulkhead_env,
    )
    results: list[int] = []
    with ProcessPoolExecutor(max_workers=parallelism, mp_context=ctx) as pool:
        futures = [
            pool.submit(
                _partition_process_worker,
                job,
                config_profile,
                processing_mode,
                output_dir,
                label,
                staging_db_url,
                bulkhead_env,
            )
            for _ in range(parallelism)
        ]
        for fut in as_completed(futures):
            child_processed, child_score_state = fut.result()
            results.append(child_processed)
            if collector is not None:
                collector.merge_score_state(child_score_state)
                collector.record_processed(child_processed)

    total = sum(results)
    if collector is not None and total > 0 and not collector.export_score_state():
        # Scoring is on (a collector exists) but no child returned usable state.
        # Publishing here would sail through the gate on computed=False, so fail
        # the job instead: a missing verdict must never read as a pass.
        raise RuntimeError(
            f"score state lost from {parallelism} process worker(s) after "
            f"{total} resources — refusing to publish an unscored export"
        )
    return total


def _run_staged_phase2_shards(
    job,
    store,
    staging,
    settings,
    pseudonymizer,
    processing_mode: str,
    output_dir: str,
    output_path: str,
    label: str,
    collector,
    phase1_done: "threading.Event | None" = None,
    phase1_thread: "threading.Thread | None" = None,
    phase1_exc: list | None = None,
) -> int:
    """Phase-2 dispatcher for ``MEDANON_OUTPUT_MODE=shards``.

    Waits for Phase 1 to complete (``plan_partitions`` needs all rows staged),
    runs ``_PROCESS_WORKERS`` partition-claim worker threads in parallel, then
    merges the per-partition shard files into *output_path*.

    Each worker thread claims partitions atomically via
    ``staging.claim_next_partition`` (``FOR UPDATE SKIP LOCKED``), so they
    never collide and load is naturally balanced.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # D7.2 §4.4 fail-closed: permit-scoped pseudonymisation is currently wired
    # only through the stream output mode (``_run_staged_phase2``). The shards
    # path fans work out to spawned processes / AMQP stage-consumers where the
    # permit contextvar cannot propagate, so a permit-bound job here would
    # silently produce UNSCOPED pseudonyms (reusable across permits  the exact
    # thing §4.4 forbids). Refuse rather than mis-scope: the operator must use
    # MEDANON_OUTPUT_MODE=stream for permit-bound exports until shards/AMQP
    # permit propagation lands.
    if (getattr(job, "params", None) or {}).get("permit_id"):
        raise RuntimeError(
            "permit-scoped pseudonymisation (permit_id) is not supported in "
            "MEDANON_OUTPUT_MODE=shards  the shards/AMQP path cannot propagate "
            "the permit context to its worker processes, which would produce "
            "pseudonyms reusable across permits (D7.2 §4.4). Use "
            "MEDANON_OUTPUT_MODE=stream for permit-bound exports."
        )

    # Shards mode requires all rows staged before plan_partitions can bucket them.
    if phase1_thread is not None:
        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

    parallelism = max(1, _PROCESS_WORKERS)
    _log.info(
        "%s_shards_start job=%s parallelism=%d",
        label,
        job.id,
        parallelism,
    )

    if parallelism == 1:
        processed = _run_staged_phase2_partition_claim(
            job,
            store,
            staging,
            settings,
            pseudonymizer,
            processing_mode,
            output_dir,
            label,
            collector,
        )
    else:
        # Plan once (idempotent) so all workers see the partition set.
        partition_count = staging.plan_partitions(job.id)
        _log.info(
            "%s_shards_planned job=%s partitions=%d",
            label,
            job.id,
            partition_count,
        )

        # Process executor: bypass the FHIRPath GIL plateau on CPU-heavy jobs.
        # Falls back to threads for small jobs where startup cost dominates.
        use_processes = (
            _STAGING_EXECUTOR == "process"
            and partition_count >= _PROCESS_MIN_PARTITIONS
        )
        if use_processes:
            processed = _drain_partitions_in_processes(
                job,
                staging,
                processing_mode,
                output_dir,
                label,
                parallelism,
                collector=collector,
            )
        else:
            results: list[int] = []
            with ThreadPoolExecutor(
                max_workers=parallelism, thread_name_prefix="shards-claim"
            ) as pool:
                futures = [
                    pool.submit(
                        _run_staged_phase2_partition_claim,
                        job,
                        store,
                        staging,
                        settings,
                        pseudonymizer,
                        processing_mode,
                        output_dir,
                        label,
                        collector,
                    )
                    for _ in range(parallelism)
                ]
                for fut in as_completed(futures):
                    results.append(fut.result())
            processed = sum(results)

    # Merge shards → final output_path so downstream code (store_result,
    # file_size_bytes, scoring) sees the conventional single-file output.
    merged_lines = _merge_shards(job.id, output_dir, output_path)
    _log.info(
        "%s_shards_merged job=%s lines=%d processed=%d",
        label,
        job.id,
        merged_lines,
        processed,
    )
    return processed


def _merge_shards(job_id: str, output_dir: str, output_path: str) -> int:
    """Concatenate ``{job_id}_p*.ndjson`` shards into *output_path* atomically.

    Shards are merged in ascending partition order.  Returns the number of lines
    written by *this* invocation.  Safe to call after all
    ``_run_staged_phase2_partition_claim`` callers have finished.

    Crash-safety contract  the shards are the durable intermediate:

    - The merge writes to a temporary sibling, fsyncs it, and only then
      ``os.replace``s it into place (atomic on POSIX).  ``output_path`` is
      therefore never observed partially written, and a crash mid-merge leaves
      every shard intact so the resume can re-merge from scratch.
    - Shards are unlinked only AFTER that rename succeeds.  The previous
      implementation truncated ``output_path`` up front and unlinked each shard
      as it was copied, so a crash left the already-consumed shards gone while
      their partitions were already ``done`` in the ledger  the resume
      reclaimed nothing and re-merged only the survivors, silently publishing a
      partial export.
    - **No shards + an existing output is a no-op, not a truncation.**  After a
      completed merge every partition is ``done`` and every shard is gone, so a
      crash-resume (e.g. killed during the score gate or the S3 upload) arrives
      here with zero inputs.  Truncating would replace a complete export with an
      empty file and publish it as successful.
    """
    import glob
    from contextlib import suppress

    pattern = os.path.join(output_dir, f"{job_id}_p*.ndjson")
    shard_files = sorted(glob.glob(pattern))

    if not shard_files:
        if os.path.exists(output_path):
            _log.info(
                "merge_shards_noop job=%s  no shards remain, preserving existing "
                "output (crash-resume after a completed merge)",
                job_id,
            )
            return 0
        # Genuinely empty result: downstream (store_result, os.path.getsize,
        # the manifest split) requires the file to exist.
        with open(output_path, "w", encoding="utf-8"):
            pass
        return 0

    tmp_path = f"{output_path}.merge-{os.getpid()}.tmp"
    total_lines = 0
    try:
        with open(tmp_path, "w", encoding="utf-8") as out:
            for shard in shard_files:
                with open(shard, encoding="utf-8") as fh:
                    for line in fh:
                        out.write(line)
                        total_lines += 1
            # Durability before the rename: without the fsync the rename can be
            # ordered ahead of the data on a crash, yielding a zero-length or
            # truncated file at the final path.
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_path, output_path)
    except BaseException:
        # Leave every shard in place so the merge is retryable, and never leak
        # a partial temp file into the glob namespace of a later attempt.
        with suppress(Exception):
            os.unlink(tmp_path)
        raise

    for shard in shard_files:
        with suppress(Exception):
            os.unlink(shard)
    return total_lines


# ---------------------------------------------------------------------------
# Internal: scoring persistence helper
# ---------------------------------------------------------------------------


def _persist_scoring_run(
    job, profile: str, summary_dict: dict, endpoint: str, collector=None
) -> None:
    """Write a processing_run row for a completed staged job. Silent on failure.

    Uses ``summary_dict["duration_sec"]`` (populated by
    ``JobSummaryCollector.to_dict()``) to avoid needing a separate t0 variable
    in every executor.  ``job.id`` is reused as the run_id so the row can be
    correlated with the async job record.

    When *collector* is supplied its job-detail payload is written too, so the
    Jobs UI can render counts/PII/fields without downloading the NDJSON result.
    The staged path is the one that actually runs for patient exports, so
    omitting the collector here would leave the UI with no detail to read.
    The collector's Markdown audit report rides along inside ``score`` so it
    lands in the DB: ``/output`` is reaped on MEDANON_RESULT_TTL_SEC, and the
    audit record has to outlive the data it describes.
    """
    if collector is not None:
        try:
            from pipeline.jobs.detail import save_job_detail

            save_job_detail(job.id, collector.detail_dict())
        except Exception:
            _log.debug("staged_job_detail_failed job=%s", job.id, exc_info=True)
    try:
        from pipeline.scoring_helpers import attach_audit_report, persist_run_sync

        persist_run_sync(
            endpoint=endpoint,
            config_profile=profile,
            resource_count=summary_dict.get("total_resources", 0),
            error_count=summary_dict.get("error_count", 0),
            duration_ms=int(summary_dict.get("duration_sec", 0) * 1000),
            input_type="ndjson",
            summary=summary_dict,
            score=attach_audit_report(summary_dict.get("score"), collector),
            run_id=job.id,
        )
    except Exception:
        _log.debug(
            "staged_persist_run_failed job=%s endpoint=%s",
            job.id,
            endpoint,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Public: two-phase executors
# ---------------------------------------------------------------------------
