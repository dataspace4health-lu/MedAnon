"""FHIR export job executors: bulk-export, cohort, patient-export, batch-patient-export, reprocess.

Each function is a synchronous callable that runs inside ``asyncio.to_thread``
in the worker loop.  When a staging store is configured, execution delegates to
the two-phase staged path; otherwise the streaming fallback is used.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from medanon_core.domain import Job
from pipeline.jobs.checkpoint import _truncate_to_lines, load_checkpoint, save_checkpoint
from pipeline.jobs.executor_stream import (
    INFRA_RESOURCE_TYPES,
    compress_ndjson,
    cursor_tracking_gen,
    process_stream_chunked,
    skip_to,
    _COMPRESS_RESULTS,
)

_worker_log = logging.getLogger("medanon.worker")
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")


def _execute_bulk_export(job: Job, store, staging) -> None:
    """Run a bulk-export job synchronously, resuming from checkpoint when available."""
    if staging is not None:
        from pipeline.jobs.staged_worker import execute_bulk_export_staged
        return execute_bulk_export_staged(job, store, staging)

    from integrations.fhir.client import fetch_all_resource_types, get_capability_statement
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
    fhir_cursor = checkpoint.get("fhir_cursor")  # pagination cursor for crash-resume
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)
        _worker_log.info(
            "bulk_export_resume job=%s from_line=%d cursor=%s",
            job.id, already_written,
            f"rt={fhir_cursor['current_rt']} page={fhir_cursor['page_url']}"
            if fhir_cursor else "none",
        )

    if resource_type:
        resource_types = [resource_type]
    elif type_filter:
        resource_types = [t.strip() for t in type_filter.split(",") if t.strip()]
    else:
        try:
            all_types = get_capability_statement(server_url, token=token, timeout=timeout)
            resource_types = [t for t in all_types if t not in INFRA_RESOURCE_TYPES]
        except Exception as exc:
            from integrations.fhir._transport import FhirCircuitBreakerOpen
            if isinstance(exc, FhirCircuitBreakerOpen):
                raise
            _worker_log.warning(
                "bulk_export capability_statement_failed job=%s: %s", job.id, exc
            )
            resource_types = ["Patient", "Observation", "Condition", "Encounter", "Procedure"]

    if not resource_types:
        _worker_log.info("bulk_export_empty job=%s — no resource types to export", job.id)
        Path(output_path).write_text("")
        from integrations.storage import store_result
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(store, job, {"phase": "done", "lines_written": 0})
        return

    extra_params: dict = {}
    if since:
        extra_params["_lastUpdated"] = f"ge{since}"

    save_checkpoint(store, job, {"phase": "fetching", "lines_written": already_written})

    # Pass cursor params so completed resource types and the current page are
    # skipped on resume, avoiding re-downloading all prior FHIR pages.
    cursor_aware_gen = fetch_all_resource_types(
        server_url,
        resource_types,
        params=extra_params if extra_params else None,
        token=token,
        timeout=timeout,
        completed_rts=(fhir_cursor or {}).get("completed_rts"),
        current_rt=(fhir_cursor or {}).get("current_rt"),
        current_rt_start_url=(fhir_cursor or {}).get("page_url"),
        yield_cursors=True,
    )
    _cursor_state: dict = {}
    cursor_gen = cursor_tracking_gen(cursor_aware_gen, _cursor_state)
    # On resume, skip only the resources already consumed within the current
    # FHIR page (page_offset entries at most).  Prior pages are skipped by
    # passing completed_rts and current_rt_start_url to the FHIR reader.
    resume_skip = (fhir_cursor or {}).get("page_offset", already_written)

    from pipeline.jobs.summary import JobSummaryCollector
    collector = JobSummaryCollector(config_profile=profile)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = process_stream_chunked(
            skip_to(cursor_gen, resume_skip),
            settings, pseudonymizer, fh, already_written,
            store, job, "bulk_export", summary=collector, cursor_state=_cursor_state,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
        from integrations.storage import store_result
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(store, job, {
            "phase": "done",
            "lines_written": count,
            "summary": collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            ),
        })
        _worker_log.info("bulk_export_done job=%s count=%d", job.id, count)


def _execute_cohort(job: Job, store, staging) -> None:
    """Run a cohort export job synchronously, resuming from checkpoint when available."""
    if staging is not None:
        from pipeline.jobs.staged_worker import execute_cohort_staged
        return execute_cohort_staged(job, store, staging)

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

    if already_written == 0:
        count = preflight_resource_count(server_url, resource_type=search_type, token=token)
        if count == 0:
            _worker_log.info(
                "cohort_empty job=%s — no %s resources found, skipping export",
                job.id, search_type,
            )
            Path(output_path).write_text("")
            from integrations.storage import store_result
            job.result_path = store_result(job.id, output_path)
            save_checkpoint(store, job, {"phase": "done", "lines_written": 0})
            return

    save_checkpoint(store, job, {"phase": "fetching", "lines_written": already_written})
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
        count, cancelled = process_stream_chunked(
            skip_to(gen, already_written),
            settings, pseudonymizer, fh, already_written,
            store, job, "cohort", summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
        from integrations.storage import store_result
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(store, job, {
            "phase": "done",
            "lines_written": count,
            "summary": collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            ),
        })
        _worker_log.info("cohort_done job=%s count=%d", job.id, count)


def _execute_reprocess(job: Job, store, staging) -> None:
    """Re-process staged rows with a (possibly different) config profile."""
    if staging is None:
        job.error = "Staging not configured (MEDANON_STAGING_DB_URL not set)"
        raise RuntimeError(job.error)
    from pipeline.jobs.staged_worker import execute_reprocess_staged
    return execute_reprocess_staged(job, store, staging)


def _execute_patient_export(job: Job, store, staging) -> None:
    """Run a patient $everything export + de-identify job."""
    if staging is not None:
        from pipeline.jobs.staged_worker import execute_patient_export_staged
        return execute_patient_export_staged(job, store, staging)

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

    save_checkpoint(store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_everything(server_url, "Patient", patient_id, token=token, timeout=timeout)

    from pipeline.jobs.summary import JobSummaryCollector
    collector = JobSummaryCollector(config_profile=profile)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = process_stream_chunked(
            skip_to(gen, already_written),
            settings, pseudonymizer, fh, already_written,
            store, job, "patient_export", summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
        from integrations.storage import store_result
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(store, job, {
            "phase": "done",
            "lines_written": count,
            "summary": collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            ),
        })
        _worker_log.info("patient_export_done job=%s count=%d", job.id, count)


def _execute_batch_patient_export(job: Job, store, staging) -> None:
    """Run a batch patient $everything export + de-identify job.

    Fetches $everything for each patient ID in the job params, de-duplicates
    shared resources, processes in chunks, and writes a single combined NDJSON.
    """
    if staging is not None:
        from pipeline.jobs.staged_worker import execute_batch_patient_export_staged
        return execute_batch_patient_export_staged(job, store, staging)

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

    save_checkpoint(store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_patients_everything(server_url, patient_ids, token=token, timeout=timeout)

    from pipeline.jobs.summary import JobSummaryCollector
    collector = JobSummaryCollector(config_profile=profile)

    with open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = process_stream_chunked(
            skip_to(gen, already_written),
            settings, pseudonymizer, fh, already_written,
            store, job, "batch_patient_export", summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
        from integrations.storage import store_result
        job.result_path = store_result(job.id, output_path)
        save_checkpoint(store, job, {
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
