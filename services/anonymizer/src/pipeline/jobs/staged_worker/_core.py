"""staged_worker._core — shared constants, imports, and private helpers.

Consumed by all executor modules in this package.  Nothing in this module
is part of the public API — use ``staged_worker.__init__`` re-exports.
"""

from __future__ import annotations

import logging
import time
from utils.json_fast import loads as _json_loads, dumps as _json_dumps
import os
from pathlib import Path

import queue
import threading

from domain.jobs import JobStatus
from pipeline.jobs.checkpoint import load_checkpoint, save_checkpoint, _truncate_to_lines
from integrations.storage import store_result

_log = logging.getLogger("medanon.staged_worker")

_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_BATCH_SIZE = int(os.environ.get("MEDANON_STAGING_BATCH_SIZE", "1000"))

# Depth of the PostgreSQL prefetch queue — mirrors MEDANON_PIPELINE_QUEUE_SIZE used by
# the non-staged executor_stream path for consistent fetch-ahead behaviour.
_PREFETCH_QUEUE_SIZE: int = int(os.environ.get("MEDANON_PIPELINE_QUEUE_SIZE", "4"))

# Number of parallel compute threads in Phase 2 (default 1 = single-threaded).
# When > 1, ``_run_staged_phase2`` runs gPAS + NLP I/O for up to N batches
# concurrently via a ThreadPoolExecutor while file writes and DB mark_done
# remain serialised on the calling (writer) thread, preserving NDJSON order.
# Recommended N \u2264 MEDANON_JOB_WORKERS to stay within the global thread budget.
# Counter-intuitively this gives the biggest single-process throughput win:
# Pass 1 alone is GIL-bound, but each batch spends >50% of its time waiting
# on gPAS/NLP HTTP, so overlapping multiple batches reclaims that time.
_PROCESS_WORKERS: int = max(1, int(os.environ.get("MEDANON_STAGING_PROCESS_WORKERS", "1")))

# Output mode: "stream" (default) writes a single NDJSON file; "shards" writes
# one {job_id}_p{NNNNNN}.ndjson per partition so multiple workers can claim
# and write partitions concurrently without file-level conflicts.  The join
# step concatenates shards into the final result.
_OUTPUT_MODE: str = os.environ.get("MEDANON_OUTPUT_MODE", "stream").lower()

