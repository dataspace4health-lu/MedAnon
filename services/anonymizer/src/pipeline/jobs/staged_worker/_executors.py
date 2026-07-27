"""staged_worker._executors  standard staged export/cohort/patient executors.

Contains five public executor functions:
    execute_bulk_export_staged          system/type-level bulk export
    execute_cohort_staged               cohort (patient-search) export
    execute_patient_export_staged       single-patient $everything export
    execute_batch_patient_export_staged  multi-patient $everything export
    execute_reprocess_staged            re-process already-staged rows
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from pipeline.jobs.staged_worker._core import (
    _log,
    _OUTPUT_DIR,
    _OUTPUT_MODE,
    _INFRA,
    _BATCH_SIZE,
    _run_staged_phase2,
    _run_staged_phase2_shards,
    _persist_scoring_run,
)
from domain.jobs import JobStatus
from pipeline.jobs.checkpoint import (
    load_checkpoint,
    save_checkpoint,
    _truncate_to_lines,
)
from pipeline.jobs.result_publisher import publish_result
from pipeline.jobs.source_resolver import resolve_source_token


# ---------------------------------------------------------------------------
# Phase-1 resource source
#
# A single bulk-export job is CPU-bound (resource_assembly + rule_evaluation)
# and GIL-bound, so it only speeds up by spreading work across PROCESSES -
# which is what this path's Phase-2 partition + process-executor does. Phase 1
# historically fed it via offset-paginated search, which is O(n^2) on HAPI and
# OOMs it on large ``Binary`` pages. Pulling Phase 1 from the native Bulk Data
# ``$export`` stream instead gives one linear server-side scan, so a single job
# gets the good fetch AND the multi-core processing.
#
# Both fetchers are module-level thin wrappers (kept lazy to avoid import cost)
# so the source can be selected and tested without a live FHIR server.
# ---------------------------------------------------------------------------


def fetch_resource_type(*args, **kwargs):
    """Lazy passthrough to the paginated per-type fetcher."""
    from integrations.fhir.client import fetch_resource_type as _impl

    return _impl(*args, **kwargs)


def _bulk_export_stream(
    server_url, *, type_filter=None, since=None, token=None, timeout=30
):
    """Yield resources from the native FHIR Bulk Data ``$export`` stream."""
    from integrations.fhir.bulk import bulk_export

    yield from bulk_export(
        server_url,
        level="system",
        type_filter=type_filter,
        since=since,
        token=token,
        timeout=timeout,
    )


def _iter_phase1_resources(
    server_url,
    resource_types,
    *,
    params=None,
    token=None,
    timeout=30,
    native=False,
    skip=0,
    type_filter=None,
):
    """Yield resources for Phase-1 staging.

    *native* selects the ``$export`` stream (a single linear scan across all
    requested types). ``$export`` carries no page cursor, so crash-resume works
    by skipping the first *skip* resources already staged. When *native* is
    False the legacy per-type offset-paginated fetch is used unchanged.

    *type_filter* is the caller's EXPLICIT scope (``resource_type`` /
    ``type_filter`` job params), forwarded as ``_type``. It must not be derived
    from *resource_types*: that list is the full CapabilityStatement (130+
    types when the export is unscoped), and sending it as ``_type`` makes HAPI
    reject the kick-off with HTTP 400. ``None`` means a bare system-level
    ``$export``, which already exports everything.
    """
    if native:
        for index, resource in enumerate(
            _bulk_export_stream(
                server_url, type_filter=type_filter, token=token, timeout=timeout
            )
        ):
            if index < skip:
                continue
            yield resource
        return

    for rt in resource_types:
        for resource, _page_url, _page_offset in fetch_resource_type(
            server_url,
            rt,
            params=params,
            token=token,
            timeout=timeout,
            yield_cursors=True,
        ):
            yield resource


def execute_bulk_export_staged(job, store, staging) -> None:
    """Staged two-phase bulk-export executor (synchronous  runs via asyncio.to_thread)."""
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
    token = resolve_source_token(params)
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

    # Write an early checkpoint before any FHIR network calls so the UI
    # immediately shows "fetching" instead of staying at "queued" during
    # get_capability_statement (up to 3 retries × 35s = 105s).
    if phase == "fetching":
        save_checkpoint(
            store,
            job,
            {
                "phase": "fetching",
                "staged_count": staged_count,
                "processed": processed,
            },
        )

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
        job.result_path = publish_result(job, output_path)
        save_checkpoint(
            store, job, {"phase": "done", "staged_count": 0, "processed": 0}
        )
        return

    extra_params: dict = {}
    if since:
        extra_params["_lastUpdated"] = f"ge{since}"

    from pipeline.jobs.summary import JobSummaryCollector

    collector = JobSummaryCollector(
        config_profile=profile, settings=settings, job_id=job.id
    )

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

        # Prefer the native $export stream for Phase 1: one linear server-side
        # scan instead of O(n^2) offset pagination (which also OOMs HAPI on big
        # Binary pages). Phase 2 keeps its partition + process-executor
        # parallelism, so a single job gets the fast fetch AND multiple cores.
        # Fail-safe: any probe failure falls back to paginated search.
        _use_native_fetch = False
        if (
            os.environ.get("MEDANON_STAGED_NATIVE_FETCH", "auto").strip().lower()
            != "off"
        ):
            try:
                from integrations.fhir.reader import server_supports_bulk_export

                _use_native_fetch = server_supports_bulk_export(
                    server_url, token=token, timeout=8
                )
            except Exception:
                _use_native_fetch = False

        def _run_phase1() -> None:
            _log.info(
                "staged_fetch_start job=%s resource_types=%s", job.id, resource_types
            )
            buffer: list[dict] = []
            try:
                if _use_native_fetch:
                    # $export carries no page cursor: resume by skipping the
                    # rows already staged in a previous run.
                    for resource in _iter_phase1_resources(
                        server_url,
                        resource_types,
                        params=extra_params if extra_params else None,
                        token=token,
                        timeout=timeout,
                        native=True,
                        skip=phase1_state["staged_count"],
                        # Explicit user scope only; None => bare system $export.
                        type_filter=type_filter or resource_type,
                    ):
                        buffer.append(resource)
                        if len(buffer) >= _BATCH_SIZE:
                            fresh = store.get(job.id)
                            if fresh and fresh.status == JobStatus.CANCELLED:
                                _log.info("staged_fetch_cancelled job=%s", job.id)
                                return
                            inserted = staging.stage_batch(
                                job.id, buffer, fhir_source_url=server_url
                            )
                            phase1_state["staged_count"] += inserted
                            buffer.clear()
                    if buffer:
                        inserted = staging.stage_batch(
                            job.id, buffer, fhir_source_url=server_url
                        )
                        phase1_state["staged_count"] += inserted
                        buffer.clear()
                    _log.info(
                        "staged_fetch_done job=%s rows=%d (native $export)",
                        job.id,
                        phase1_state["staged_count"],
                    )
                    return

                for ti, rt in enumerate(resource_types):
                    if ti < phase1_state["type_index"]:
                        continue  # already fetched in a previous run
                    start = (
                        phase1_state["fetch_cursor"]
                        if ti == phase1_state["type_index"]
                        else None
                    )

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
                            inserted = staging.stage_batch(
                                job.id, buffer, fhir_source_url=server_url
                            )
                            phase1_state["staged_count"] += inserted
                            buffer.clear()
                            phase1_state["fetch_cursor"] = page_url
                            phase1_state["type_index"] = ti
                            phase1_state["type_name"] = rt

                    if buffer:
                        inserted = staging.stage_batch(
                            job.id, buffer, fhir_source_url=server_url
                        )
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

        # Save an early "fetching" checkpoint before Phase 1 starts so the UI
        # immediately shows the correct phase instead of staying at "queued"
        # while Phase 1 works through all 130+ FHIR resource types.
        save_checkpoint(
            store,
            job,
            {
                "phase": "fetching",
                "staged_count": staged_count,
                "processed": processed,
            },
        )

        phase1_thread = threading.Thread(target=_run_phase1, daemon=True)
        phase1_thread.start()

        # Phase 2 starts immediately; it polls for rows while Phase 1 is running.
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info(
            "staged_process_start job=%s processed=%d (overlap mode)", job.id, processed
        )

        if _OUTPUT_MODE == "shards":
            processed = _run_staged_phase2_shards(
                job,
                store,
                staging,
                settings,
                pseudonymizer,
                processing_mode,
                _OUTPUT_DIR,
                output_path,
                "staged_bulk_export",
                collector,
                phase1_done=phase1_done,
                phase1_thread=phase1_thread,
                phase1_exc=phase1_exc,
            )
        else:
            processed = _run_staged_phase2(
                job,
                store,
                staging,
                settings,
                pseudonymizer,
                processing_mode,
                output_path,
                open_mode,
                staged_count,
                processed,
                "staged_bulk_export",
                collector,
                phase1_done=phase1_done,
                phase1_state=phase1_state,
            )

            phase1_thread.join()
            if phase1_exc:
                raise phase1_exc[0]

        staged_count = phase1_state.get("staged_count", staged_count)

        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        job.result_path = publish_result(job, output_path, audit=summary_dict)
        checkpoint_data: dict = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        save_checkpoint(store, job, checkpoint_data)
        _persist_scoring_run(
            job, profile, summary_dict, "staged_bulk_export", collector
        )
        _log.info("staged_bulk_export_done job=%s processed=%d", job.id, processed)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2 only (crash-resume: Phase 1 was already complete)
    # ════════════════════════════════════════════════════════════════════════
    elif phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_process_start job=%s processed=%d", job.id, processed)

        if _OUTPUT_MODE == "shards":
            processed = _run_staged_phase2_shards(
                job,
                store,
                staging,
                settings,
                pseudonymizer,
                processing_mode,
                _OUTPUT_DIR,
                output_path,
                "staged_bulk_export",
                collector,
            )
        else:
            processed = _run_staged_phase2(
                job,
                store,
                staging,
                settings,
                pseudonymizer,
                processing_mode,
                output_path,
                open_mode,
                staged_count,
                processed,
                "staged_bulk_export",
                collector,
            )

        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        job.result_path = publish_result(job, output_path, audit=summary_dict)
        checkpoint_data: dict = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        save_checkpoint(store, job, checkpoint_data)
        _persist_scoring_run(
            job, profile, summary_dict, "staged_bulk_export", collector
        )
        _log.info("staged_bulk_export_done job=%s processed=%d", job.id, processed)


def execute_cohort_staged(job, store, staging) -> None:
    """Staged two-phase cohort executor (synchronous  runs via asyncio.to_thread)."""
    from integrations.fhir.client import fetch_cohort, preflight_resource_count
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    search_type = params["search_type"]
    search_params_dict = params.get("search_params", {})
    everything_params = params.get("everything_params", {})
    token = resolve_source_token(params)
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
            job.result_path = publish_result(job, output_path)
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
                        inserted = staging.stage_batch(
                            job.id, buffer, fhir_source_url=server_url
                        )
                        phase1_state["staged_count"] += inserted
                        buffer.clear()

                if buffer:
                    inserted = staging.stage_batch(
                        job.id, buffer, fhir_source_url=server_url
                    )
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

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )
        _log.info(
            "staged_cohort_process_start job=%s processed=%d (overlap mode)",
            job.id,
            processed,
        )

        processed = _run_staged_phase2(
            job,
            store,
            staging,
            settings,
            pseudonymizer,
            processing_mode,
            output_path,
            "w",
            staged_count,
            processed,
            "staged_cohort",
            collector,
            phase1_done=phase1_done,
            phase1_state=phase1_state,
        )

        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

        staged_count = phase1_state.get("staged_count", staged_count)

        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        job.result_path = publish_result(job, output_path, audit=summary_dict)
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        save_checkpoint(store, job, checkpoint_data)
        _persist_scoring_run(job, profile, summary_dict, "staged_cohort", collector)
        _log.info("staged_cohort_done job=%s processed=%d", job.id, processed)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2 only (crash-resume: Phase 1 was already complete)
    # ════════════════════════════════════════════════════════════════════════
    elif phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_cohort_process_start job=%s processed=%d", job.id, processed)

        processed = _run_staged_phase2(
            job,
            store,
            staging,
            settings,
            pseudonymizer,
            processing_mode,
            output_path,
            open_mode,
            staged_count,
            processed,
            "staged_cohort",
            collector,
        )

        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        job.result_path = publish_result(job, output_path, audit=summary_dict)
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        save_checkpoint(store, job, checkpoint_data)
        _persist_scoring_run(job, profile, summary_dict, "staged_cohort", collector)
        _log.info("staged_cohort_done job=%s processed=%d", job.id, processed)


def execute_patient_export_staged(job, store, staging) -> None:
    """Staged two-phase patient $everything executor (synchronous  runs via asyncio.to_thread)."""
    from integrations.fhir.client import fetch_everything
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    patient_id = params["patient_id"]
    token = resolve_source_token(params)
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
        phase1_state: dict = {
            "staged_count": staged_count,
            "fetch_cursor": fetch_cursor,
        }
        phase1_done = threading.Event()
        phase1_exc: list = []

        def _run_phase1() -> None:
            _log.info(
                "staged_patient_fetch_start job=%s patient=%s", job.id, patient_id
            )
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
                        inserted = staging.stage_batch(
                            job.id, buffer, fhir_source_url=server_url
                        )
                        phase1_state["staged_count"] += inserted
                        buffer.clear()

                if buffer:
                    inserted = staging.stage_batch(
                        job.id, buffer, fhir_source_url=server_url
                    )
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

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )
        _log.info(
            "staged_patient_process_start job=%s processed=%d (overlap mode)",
            job.id,
            processed,
        )

        processed = _run_staged_phase2(
            job,
            store,
            staging,
            settings,
            pseudonymizer,
            processing_mode,
            output_path,
            "w",
            staged_count,
            processed,
            "staged_patient",
            collector,
            phase1_done=phase1_done,
            phase1_state=phase1_state,
        )

        phase1_thread.join()
        if phase1_exc:
            raise phase1_exc[0]

        staged_count = phase1_state.get("staged_count", staged_count)

        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        job.result_path = publish_result(job, output_path, audit=summary_dict)
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        save_checkpoint(store, job, checkpoint_data)
        _persist_scoring_run(
            job, profile, summary_dict, "staged_patient_export", collector
        )
        _log.info("staged_patient_export_done job=%s processed=%d", job.id, processed)

    # ════════════════════════════════════════════════════════════════════════
    # Phase 2 only (crash-resume: Phase 1 was already complete)
    # ════════════════════════════════════════════════════════════════════════
    elif phase == "processing":
        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info("staged_patient_process_start job=%s processed=%d", job.id, processed)

        processed = _run_staged_phase2(
            job,
            store,
            staging,
            settings,
            pseudonymizer,
            processing_mode,
            output_path,
            open_mode,
            staged_count,
            processed,
            "staged_patient",
            collector,
        )

        summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
        job.result_path = publish_result(job, output_path, audit=summary_dict)
        checkpoint_data = {
            "phase": "done",
            "staged_count": staged_count,
            "processed": processed,
            "summary": summary_dict,
        }
        save_checkpoint(store, job, checkpoint_data)
        _persist_scoring_run(
            job, profile, summary_dict, "staged_patient_export", collector
        )
        _log.info("staged_patient_export_done job=%s processed=%d", job.id, processed)


def execute_batch_patient_export_staged(job, store, staging) -> None:
    """Staged two-phase batch patient $everything executor (synchronous  runs via asyncio.to_thread)."""
    from integrations.fhir.client import fetch_patients_everything
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    patient_ids = params["patient_ids"]
    token = resolve_source_token(params)
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
    # Phase 1 + Phase 2 (overlapped)  normal first run
    # ════════════════════════════════════════════════════════════════════════
    collector = None
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
                            _log.info(
                                "staged_batch_patient_fetch_cancelled job=%s", job.id
                            )
                            return
                        inserted = staging.stage_batch(
                            job.id, buffer, fhir_source_url=server_url
                        )
                        phase1_state["staged_count"] += inserted
                        buffer.clear()

                if buffer:
                    inserted = staging.stage_batch(
                        job.id, buffer, fhir_source_url=server_url
                    )
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

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )
        _log.info(
            "staged_batch_patient_process_start job=%s processed=%d (overlap mode)",
            job.id,
            processed,
        )

        processed = _run_staged_phase2(
            job,
            store,
            staging,
            settings,
            pseudonymizer,
            processing_mode,
            output_path,
            "w",
            staged_count,
            processed,
            "staged_batch_patient",
            collector,
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

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )
        open_mode = "a" if processed > 0 else "w"
        if processed > 0:
            _truncate_to_lines(output_path, processed)
        _log.info(
            "staged_batch_patient_process_start job=%s processed=%d", job.id, processed
        )

        processed = _run_staged_phase2(
            job,
            store,
            staging,
            settings,
            pseudonymizer,
            processing_mode,
            output_path,
            open_mode,
            staged_count,
            processed,
            "staged_batch_patient",
            collector,
        )

    # ════════════════════════════════════════════════════════════════════════
    # Finalize  runs once after either branch completes (and not when the job
    # was already in "done" or other phase). Persists result_path so callers
    # can download via GET /v1/jobs/{id}/result instead of getting HTTP 410.
    # ════════════════════════════════════════════════════════════════════════
    if collector is None:
        _log.warning(
            "staged_batch_patient_unexpected_phase job=%s phase=%s  skipping finalize",
            job.id,
            phase,
        )
        return

    fresh = store.get(job.id)
    if fresh and fresh.status == JobStatus.CANCELLED:
        _log.info(
            "staged_batch_patient_cancelled_skip_finalize job=%s processed=%d",
            job.id,
            processed,
        )
        return

    if not os.path.exists(output_path):
        _log.warning(
            "staged_batch_patient_no_output job=%s path=%s  skipping finalize",
            job.id,
            output_path,
        )
        return

    summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
    job.result_path = publish_result(job, output_path, audit=summary_dict)
    checkpoint_data = {
        "phase": "done",
        "staged_count": staged_count,
        "processed": processed,
        "summary": summary_dict,
    }
    save_checkpoint(store, job, checkpoint_data)
    _persist_scoring_run(
        job, profile, summary_dict, "staged_batch_patient_export", collector
    )
    _log.info(
        "staged_batch_patient_export_done job=%s processed=%d result_path=%s",
        job.id,
        processed,
        job.result_path,
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

    collector = JobSummaryCollector(
        config_profile=profile, settings=settings, job_id=job.id
    )

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
        job,
        store,
        staging,
        settings,
        pseudonymizer,
        processing_mode,
        output_path,
        open_mode,
        0,
        processed,
        "staged_reprocess",
        collector,
        staging_job_id=source_job_id,
    )

    summary_dict = collector.to_dict(file_size_bytes=os.path.getsize(output_path))
    job.result_path = publish_result(job, output_path, audit=summary_dict)
    checkpoint_data = {
        "phase": "done",
        "processed": processed,
        "summary": summary_dict,
    }
    save_checkpoint(store, job, checkpoint_data)
    _persist_scoring_run(job, profile, summary_dict, "staged_reprocess", collector)
    _log.info("staged_reprocess_done job=%s processed=%d", job.id, processed)


# ---------------------------------------------------------------------------
# Risk-driven adaptive generalization executor
# ---------------------------------------------------------------------------
