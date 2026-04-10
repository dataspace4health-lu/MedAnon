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
from utils.json_fast import loads as _json_loads, dumps as _json_dumps
import os
from pathlib import Path

from medanon_core.domain import JobStatus
from pipeline.jobs.checkpoint import load_checkpoint, save_checkpoint
from pipeline.jobs.worker import _truncate_to_lines
from integrations.storage import store_result

_log = logging.getLogger("medanon.staged_worker")

_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_BATCH_SIZE = int(os.environ.get("MEDANON_STAGING_BATCH_SIZE", "1000"))

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
) -> list[dict]:
    """Process a batch of staged rows via the unified batch pipeline.

    Parses PostgreSQL rows into resource dicts, then delegates to
    :func:`pipeline.processor.process_data_batch` for cross-resource
    gPAS batching.
    """
    from pipeline.processor import process_data_batch

    resources: list[dict] = []
    for row in batch_rows:
        rj = row["resource_json"]
        resources.append(rj if isinstance(rj, dict) else _json_loads(rj))

    return process_data_batch(resources, settings, pseudonymizer, attach_manifest=True)


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
        # Per-resource fallback: process each resource individually
        for row in batch_rows:
            rj = row["resource_json"]
            resource = rj if isinstance(rj, dict) else _json_loads(rj)
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

    # ════════════════════════════════════════════════════════════════════════
    # Phase 1: Fetch resources → staging table
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        _log.info("staged_fetch_start job=%s resource_types=%s", job.id, resource_types)
        buffer: list[dict] = []

        for ti, rt in enumerate(resource_types):
            if ti < type_index:
                continue  # already fetched in a previous run
            start = fetch_cursor if ti == type_index else None

            for resource, next_url in fetch_resource_type(
                server_url,
                rt,
                params=extra_params if extra_params else None,
                token=token,
                timeout=timeout,
                start_url=start,
                yield_cursors=True,
            ):
                fresh = store.get(job.id)
                if fresh and fresh.status == JobStatus.CANCELLED:
                    _log.info("staged_fetch_cancelled job=%s", job.id)
                    return

                buffer.append(resource)
                if len(buffer) >= _BATCH_SIZE:
                    inserted = staging.stage_batch(job.id, buffer)
                    staged_count += inserted
                    buffer.clear()
                    fetch_cursor = next_url
                    save_checkpoint(
                        store,
                        job,
                        {
                            "phase": "fetching",
                            "staged_count": staged_count,
                            "type_index": ti,
                            "fetch_cursor": fetch_cursor,
                            "type_name": rt,
                        },
                    )

            if buffer:
                inserted = staging.stage_batch(job.id, buffer)
                staged_count += inserted
                buffer.clear()
            fetch_cursor = None

        _log.info("staged_fetch_done job=%s rows=%d", job.id, staged_count)
        save_checkpoint(
            store,
            job,
            {"phase": "processing", "staged_count": staged_count, "processed": 0},
        )
        phase = "processing"
        processed = 0

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2: Process staged rows → NDJSON
    # ════════════════════════════════════════════════════════════════════════
    if phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_process_start job=%s processed=%d", job.id, processed)

        with open(output_path, open_mode, encoding="utf-8") as fh:
            while True:
                fresh = store.get(job.id)
                if fresh and fresh.status == JobStatus.CANCELLED:
                    _log.info(
                        "staged_process_cancelled job=%s at=%d", job.id, processed
                    )
                    return

                batch_rows = staging.get_pending_batch(job.id, _BATCH_SIZE)
                if not batch_rows:
                    break

                ok, bad = _process_batch_with_fallback(
                    batch_rows,
                    settings,
                    pseudonymizer,
                    processing_mode,
                    fh,
                    staging,
                    job.id,
                    "staged_bulk_export",
                    summary=collector,
                )
                processed += ok + bad
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "processing",
                        "staged_count": staged_count,
                        "processed": processed,
                    },
                )

        job.result_path = store_result(job.id, output_path)
        save_checkpoint(
            store,
            job,
            {
                "phase": "done",
                "staged_count": staged_count,
                "processed": processed,
                "summary": collector.to_dict(
                    file_size_bytes=os.path.getsize(output_path)
                ),
            },
        )
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
    # Phase 1: Fetch → staging table
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        _log.info("staged_cohort_fetch_start job=%s", job.id)
        buffer: list[dict] = []
        gen = fetch_cohort(
            server_url,
            search_type=search_type,
            search_params=search_params_dict,
            everything_params=everything_params,
            token=token,
            timeout=timeout,
        )
        for resource in gen:
            fresh = store.get(job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                _log.info("staged_cohort_fetch_cancelled job=%s", job.id)
                return

            buffer.append(resource)
            if len(buffer) >= _BATCH_SIZE:
                inserted = staging.stage_batch(job.id, buffer)
                staged_count += inserted
                buffer.clear()
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "fetching",
                        "staged_count": staged_count,
                        "processed": 0,
                    },
                )

        if buffer:
            inserted = staging.stage_batch(job.id, buffer)
            staged_count += inserted

        _log.info("staged_cohort_fetch_done job=%s rows=%d", job.id, staged_count)
        save_checkpoint(
            store,
            job,
            {"phase": "processing", "staged_count": staged_count, "processed": 0},
        )
        phase = "processing"
        processed = 0

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2: Process staged rows → NDJSON
    # ════════════════════════════════════════════════════════════════════════
    if phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_cohort_process_start job=%s processed=%d", job.id, processed)

        with open(output_path, open_mode, encoding="utf-8") as fh:
            while True:
                fresh = store.get(job.id)
                if fresh and fresh.status == JobStatus.CANCELLED:
                    _log.info(
                        "staged_cohort_process_cancelled job=%s at=%d",
                        job.id,
                        processed,
                    )
                    return

                batch_rows = staging.get_pending_batch(job.id, _BATCH_SIZE)
                if not batch_rows:
                    break

                ok, bad = _process_batch_with_fallback(
                    batch_rows,
                    settings,
                    pseudonymizer,
                    processing_mode,
                    fh,
                    staging,
                    job.id,
                    "staged_cohort",
                    summary=collector,
                )
                processed += ok + bad
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "processing",
                        "staged_count": staged_count,
                        "processed": processed,
                    },
                )

        job.result_path = store_result(job.id, output_path)
        save_checkpoint(
            store,
            job,
            {
                "phase": "done",
                "staged_count": staged_count,
                "processed": processed,
                "summary": collector.to_dict(
                    file_size_bytes=os.path.getsize(output_path)
                ),
            },
        )
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
    # Phase 1: Fetch $everything → staging table
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        _log.info("staged_patient_fetch_start job=%s patient=%s", job.id, patient_id)
        buffer: list[dict] = []
        gen = fetch_everything(
            server_url,
            "Patient",
            patient_id,
            token=token,
            timeout=timeout,
            start_url=fetch_cursor,
        )
        for resource in gen:
            fresh = store.get(job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                _log.info("staged_patient_fetch_cancelled job=%s", job.id)
                return

            buffer.append(resource)
            if len(buffer) >= _BATCH_SIZE:
                inserted = staging.stage_batch(job.id, buffer)
                staged_count += inserted
                buffer.clear()
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "fetching",
                        "staged_count": staged_count,
                        "processed": 0,
                    },
                )

        if buffer:
            inserted = staging.stage_batch(job.id, buffer)
            staged_count += inserted

        _log.info("staged_patient_fetch_done job=%s rows=%d", job.id, staged_count)
        save_checkpoint(
            store,
            job,
            {"phase": "processing", "staged_count": staged_count, "processed": 0},
        )
        phase = "processing"
        processed = 0

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2: Process staged rows → NDJSON
    # ════════════════════════════════════════════════════════════════════════
    if phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_patient_process_start job=%s processed=%d", job.id, processed)

        with open(output_path, open_mode, encoding="utf-8") as fh:
            while True:
                fresh = store.get(job.id)
                if fresh and fresh.status == JobStatus.CANCELLED:
                    _log.info(
                        "staged_patient_process_cancelled job=%s at=%d",
                        job.id,
                        processed,
                    )
                    return

                batch_rows = staging.get_pending_batch(job.id, _BATCH_SIZE)
                if not batch_rows:
                    break

                ok, bad = _process_batch_with_fallback(
                    batch_rows,
                    settings,
                    pseudonymizer,
                    processing_mode,
                    fh,
                    staging,
                    job.id,
                    "staged_patient",
                    summary=collector,
                )
                processed += ok + bad
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "processing",
                        "staged_count": staged_count,
                        "processed": processed,
                    },
                )

        job.result_path = store_result(job.id, output_path)
        save_checkpoint(
            store,
            job,
            {
                "phase": "done",
                "staged_count": staged_count,
                "processed": processed,
                "summary": collector.to_dict(
                    file_size_bytes=os.path.getsize(output_path)
                ),
            },
        )
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
    # Phase 1: Fetch $everything for all patients → staging table
    # ════════════════════════════════════════════════════════════════════════
    if phase == "fetching":
        _log.info(
            "staged_batch_patient_fetch_start job=%s patients=%d",
            job.id,
            len(patient_ids),
        )
        buffer: list[dict] = []
        gen = fetch_patients_everything(
            server_url,
            patient_ids,
            token=token,
            timeout=timeout,
        )
        for resource in gen:
            fresh = store.get(job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                _log.info("staged_batch_patient_fetch_cancelled job=%s", job.id)
                return

            buffer.append(resource)
            if len(buffer) >= _BATCH_SIZE:
                inserted = staging.stage_batch(job.id, buffer)
                staged_count += inserted
                buffer.clear()
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "fetching",
                        "staged_count": staged_count,
                        "processed": 0,
                    },
                )

        if buffer:
            inserted = staging.stage_batch(job.id, buffer)
            staged_count += inserted

        _log.info(
            "staged_batch_patient_fetch_done job=%s rows=%d", job.id, staged_count
        )
        save_checkpoint(
            store,
            job,
            {"phase": "processing", "staged_count": staged_count, "processed": 0},
        )
        phase = "processing"
        processed = 0

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2: Process staged rows → NDJSON
    # ════════════════════════════════════════════════════════════════════════
    if phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(config_profile=profile)
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info(
            "staged_batch_patient_process_start job=%s processed=%d", job.id, processed
        )

        with open(output_path, open_mode, encoding="utf-8") as fh:
            while True:
                fresh = store.get(job.id)
                if fresh and fresh.status == JobStatus.CANCELLED:
                    _log.info(
                        "staged_batch_patient_process_cancelled job=%s at=%d",
                        job.id,
                        processed,
                    )
                    return

                batch_rows = staging.get_pending_batch(job.id, _BATCH_SIZE)
                if not batch_rows:
                    break

                ok, bad = _process_batch_with_fallback(
                    batch_rows,
                    settings,
                    pseudonymizer,
                    processing_mode,
                    fh,
                    staging,
                    job.id,
                    "staged_batch_patient",
                    summary=collector,
                )
                processed += ok + bad
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "processing",
                        "staged_count": staged_count,
                        "processed": processed,
                    },
                )

        job.result_path = store_result(job.id, output_path)
        save_checkpoint(
            store,
            job,
            {
                "phase": "done",
                "staged_count": staged_count,
                "processed": processed,
                "summary": collector.to_dict(
                    file_size_bytes=os.path.getsize(output_path)
                ),
            },
        )
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

    collector = JobSummaryCollector(config_profile=profile)

    _log.info(
        "staged_reprocess_start job=%s source=%s profile=%s",
        job.id,
        source_job_id,
        profile,
    )
    open_mode = "a" if processed > 0 else "w"
    if processed > 0:
        _truncate_to_lines(output_path, processed)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        while True:
            fresh = store.get(job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                _log.info("staged_reprocess_cancelled job=%s at=%d", job.id, processed)
                return

            batch_rows = staging.get_pending_batch(source_job_id, _BATCH_SIZE)
            if not batch_rows:
                break

            ok, bad = _process_batch_with_fallback(
                batch_rows,
                settings,
                pseudonymizer,
                processing_mode,
                fh,
                staging,
                source_job_id,
                "staged_reprocess",
                summary=collector,
            )
            processed += ok + bad
            save_checkpoint(store, job, {"phase": "processing", "processed": processed})

    job.result_path = store_result(job.id, output_path)
    save_checkpoint(
        store,
        job,
        {
            "phase": "done",
            "processed": processed,
            "summary": collector.to_dict(file_size_bytes=os.path.getsize(output_path)),
        },
    )
    _log.info("staged_reprocess_done job=%s processed=%d", job.id, processed)