# Infrastructure resource types excluded from auto-discovery
_INFRA = frozenset(
    {
        "CapabilityStatement",
        "OperationDefinition",
        "SearchParameter",
        "StructureDefinition",
        "CompartmentDefinition",
        "ImplementationGuide",
        "CodeSystem",
        "ValueSet",
        "ConceptMap",
        "NamingSystem",
        "OperationOutcome",
        "Bundle",
    }
)


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
    fhir_source_url) — no patient data is stored in the staging table.  This
    function re-fetches the actual resources from the source FHIR server so
    that Phase 2 can de-identify them.

    Rows are grouped by (fhir_source_url, resource_type) for efficient batch
    fetching via ``_id`` search (one HTTP call per group, not one per resource).
    Resources that the FHIR server no longer returns (deleted, not found) are
    silently omitted.
    """
    from integrations.fhir.reader import fetch_resources_by_ids

    # Group refs by (fhir_source_url, resource_type) for batch HTTP calls.
    groups: dict[tuple[str, str], list[str]] = {}
    for row in batch_rows:
        url = row.get("fhir_source_url", "")
        rtype = row.get("resource_type", "Unknown")
        rid = row.get("resource_id", "")
        # resource_id may be stored as "Patient/abc123" or just "abc123"
        logical_id = rid.split("/")[-1] if "/" in rid else rid
        if url and logical_id:
            groups.setdefault((url, rtype), []).append(logical_id)

    resources: list[dict] = []
    for (url, rtype), ids in groups.items():
        try:
            fetched = fetch_resources_by_ids(url, rtype, ids)
            resources.extend(fetched)
        except Exception as exc:
            _log.warning(
                "staged_refetch_failed resource_type=%s count=%d: %s",
                rtype, len(ids), exc,
            )
    return resources


def _process_batch(
    batch_rows: list[dict],
    settings,
    pseudonymizer,
    processing_mode: str,
) -> list[dict]:
    """Re-fetch and de-identify a batch of staged resource references.

    Staging rows contain only references (resource_id, resource_type,
    fhir_source_url) — no patient data.  This function re-fetches from the
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
    fhir_source_url) — no patient data.  Resources are re-fetched from the
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
        # Re-fetching from FHIR guarantees a clean, unmutated copy — no risk of
        # double-pseudonymization from a partially-mutated batch dict.
        for row in batch_rows:
            rtype = row.get("resource_type", "Unknown")
            try:
                resources = _fetch_staged_resources([row])
                if not resources:
                    raise ValueError(f"Resource not found on FHIR server: {row.get('resource_id')}")
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
                    label, job_id, rtype, row["id"], exc,
                )
                fh.write(
                    _json_dumps({"error": "processing error", "resourceType": rtype}) + "\n"
                )
                try:
                    staging.mark_error(job_id, row["id"], str(exc))
                except Exception:
                    _log.warning(
                        "%s job=%s mark_error failed row_id=%d", label, job_id, row["id"]
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
                    label, job_id, rtype, row["id"], exc,
                )
                output.append((
                    {"error": "processing error", "resourceType": rtype},
                    row["id"],
                    False,
                ))
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
                staging.mark_error(job_id, row_id, result.get("error", "processing error"))
            except Exception:
                _log.warning("%s job=%s mark_error failed row_id=%d", label, job_id, row_id)
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
    Phase 1 is still staging them — enabling Phase 1 and Phase 2 to run
    concurrently.  *phase1_state* is a dict updated in-place by the Phase 1
    thread; its cursor fields are merged into every checkpoint so crash-resume
    can restart Phase 1 from its last known position.

    ``staging_job_id`` selects which job's rows to read from the staging table.
    Defaults to ``job.id``; pass ``source_job_id`` for reprocess jobs that read
    another job's rows but write output under the current job's ID.

    Returns the total number of resources processed (succeeded + failed).
    """
    _staging_id = staging_job_id or job.id

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
                    # Phase 1 is still running — more rows may arrive shortly.
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
        with open(output_path, open_mode, encoding="utf-8") as fh:
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
                        # Timeout: Phase 1 is still running — save a checkpoint
                        # so the UI shows "fetching" and check for cancellation.
                        if _checkpoint_or_cancel(
                            store, job, processed, label,
                            phase1_done, phase1_state, staged_count,
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
                            store, job, processed, label,
                            phase1_done, phase1_state, staged_count,
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
                        computed, fh, staging, _staging_id, label, summary=collector,
                    )
                    processed += ok + bad
                    _batch_num += 1
                    if _batch_num % 5 == 0:
                        return 1 if _checkpoint_or_cancel(
                            store, job, processed, label,
                            phase1_done, phase1_state, staged_count,
                        ) else 0
                    return 0

                while True:
                    batch_rows = _prefetch_q.get()
                    if batch_rows is _SENTINEL:
                        break

                    fut = _compute_pool.submit(
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

    Calls ``plan_partitions`` once (idempotent), then loops through
    ``claim_next_partition`` / ``iter_partition`` / ``mark_partition_done``
    until no unclaimed partitions remain.  Each partition is written to an
    independent shard file ``{job.id}_p{NNNNNN}.ndjson`` under *output_dir*.

    On exception, the partition is released back to ``unclaimed`` via
    ``release_partition`` so a sibling worker or an Argo retry pod reclaims it.

    This function is safe to call concurrently from multiple worker threads or
    pods: the ``FOR UPDATE SKIP LOCKED`` in ``claim_next_partition`` ensures
    each partition is processed by exactly one caller at a time.

    Returns the total number of resources processed by *this* call.
    """
    from contextlib import suppress

    partition_count = staging.plan_partitions(job.id)
    _log.info(
        "%s_partition_plan job=%s partitions=%d",
        label, job.id, partition_count,
    )

    processed = 0
    _partitions_done = 0
    _LOG_EVERY = max(1, partition_count // 20)  # ~5% progress intervals
    while True:
        claim = staging.claim_next_partition(job.id)
        if claim is None:
            _log.info("%s_partitions_exhausted job=%s processed=%d", label, job.id, processed)
            break

        resource_type, partition_id = claim
        shard_path = os.path.join(output_dir, f"{job.id}_p{partition_id:06d}.ndjson")

        # Stream the partition in _BATCH_SIZE chunks rather than buffering all
        # rows at once.  A single 50 000-row partition processed as one batch
        # would (a) spike RAM, (b) defer mark_done until the whole partition
        # finished (no incremental progress), and (c) trigger a 50 000-resource
        # per-resource fallback on a single failure.
        shard_ok = shard_bad = 0
        chunk: list[dict] = []
        try:
            with open(shard_path, "w", encoding="utf-8") as fh:
                for row in staging.iter_partition(job.id, resource_type, partition_id):
                    chunk.append(row)
                    if len(chunk) >= _BATCH_SIZE:
                        ok, bad = _process_batch_with_fallback(
                            chunk, settings, pseudonymizer, processing_mode,
                            fh, staging, job.id, label,
                            summary=collector,
                        )
                        shard_ok += ok
                        shard_bad += bad
                        chunk = []
                if chunk:
                    ok, bad = _process_batch_with_fallback(
                        chunk, settings, pseudonymizer, processing_mode,
                        fh, staging, job.id, label,
                        summary=collector,
                    )
                    shard_ok += ok
                    shard_bad += bad
            staging.mark_partition_done(job.id, resource_type, partition_id)
            processed += shard_ok + shard_bad
            _partitions_done += 1
            # Log at INFO every ~5% to give visibility without flooding.
            if _partitions_done % _LOG_EVERY == 0:
                _log.info(
                    "%s_partition_progress job=%s done=%d/%d processed=%d",
                    label, job.id, _partitions_done, partition_count, processed,
                )
            else:
                _log.debug(
                    "%s_partition_done job=%s partition=%d ok=%d bad=%d",
                    label, job.id, partition_id, shard_ok, shard_bad,
                )
        except Exception as exc:
            _log.error(
                "%s_partition_failed job=%s partition=%d: %s",
                label, job.id, partition_id, exc, exc_info=True,
            )
            with suppress(Exception):
                staging.release_partition(job.id, resource_type, partition_id)
            with suppress(Exception):
                import os as _os
                _os.unlink(shard_path)
            raise

    return processed


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

    # Shards mode requires all rows staged before plan_partitions can bucket them.
    if phase1_thread is not None:
        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

    parallelism = max(1, _PROCESS_WORKERS)
    _log.info(
        "%s_shards_start job=%s parallelism=%d",
        label, job.id, parallelism,
    )

    if parallelism == 1:
        processed = _run_staged_phase2_partition_claim(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_dir, label, collector,
        )
    else:
        # Plan once (idempotent) so all worker threads see the partition set.
        partition_count = staging.plan_partitions(job.id)
        _log.info(
            "%s_shards_planned job=%s partitions=%d", label, job.id, partition_count,
        )

        results: list[int] = []
        with ThreadPoolExecutor(
            max_workers=parallelism, thread_name_prefix="shards-claim"
        ) as pool:
            futures = [
                pool.submit(
                    _run_staged_phase2_partition_claim,
                    job, store, staging, settings, pseudonymizer, processing_mode,
                    output_dir, label, collector,
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
        label, job.id, merged_lines, processed,
    )
    return processed


def _merge_shards(job_id: str, output_dir: str, output_path: str) -> int:
    """Concatenate ``{job_id}_p*.ndjson`` shards into *output_path*.

    Shards are merged in ascending partition order.  Returns the total line
    count written.  Safe to call after all ``_run_staged_phase2_partition_claim``
    callers have finished.
    """
    import glob

    pattern = os.path.join(output_dir, f"{job_id}_p*.ndjson")
    shard_files = sorted(glob.glob(pattern))
    total_lines = 0
    with open(output_path, "w", encoding="utf-8") as out:
        for shard in shard_files:
            with open(shard, encoding="utf-8") as fh:
                for line in fh:
                    out.write(line)
                    total_lines += 1
            try:
                os.unlink(shard)
            except Exception:
                pass
    return total_lines


# ---------------------------------------------------------------------------
# Internal: scoring persistence helper
# ---------------------------------------------------------------------------


def _persist_scoring_run(job, profile: str, summary_dict: dict, endpoint: str) -> None:
    """Write a processing_run row for a completed staged job. Silent on failure.

    Uses ``summary_dict["duration_sec"]`` (populated by
    ``JobSummaryCollector.to_dict()``) to avoid needing a separate t0 variable
    in every executor.  ``job.id`` is reused as the run_id so the row can be
    correlated with the async job record.
    """
    try:
        from api.services.scoring_helpers import persist_run_sync

        persist_run_sync(
            endpoint=endpoint,
            config_profile=profile,
            resource_count=summary_dict.get("total_resources", 0),
            error_count=summary_dict.get("error_count", 0),
            duration_ms=int(summary_dict.get("duration_sec", 0) * 1000),
            input_type="ndjson",
            summary=summary_dict,
            score=summary_dict.get("score"),
            run_id=job.id,
        )
    except Exception:
        _log.debug(
            "staged_persist_run_failed job=%s endpoint=%s", job.id, endpoint, exc_info=True
        )


# ---------------------------------------------------------------------------
# Public: two-phase executors
# ---------------------------------------------------------------------------


