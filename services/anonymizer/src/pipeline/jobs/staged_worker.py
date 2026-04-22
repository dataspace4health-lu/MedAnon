"""Two-phase staged bulk-export / cohort executor.

Phase 1 — Fetch:   Stream resources from HAPI FHIR into ``medanon.staged_resources``
                   (PostgreSQL) with pagination-cursor checkpoints.  A crash here
                   resumes from the saved cursor — no FHIR re-fetch.

Phase 2 — Process: Read ``pending`` rows in batches of BATCH_SIZE, invoke ONE gPAS
                   call per batch (instead of one per resource), write NDJSON, mark
                   rows ``done`` atomically.

Backward-compat:   Activated only when ``MEDANON_STAGING_DB_URL`` is set.  The
                   original ``_execute_bulk_export`` / ``_execute_cohort`` are kept
                   as fallback so existing deployments are unaffected.
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


def _process_batch(
    batch_rows: list[dict],
    settings,
    pseudonymizer,
    processing_mode: str,
    seen_values=None,
) -> list[dict]:
    """Process a batch of staged rows via the unified batch pipeline.

    Parses PostgreSQL rows into resource dicts, then delegates to
    :func:`pipeline.processor.process_data_batch` for cross-resource
    gPAS batching.

    When *seen_values* is provided (a :class:`~pipeline.processor._CappedSet`),
    values already pseudonymized in prior batches are deduped from the gPAS
    HTTP call — same Patient ID referenced 500 times = 1 gPAS lookup.
    """
    from pipeline.processor import process_data_batch

    resources: list[dict] = []
    for row in batch_rows:
        rj = row["resource_json"]
        resources.append(rj if isinstance(rj, dict) else _json_loads(rj))

    return process_data_batch(
        resources,
        settings,
        pseudonymizer,
        attach_manifest=True,
        _exclude_cached=seen_values,
        _seen_accumulator=seen_values,
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
    seen_values=None,
) -> tuple[int, int]:
    """Process a staged batch with per-resource fallback on failure.

    Tries the entire batch first.  On failure, falls back to processing
    each resource individually so a single bad resource does not kill the
    entire job.  Successful results are written to *fh*; failed rows are
    marked via ``staging.mark_error``.

    When *summary* (:class:`~pipeline.jobs.summary.JobSummaryCollector`) is
    provided, each result is recorded for the completion summary.

    Returns ``(succeeded, failed)`` counts.
    """
    from pipeline.processor import process_data_batch

    succeeded = 0
    failed = 0

    # Snapshot originals as JSON strings BEFORE batch processing.
    # _process_batch mutates resource dicts in-place (gPAS write-back). If the
    # batch partially succeeds then raises, batch_rows[i]["resource_json"] for
    # already-processed rows will hold mutated dicts (id = txt_*) instead of
    # original UUIDs. Without this snapshot the fallback would send pseudonyms
    # back to gPAS as originals, creating double-pseudonymization chains
    # (e.g. uuid → txt_A → txt_B). Always serialize to string here so the
    # fallback always parses a fresh, unmutated copy of each resource.
    original_jsons: list[str] = [
        row["resource_json"]
        if isinstance(row["resource_json"], str)
        else _json_dumps(row["resource_json"])
        for row in batch_rows
    ]

    try:
        results = _process_batch(batch_rows, settings, pseudonymizer, processing_mode, seen_values)
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
        # Per-resource fallback: process each resource from the pre-mutation snapshot.
        # Using original_jsons (not row["resource_json"]) ensures the fallback always
        # sees the original UUID-based resource, never a partially-mutated copy.
        for orig_json, row in zip(original_jsons, batch_rows):
            resource = _json_loads(orig_json)
            rtype = (
                resource.get("resourceType", "Unknown")
                if isinstance(resource, dict)
                else "Unknown"
            )
            try:
                result = process_data_batch(
                    [resource], settings, pseudonymizer, attach_manifest=True
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

    Provides (a) cross-batch gPAS dedup via a shared ``_CappedSet`` and
    (b) a ``_PREFETCH_QUEUE_SIZE``-batch lookahead pipeline so PostgreSQL
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
    from pipeline.processor import _CappedSet

    _staging_id = staging_job_id or job.id
    _seen_values = _CappedSet()

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
    try:
        with open(output_path, open_mode, encoding="utf-8") as fh:
            while True:
                batch_rows = _prefetch_q.get()
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
                    seen_values=_seen_values,
                )
                processed += ok + bad

                # Throttle DB round-trips: check for cancellation and save a
                # checkpoint every 5 batches (matches executor_stream cadence).
                if _batch_num % 5 == 0:
                    fresh = store.get(job.id)
                    if fresh and fresh.status == JobStatus.CANCELLED:
                        _log.info("%s_cancelled job=%s at=%d", label, job.id, processed)
                        return processed

                    # Assemble checkpoint — merge Phase 1 cursor state when
                    # Phase 1 is still running so crash-resume can restart
                    # fetching from the last known FHIR page offset.
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
    finally:
        prefetch_thread.join(timeout=5.0)

    if _prefetch_exc:
        raise _prefetch_exc[0]
    return processed


# ---------------------------------------------------------------------------
# Public: two-phase executors
# ---------------------------------------------------------------------------


def execute_bulk_export_staged(job, store, staging) -> None:
    """Staged two-phase bulk-export executor (synchronous — runs via asyncio.to_thread)."""
    from integrations.fhir.client import (
        fetch_resource_type,
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
    processing_mode = str(getattr(settings, "processing_errors", "raise")).lower()
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    phase = checkpoint.get("phase", "fetching")
    staged_count = checkpoint.get("staged_count", 0)
    processed = checkpoint.get("processed", 0)
    type_index = checkpoint.get("type_index", 0)
    fetch_cursor = checkpoint.get("fetch_cursor")

    # ── Determine resource types ────────────────────────────────────────────
    if resource_type:
        resource_types = [resource_type]
    elif type_filter:
        resource_types = [t.strip() for t in type_filter.split(",") if t.strip()]
    else:
        try:
            all_types = get_capability_statement(
                server_url, token=token, timeout=timeout
            )
            resource_types = [t for t in all_types if t not in _INFRA]
        except Exception as exc:
            from integrations.fhir._transport import FhirCircuitBreakerOpen

            if isinstance(exc, FhirCircuitBreakerOpen):
                raise
            _log.warning("capability_statement_failed job=%s: %s", job.id, exc)
            resource_types = [
                "Patient",
                "Observation",
                "Condition",
                "Encounter",
                "Procedure",
            ]

    if not resource_types:
        _log.info("bulk_export_staged_empty job=%s", job.id)
        Path(output_path).write_text("")
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(
            store, job, {"phase": "done", "staged_count": 0, "processed": 0}
        )
        return

    extra_params: dict = {}
    if since:
        extra_params["_lastUpdated"] = f"ge{since}"

    from pipeline.jobs.summary import JobSummaryCollector

    collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 1 + Phase 2 (overlapped): fetch into staging while processing
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        # Shared mutable state updated by the Phase 1 thread so Phase 2 can
        # embed the current FHIR cursor into every checkpoint it writes.
        # This ensures crash-resume can restart Phase 1 from where it left off
        # even when Phase 2 has already made progress.
        phase1_state: dict = {
            "staged_count": staged_count,
            "type_index": type_index,
            "fetch_cursor": fetch_cursor,
        }
        phase1_done = threading.Event()
        phase1_exc: list = []

        def _run_phase1() -> None:
            _log.info("staged_fetch_start job=%s resource_types=%s", job.id, resource_types)
            buffer: list[dict] = []
            try:
                for ti, rt in enumerate(resource_types):
                    if ti < phase1_state["type_index"]:
                        continue  # already fetched in a previous run
                    start = phase1_state["fetch_cursor"] if ti == phase1_state["type_index"] else None

                    for resource, page_url, page_offset in fetch_resource_type(
                        server_url,
                        rt,
                        params=extra_params if extra_params else None,
                        token=token,
                        timeout=timeout,
                        start_url=start,
                        yield_cursors=True,
                    ):
                        buffer.append(resource)
                        if len(buffer) >= _BATCH_SIZE:
                            fresh = store.get(job.id)
                            if fresh and fresh.status == JobStatus.CANCELLED:
                                _log.info("staged_fetch_cancelled job=%s", job.id)
                                return
                            inserted = staging.stage_batch(job.id, buffer)
                            phase1_state["staged_count"] += inserted
                            buffer.clear()
                            phase1_state["fetch_cursor"] = page_url
                            phase1_state["type_index"] = ti
                            phase1_state["type_name"] = rt

                    if buffer:
                        inserted = staging.stage_batch(job.id, buffer)
                        phase1_state["staged_count"] += inserted
                        buffer.clear()
                    phase1_state["fetch_cursor"] = None

                _log.info(
                    "staged_fetch_done job=%s rows=%d",
                    job.id,
                    phase1_state["staged_count"],
                )
            except Exception as exc:
                phase1_exc.append(exc)
            finally:
                phase1_done.set()

        phase1_thread = threading.Thread(target=_run_phase1, daemon=True)
        phase1_thread.start()

        # Phase 2 starts immediately; it polls for rows while Phase 1 is running.
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info(
            "staged_process_start job=%s processed=%d (overlap mode)", job.id, processed
        )

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, open_mode, staged_count, processed,
            "staged_bulk_export", collector,
            phase1_done=phase1_done,
            phase1_state=phase1_state,
        )

        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

        staged_count = phase1_state.get("staged_count", staged_count)

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        checkpoint_data: dict = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report()
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as _fh:
                    _fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _log.debug("staged_bulk_export_audit_write_failed job=%s", job.id, exc_info=True)
        save_checkpoint(store, job, checkpoint_data)
        _log.info("staged_bulk_export_done job=%s processed=%d", job.id, processed)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2 only (crash-resume: Phase 1 was already complete)
    # ════════════════════════════════════════════════════════════════════════
    elif phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_process_start job=%s processed=%d", job.id, processed)

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, open_mode, staged_count, processed,
            "staged_bulk_export", collector,
        )

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        checkpoint_data: dict = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report()
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as _fh:
                    _fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _log.debug("staged_bulk_export_audit_write_failed job=%s", job.id, exc_info=True)
        save_checkpoint(store, job, checkpoint_data)
        _log.info("staged_bulk_export_done job=%s processed=%d", job.id, processed)


def execute_cohort_staged(job, store, staging) -> None:
    """Staged two-phase cohort executor (synchronous — runs via asyncio.to_thread)."""
    from integrations.fhir.client import fetch_cohort, preflight_resource_count
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    search_type = params["search_type"]
    search_params_dict = params.get("search_params", {})
    everything_params = params.get("everything_params", {})
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()
    processing_mode = str(getattr(settings, "processing_errors", "raise")).lower()
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    phase = checkpoint.get("phase", "fetching")
    staged_count = checkpoint.get("staged_count", 0)
    processed = checkpoint.get("processed", 0)

    # Preflight (only on fresh start)
    if phase == "fetching" and staged_count == 0:
        count = preflight_resource_count(
            server_url, resource_type=search_type, token=token
        )
        if count == 0:
            _log.info("cohort_staged_empty job=%s", job.id)
            Path(output_path).write_text("")
            job.result_path = store_result(job.id, output_path)
            save_checkpoint(
                store, job, {"phase": "done", "staged_count": 0, "processed": 0}
            )
            return

    # ════════════════════════════════════════════════════════════════════════
    # Phase 1 + Phase 2 (overlapped)
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        phase1_state: dict = {"staged_count": staged_count}
        phase1_done = threading.Event()
        phase1_exc: list = []

        def _run_phase1() -> None:
            _log.info("staged_cohort_fetch_start job=%s", job.id)
            buffer: list[dict] = []
            try:
                gen = fetch_cohort(
                    server_url,
                    search_type=search_type,
                    search_params=search_params_dict,
                    everything_params=everything_params,
                    token=token,
                    timeout=timeout,
                )
                for resource in gen:
                    buffer.append(resource)
                    if len(buffer) >= _BATCH_SIZE:
                        fresh = store.get(job.id)
                        if fresh and fresh.status == JobStatus.CANCELLED:
                            _log.info("staged_cohort_fetch_cancelled job=%s", job.id)
                            return
                        inserted = staging.stage_batch(job.id, buffer)
                        phase1_state["staged_count"] += inserted
                        buffer.clear()

                if buffer:
                    inserted = staging.stage_batch(job.id, buffer)
                    phase1_state["staged_count"] += inserted

                _log.info(
                    "staged_cohort_fetch_done job=%s rows=%d",
                    job.id,
                    phase1_state["staged_count"],
                )
            except Exception as exc:
                phase1_exc.append(exc)
            finally:
                phase1_done.set()

        phase1_thread = threading.Thread(target=_run_phase1, daemon=True)
        phase1_thread.start()

        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)
        _log.info(
            "staged_cohort_process_start job=%s processed=%d (overlap mode)", job.id, processed
        )

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, "w", staged_count, processed,
            "staged_cohort", collector,
            phase1_done=phase1_done,
            phase1_state=phase1_state,
        )

        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

        staged_count = phase1_state.get("staged_count", staged_count)

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report()
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as _fh:
                    _fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _log.debug("staged_cohort_audit_write_failed job=%s", job.id, exc_info=True)
        save_checkpoint(store, job, checkpoint_data)
        _log.info("staged_cohort_done job=%s processed=%d", job.id, processed)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2 only (crash-resume: Phase 1 was already complete)
    # ════════════════════════════════════════════════════════════════════════
    elif phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_cohort_process_start job=%s processed=%d", job.id, processed)

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, open_mode, staged_count, processed,
            "staged_cohort", collector,
        )

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report()
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as _fh:
                    _fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _log.debug("staged_cohort_audit_write_failed job=%s", job.id, exc_info=True)
        save_checkpoint(store, job, checkpoint_data)
        _log.info("staged_cohort_done job=%s processed=%d", job.id, processed)


def execute_patient_export_staged(job, store, staging) -> None:
    """Staged two-phase patient $everything executor (synchronous — runs via asyncio.to_thread)."""
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
    processing_mode = str(getattr(settings, "processing_errors", "raise")).lower()
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    phase = checkpoint.get("phase", "fetching")
    staged_count = checkpoint.get("staged_count", 0)
    processed = checkpoint.get("processed", 0)
    fetch_cursor = checkpoint.get("fetch_cursor")

    # ════════════════════════════════════════════════════════════════════════
    # Phase 1 + Phase 2 (overlapped)
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        phase1_state: dict = {"staged_count": staged_count, "fetch_cursor": fetch_cursor}
        phase1_done = threading.Event()
        phase1_exc: list = []

        def _run_phase1() -> None:
            _log.info("staged_patient_fetch_start job=%s patient=%s", job.id, patient_id)
            buffer: list[dict] = []
            try:
                gen = fetch_everything(
                    server_url,
                    "Patient",
                    patient_id,
                    token=token,
                    timeout=timeout,
                    start_url=phase1_state["fetch_cursor"],
                )
                for resource in gen:
                    buffer.append(resource)
                    if len(buffer) >= _BATCH_SIZE:
                        fresh = store.get(job.id)
                        if fresh and fresh.status == JobStatus.CANCELLED:
                            _log.info("staged_patient_fetch_cancelled job=%s", job.id)
                            return
                        inserted = staging.stage_batch(job.id, buffer)
                        phase1_state["staged_count"] += inserted
                        buffer.clear()

                if buffer:
                    inserted = staging.stage_batch(job.id, buffer)
                    phase1_state["staged_count"] += inserted

                _log.info(
                    "staged_patient_fetch_done job=%s rows=%d",
                    job.id,
                    phase1_state["staged_count"],
                )
            except Exception as exc:
                phase1_exc.append(exc)
            finally:
                phase1_done.set()

        phase1_thread = threading.Thread(target=_run_phase1, daemon=True)
        phase1_thread.start()

        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)
        _log.info(
            "staged_patient_process_start job=%s processed=%d (overlap mode)", job.id, processed
        )

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, "w", staged_count, processed,
            "staged_patient", collector,
            phase1_done=phase1_done,
            phase1_state=phase1_state,
        )

        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

        staged_count = phase1_state.get("staged_count", staged_count)

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report()
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as _fh:
                    _fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _log.debug("staged_patient_audit_write_failed job=%s", job.id, exc_info=True)
        save_checkpoint(store, job, checkpoint_data)
        _log.info("staged_patient_export_done job=%s processed=%d", job.id, processed)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2 only (crash-resume: Phase 1 was already complete)
    # ════════════════════════════════════════════════════════════════════════
    elif phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_patient_process_start job=%s processed=%d", job.id, processed)

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, open_mode, staged_count, processed,
            "staged_patient", collector,
        )

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report()
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as _fh:
                    _fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _log.debug("staged_patient_audit_write_failed job=%s", job.id, exc_info=True)
        save_checkpoint(store, job, checkpoint_data)
        _log.info("staged_patient_export_done job=%s processed=%d", job.id, processed)


def execute_batch_patient_export_staged(job, store, staging) -> None:
    """Staged two-phase batch patient $everything executor (synchronous — runs via asyncio.to_thread)."""
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
    processing_mode = str(getattr(settings, "processing_errors", "raise")).lower()
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    phase = checkpoint.get("phase", "fetching")
    staged_count = checkpoint.get("staged_count", 0)
    processed = checkpoint.get("processed", 0)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 1 + Phase 2 (overlapped)
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        phase1_state: dict = {"staged_count": staged_count}
        phase1_done = threading.Event()
        phase1_exc: list = []

        def _run_phase1() -> None:
            _log.info(
                "staged_batch_patient_fetch_start job=%s patients=%d",
                job.id,
                len(patient_ids),
            )
            buffer: list[dict] = []
            try:
                gen = fetch_patients_everything(
                    server_url,
                    patient_ids,
                    token=token,
                    timeout=timeout,
                )
                for resource in gen:
                    buffer.append(resource)
                    if len(buffer) >= _BATCH_SIZE:
                        fresh = store.get(job.id)
                        if fresh and fresh.status == JobStatus.CANCELLED:
                            _log.info("staged_batch_patient_fetch_cancelled job=%s", job.id)
                            return
                        inserted = staging.stage_batch(job.id, buffer)
                        phase1_state["staged_count"] += inserted
                        buffer.clear()

                if buffer:
                    inserted = staging.stage_batch(job.id, buffer)
                    phase1_state["staged_count"] += inserted

                _log.info(
                    "staged_batch_patient_fetch_done job=%s rows=%d",
                    job.id,
                    phase1_state["staged_count"],
                )
            except Exception as exc:
                phase1_exc.append(exc)
            finally:
                phase1_done.set()

        phase1_thread = threading.Thread(target=_run_phase1, daemon=True)
        phase1_thread.start()

        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)
        _log.info(
            "staged_batch_patient_process_start job=%s processed=%d (overlap mode)",
            job.id,
            processed,
        )

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, "w", staged_count, processed,
            "staged_batch_patient", collector,
            phase1_done=phase1_done,
            phase1_state=phase1_state,
        )

        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

        staged_count = phase1_state.get("staged_count", staged_count)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2 only (crash-resume: Phase 1 was already complete)
    # ════════════════════════════════════════════════════════════════════════
    elif phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info(
            "staged_batch_patient_process_start job=%s processed=%d", job.id, processed
        )

        processed = _run_staged_phase2(
            job, store, staging, settings, pseudonymizer, processing_mode,
            output_path, open_mode, staged_count, processed,
            "staged_batch_patient", collector,
        )

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report()
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as _fh:
                    _fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _log.debug("staged_batch_patient_audit_write_failed job=%s", job.id, exc_info=True)
        save_checkpoint(store, job, checkpoint_data)
        _log.info(
            "staged_batch_patient_export_done job=%s processed=%d", job.id, processed
        )


def execute_reprocess_staged(job, store, staging) -> None:
    """Re-process a previous job's staged rows with a (possibly different) config profile.

    Reads ``job.params["source_job_id"]`` for the staging rows to re-process,
    resets them to ``pending``, and writes output to a new NDJSON (this job's id).
    """
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    source_job_id = params["source_job_id"]
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()
    processing_mode = str(getattr(settings, "processing_errors", "raise")).lower()
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    checkpoint = load_checkpoint(job) or {}
    processed = checkpoint.get("processed", 0)

    # Reset source rows to pending (idempotent on resume since rows are already pending)
    if processed == 0:
        staging.reset_pending(source_job_id)

    from pipeline.jobs.summary import JobSummaryCollector

    collector = JobSummaryCollector(config_profile=profile, settings=settings, job_id=job.id)

    _log.info(
        "staged_reprocess_start job=%s source=%s profile=%s",
        job.id,
        source_job_id,
        profile,
    )
    open_mode = "a" if processed > 0 else "w"
    if processed > 0:
        _truncate_to_lines(output_path, processed)

    processed = _run_staged_phase2(
        job, store, staging, settings, pseudonymizer, processing_mode,
        output_path, open_mode, 0, processed,
        "staged_reprocess", collector,
        staging_job_id=source_job_id,
    )

    job.result_path = store_result(job.id, output_path)
    summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
    checkpoint_data = {
        "phase": "done",
        "processed": processed,
        "summary": summary_dict,
    }
    audit_report = collector.generate_audit_report()
    if audit_report:
        try:
            audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
            with open(audit_path, "w", encoding="utf-8") as _fh:
                _fh.write(audit_report)
            checkpoint_data["score_audit_path"] = audit_path
        except Exception:
            _log.debug("staged_reprocess_audit_write_failed job=%s", job.id, exc_info=True)
    save_checkpoint(store, job, checkpoint_data)
    _log.info("staged_reprocess_done job=%s processed=%d", job.id, processed)
