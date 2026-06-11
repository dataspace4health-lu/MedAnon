"""FHIR export job executors: bulk-export, cohort, patient-export, batch-patient-export, reprocess.

Each function is a synchronous callable that runs inside ``asyncio.to_thread``
in the worker loop.  When a staging store is configured, execution delegates to
the two-phase staged path; otherwise the streaming fallback is used.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from domain.jobs import Job
from pipeline.jobs.checkpoint import (
    _truncate_to_lines,
    load_checkpoint,
    save_checkpoint,
)
from pipeline.jobs.executor_stream import (
    INFRA_RESOURCE_TYPES,
    compress_ndjson,
    cursor_tracking_gen,
    stream_and_deidentify,
    skip_to,
    _COMPRESS_RESULTS,
)

_worker_log = logging.getLogger("medanon.worker")
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")

# Jobs with an estimated row count *below* this threshold skip the two-phase
# staged path and use the fast stream executor instead.  The stream path still
# supports crash-resume at FHIR pagination granularity; the staged path adds
# per-row resume and cross-pod parallelism which only pays off for large jobs.
# Set to 0 to always use staging when a store is configured.
_STAGED_THRESHOLD_ROWS: int = int(
    os.environ.get("MEDANON_STAGED_THRESHOLD_ROWS", "500000")
)


_PREFLIGHT_PARALLEL = int(os.environ.get("MEDANON_FHIR_FETCH_PARALLEL", "4"))


def _filter_nonempty_types(
    candidate_types: list[str],
    server_url: str,
    token: str | None,
    timeout: float,
) -> list[str]:
    """Return only resource types that have at least one resource on the server.

    Runs one lightweight ``GET /{type}?_summary=count&_count=0`` request per
    type in parallel (up to ``MEDANON_FHIR_FETCH_PARALLEL`` threads).  This
    typically takes 2–5 s regardless of the total type list length and eliminates
    dozens of wasted paginated fetches for empty resource types.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from integrations.fhir.reader import preflight_resource_count

    nonempty: list[str] = []

    def _check(rt: str) -> tuple[str, int]:
        try:
            count = preflight_resource_count(
                server_url, resource_type=rt, token=token, timeout=5
            )
        except Exception:
            count = -1  # treat unknown as non-empty to be safe
        return rt, count

    with ThreadPoolExecutor(max_workers=_PREFLIGHT_PARALLEL) as pool:
        futures = {pool.submit(_check, rt): rt for rt in candidate_types}
        for fut in as_completed(futures):
            rt, count = fut.result()
            if count != 0:  # -1 (unknown) or >0 → include
                nonempty.append(rt)

    # Preserve original capability-statement order for deterministic checkpoints.
    order = {rt: i for i, rt in enumerate(candidate_types)}
    nonempty.sort(key=lambda rt: order.get(rt, 9999))
    _worker_log.info(
        "bulk_export_preflight kept=%d / total=%d resource types",
        len(nonempty),
        len(candidate_types),
    )
    return nonempty


def _cleanup_blocked_output(
    job: Job,
    output_path: str,
    audit_path: str | None = None,
) -> None:
    """Delete output files and clear job.result_path when the score gate blocks."""
    from pathlib import Path
    from integrations.storage import delete_result

    if job.result_path:
        delete_result(job.result_path)
        job.result_path = None

    # Remove the local file too (for S3 path, the local copy may still exist)
    try:
        Path(output_path).unlink(missing_ok=True)
    except OSError:
        pass

    if audit_path:
        try:
            Path(audit_path).unlink(missing_ok=True)
        except OSError:
            pass

    _worker_log.info("score_gate_cleanup job=%s output_deleted=True", job.id)


def _use_staged(staging, estimated_rows: int | None) -> bool:
    """Return True when the staged path should be used for this job."""
    if staging is None:
        return False
    if _STAGED_THRESHOLD_ROWS <= 0:
        return True  # always staged if threshold explicitly disabled
    if estimated_rows is not None and estimated_rows < _STAGED_THRESHOLD_ROWS:
        _worker_log.info(
            "staged_threshold_skip estimated_rows=%d threshold=%d — using stream path",
            estimated_rows,
            _STAGED_THRESHOLD_ROWS,
        )
        return False
    return True


def _secure_open(path: str, mode: str, **kw):
    """Open *path* for writing with owner-only permissions (mode 0o600).

    Using os.open with O_CREAT+explicit mode ensures the file is never
    world-readable even for a brief moment — unlike open() + chmod().
    Falls back to regular open() for read/append modes not covered by O_CREAT.
    """
    if "w" in mode and "r" not in mode:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(path, flags, 0o600)
        return open(fd, mode, **kw)
    if "a" in mode:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(path, flags, 0o600)
        return open(fd, mode, **kw)
    return open(path, mode, **kw)


def _mkdir_secure(path: str) -> None:
    """Create directory with 0o700 permissions (owner-only)."""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    try:
        p.chmod(0o700)
    except OSError:
        pass  # read-only filesystem or insufficient permissions — best effort


def _execute_bulk_export(job: Job, store, staging) -> None:
    """Run a bulk-export job synchronously, resuming from checkpoint when available."""
    estimated_rows = job.params.get("estimated_rows")
    if staging is not None and estimated_rows is None:
        # Fast preflight: GET /Patient?_summary=count&_count=0 as a proxy for
        # total job size.  Takes ~100 ms and avoids staging overhead for small
        # servers.  Multiply by a conservative factor (15×) to account for
        # Observation/Condition/Procedure/etc. resources per patient.
        try:
            from integrations.fhir.reader import preflight_resource_count

            _token = job.params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
            _patient_count = preflight_resource_count(
                job.params["server_url"],
                resource_type="Patient",
                token=_token,
                timeout=5,
            )
            if _patient_count > 0:
                estimated_rows = _patient_count * 15
                _worker_log.info(
                    "bulk_export_estimated_rows job=%s patients=%d estimated=%d",
                    job.id,
                    _patient_count,
                    estimated_rows,
                )
        except Exception:
            pass  # preflight failure → _use_staged will default to True (safe)

    if _use_staged(staging, estimated_rows):
        from pipeline.jobs.staged_worker import execute_bulk_export_staged

        return execute_bulk_export_staged(job, store, staging)

    from integrations.fhir.client import (
        fetch_all_resource_types,
        get_capability_statement,
    )
    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer

    params = job.params
    server_url = params["server_url"]
    resource_type = params.get("resource_type")
    group_id = params.get("group_id")
    level = params.get("level", "system")
    type_filter = params.get("type_filter")
    since = params.get("since")
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    settings = get_settings(profile)
    pseudonymizer = _get_default_pseudonymizer()

    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")
    _mkdir_secure(_OUTPUT_DIR)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    fhir_cursor = checkpoint.get("fhir_cursor")  # pagination cursor for crash-resume
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)
        _worker_log.info(
            "bulk_export_resume job=%s from_line=%d cursor=%s",
            job.id,
            already_written,
            f"rt={fhir_cursor['current_rt']} page={fhir_cursor['page_url']}"
            if fhir_cursor
            else "none",
        )

    # ── Group-level export: use FHIR Bulk Data API (Group/{id}/$export) ────────
    if level == "group" or group_id:
        if not group_id:
            raise ValueError("group_id is required for group-level bulk export")
        from integrations.fhir.bulk import bulk_export as fhir_bulk_export

        _t0 = time.monotonic()
        save_checkpoint(
            store, job, {"phase": "fetching", "lines_written": already_written}
        )
        _worker_log.info("bulk_export_group_start job=%s group_id=%s", job.id, group_id)

        from pipeline.jobs.summary import JobSummaryCollector

        collector = JobSummaryCollector(
            config_profile=profile, settings=settings, job_id=job.id
        )

        raw_gen = fhir_bulk_export(
            server_url,
            level="group",
            group_id=group_id,
            type_filter=type_filter,
            since=since,
            token=token,
            timeout=timeout,
        )

        with _secure_open(output_path, open_mode, encoding="utf-8") as fh:
            count, cancelled = stream_and_deidentify(
                skip_to(raw_gen, already_written),
                settings,
                pseudonymizer,
                fh,
                already_written,
                store,
                job,
                "bulk_export",
                summary=collector,
                cursor_state={},
            )

        if not cancelled:
            if _COMPRESS_RESULTS:
                output_path = compress_ndjson(output_path)
            from integrations.storage import store_result

            job.result_path = store_result(job.id, output_path)
            summary_dict = collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=_COMPRESS_RESULTS,
            )
            save_checkpoint(
                store,
                job,
                {
                    "phase": "done",
                    "lines_written": count,
                    "summary": summary_dict,
                },
            )
            _worker_log.info("bulk_export_group_done job=%s count=%d", job.id, count)
        return

    # ── System/type-level export: FHIR resource-search path ─────────────────
    if resource_type:
        resource_types = [resource_type]
    elif type_filter:
        resource_types = [t.strip() for t in type_filter.split(",") if t.strip()]
    else:
        try:
            all_types = get_capability_statement(
                server_url, token=token, timeout=timeout
            )
            candidate_types = [t for t in all_types if t not in INFRA_RESOURCE_TYPES]
            # Skip resource types that have no data — avoids paginating through
            # dozens of empty FHIR R4 types that the capability statement lists
            # but the server has never received data for.  Runs one lightweight
            # _summary=count request per type in parallel (same pool size as the
            # FHIR fetch pool) so the preflight finishes in seconds.
            resource_types = _filter_nonempty_types(
                candidate_types, server_url, token, timeout
            )
        except Exception as exc:
            from integrations.fhir._transport import FhirCircuitBreakerOpen

            if isinstance(exc, FhirCircuitBreakerOpen):
                raise
            _worker_log.warning(
                "bulk_export capability_statement_failed job=%s: %s", job.id, exc
            )
            resource_types = [
                "Patient",
                "Observation",
                "Condition",
                "Encounter",
                "Procedure",
            ]

    if not resource_types:
        _worker_log.info(
            "bulk_export_empty job=%s — no resource types to export", job.id
        )
        _secure_open(output_path, "w").close()
        from integrations.storage import store_result

        job.result_path = store_result(job.id, output_path)
        save_checkpoint(store, job, {"phase": "done", "lines_written": 0})
        return

    extra_params: dict = {}
    if since:
        extra_params["_lastUpdated"] = f"ge{since}"

    _t0 = time.monotonic()
    save_checkpoint(store, job, {"phase": "fetching", "lines_written": already_written})

    _fhir_parallel = int(os.environ.get("MEDANON_FHIR_FETCH_PARALLEL", "4"))
    _cursor_state: dict = {}

    if _fhir_parallel > 1:
        # Parallel mode: all resource types are fetched concurrently.
        # Empty types (260 of them in a typical FHIR R4 server) return in ~10ms
        # and release their thread slots immediately.  Heavy types like Observation
        # (132 pages) run in parallel with Condition, DiagnosticReport, etc. instead
        # of waiting for them to finish serially.
        #
        # Trade-off: no per-type FHIR page cursor.  On crash-resume the completed
        # resource types (completed_rts from the last checkpoint) are skipped; the
        # rest are re-fetched from the beginning.  Already-written lines are skipped
        # by the lines_written counter (no double-processing).
        _completed_rts = set((fhir_cursor or {}).get("completed_rts") or [])
        _raw_gen = fetch_all_resource_types(
            server_url,
            resource_types,
            params=extra_params if extra_params else None,
            token=token,
            timeout=timeout,
            completed_rts=_completed_rts,
            yield_cursors=False,
        )
        resource_gen = (resource for _rt, resource in _raw_gen)
        resume_skip = already_written
    else:
        # Serial mode: full FHIR page cursor tracking so a crash mid-type resumes
        # from the exact page instead of re-fetching the entire type.
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
        resource_gen = cursor_tracking_gen(cursor_aware_gen, _cursor_state)
        resume_skip = (fhir_cursor or {}).get("page_offset", already_written)

    from pipeline.jobs.summary import JobSummaryCollector

    collector = JobSummaryCollector(
        config_profile=profile, settings=settings, job_id=job.id
    )

    with _secure_open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = stream_and_deidentify(
            skip_to(resource_gen, resume_skip),
            settings,
            pseudonymizer,
            fh,
            already_written,
            store,
            job,
            "bulk_export",
            summary=collector,
            cursor_state=_cursor_state,
        )

    if not cancelled:
        # Compute the summary from the (local, streamed) output file BEFORE
        # promoting it to the durable result store.  The score gate runs here,
        # pre-publish, so a blocked job never calls store_result — eliminating
        # the former write-then-delete race (publish, then delete from both the
        # durable store and local disk).  On block we only remove the local
        # staging file; job.result_path was never set.
        summary_dict = collector.to_dict(
            file_size_bytes=os.path.getsize(output_path),
            compressed=False,
        )

        audit_report = collector.generate_audit_report(
            export_meta={"fhir_source": params.get("server_url", "")}
        )
        audit_path: str | None = None
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as fh:
                    fh.write(audit_report)
            except Exception:
                _worker_log.debug(
                    "bulk_export_audit_write_failed job=%s", job.id, exc_info=True
                )
                audit_path = None

        # Score gate (pre-publish): if quality is below threshold or PII leaked,
        # fail the job with a plain-language explanation and never publish.
        from pipeline.scoring.gate import ScoreGateBlocked, check_score_gate

        try:
            check_score_gate(summary_dict.get("score"), profile)
        except ScoreGateBlocked:
            _cleanup_blocked_output(job, output_path, audit_path)
            raise

        # Gate passed — now (and only now) promote the output to the durable
        # store and finalise the job.
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
            # Refresh the file-size on the summary after compression.
            summary_dict = collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=True,
            )
        from integrations.storage import store_result

        job.result_path = store_result(job.id, output_path)

        checkpoint_data: dict = {
            "phase": "done",
            "lines_written": count,
            "summary": summary_dict,
        }
        if audit_path:
            checkpoint_data["score_audit_path"] = audit_path

        save_checkpoint(store, job, checkpoint_data)
        try:
            from api.services.scoring_helpers import persist_run_sync

            persist_run_sync(
                endpoint="bulk_export",
                config_profile=profile,
                resource_count=summary_dict.get("total_resources", count),
                error_count=summary_dict.get("error_count", 0),
                duration_ms=int((time.monotonic() - _t0) * 1000),
                input_type="ndjson",
                summary=summary_dict,
                score=summary_dict.get("score"),
            )
        except Exception:
            _worker_log.debug("bulk_export_persist_run_failed", exc_info=True)
        _worker_log.info("bulk_export_done job=%s count=%d", job.id, count)


def _execute_cohort(job: Job, store, staging) -> None:
    """Run a cohort export job synchronously, resuming from checkpoint when available."""
    estimated_rows = job.params.get("estimated_rows")
    if staging is not None and estimated_rows is None:
        try:
            from integrations.fhir.reader import preflight_resource_count

            _token = job.params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
            _count = preflight_resource_count(
                job.params["server_url"],
                resource_type=job.params.get("search_type"),
                token=_token,
                timeout=5,
            )
            if _count > 0:
                estimated_rows = (
                    _count * 10
                )  # cohort: fewer linked resources than system export
                _worker_log.info(
                    "cohort_estimated_rows job=%s preflight=%d estimated=%d",
                    job.id,
                    _count,
                    estimated_rows,
                )
        except Exception:
            pass
    if _use_staged(staging, estimated_rows):
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
    _mkdir_secure(_OUTPUT_DIR)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)
        _worker_log.info("cohort_resume job=%s from_line=%d", job.id, already_written)

    if already_written == 0:
        count = preflight_resource_count(
            server_url, resource_type=search_type, token=token
        )
        if count == 0:
            _worker_log.info(
                "cohort_empty job=%s — no %s resources found, skipping export",
                job.id,
                search_type,
            )
            _secure_open(output_path, "w").close()
            from integrations.storage import store_result

            job.result_path = store_result(job.id, output_path)
            save_checkpoint(store, job, {"phase": "done", "lines_written": 0})
            return

    _t0 = time.monotonic()
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

    collector = JobSummaryCollector(
        config_profile=profile, settings=settings, job_id=job.id
    )

    with _secure_open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = stream_and_deidentify(
            skip_to(gen, already_written),
            settings,
            pseudonymizer,
            fh,
            already_written,
            store,
            job,
            "cohort",
            summary=collector,
        )

    if not cancelled:
        # Validate BEFORE promoting to the durable store (see the bulk-export
        # path for the rationale — pre-publish gate eliminates write-then-delete).
        summary_dict = collector.to_dict(
            file_size_bytes=os.path.getsize(output_path),
            compressed=False,
        )
        audit_report = collector.generate_audit_report(
            export_meta={"fhir_source": server_url}
        )
        cohort_audit_path: str | None = None
        if audit_report:
            try:
                cohort_audit_path = os.path.join(
                    _OUTPUT_DIR, f"{job.id}_score_audit.md"
                )
                with open(cohort_audit_path, "w", encoding="utf-8") as fh:
                    fh.write(audit_report)
            except Exception:
                _worker_log.debug(
                    "cohort_audit_write_failed job=%s", job.id, exc_info=True
                )
                cohort_audit_path = None

        from pipeline.scoring.gate import ScoreGateBlocked, check_score_gate

        try:
            check_score_gate(summary_dict.get("score"), profile)
        except ScoreGateBlocked:
            _cleanup_blocked_output(job, output_path, cohort_audit_path)
            raise

        # Gate passed — promote to the durable store and finalise.
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
            summary_dict = collector.to_dict(
                file_size_bytes=os.path.getsize(output_path),
                compressed=True,
            )
        from integrations.storage import store_result

        job.result_path = store_result(job.id, output_path)

        checkpoint_data = {
            "phase": "done",
            "lines_written": count,
            "summary": summary_dict,
        }
        if cohort_audit_path:
            checkpoint_data["score_audit_path"] = cohort_audit_path

        save_checkpoint(store, job, checkpoint_data)
        try:
            from api.services.scoring_helpers import persist_run_sync

            persist_run_sync(
                endpoint="cohort",
                config_profile=profile,
                resource_count=summary_dict.get("total_resources", count),
                error_count=summary_dict.get("error_count", 0),
                duration_ms=int((time.monotonic() - _t0) * 1000),
                input_type="ndjson",
                summary=summary_dict,
                score=summary_dict.get("score"),
            )
        except Exception:
            _worker_log.debug("cohort_persist_run_failed", exc_info=True)
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
    _mkdir_secure(_OUTPUT_DIR)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)

    _t0 = time.monotonic()
    save_checkpoint(store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_everything(
        server_url, "Patient", patient_id, token=token, timeout=timeout
    )

    from pipeline.jobs.summary import JobSummaryCollector

    collector = JobSummaryCollector(
        config_profile=profile, settings=settings, job_id=job.id
    )

    with _secure_open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = stream_and_deidentify(
            skip_to(gen, already_written),
            settings,
            pseudonymizer,
            fh,
            already_written,
            store,
            job,
            "patient_export",
            summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
        from integrations.storage import store_result

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(
            file_size_bytes=os.path.getsize(output_path),
            compressed=_COMPRESS_RESULTS,
        )
        checkpoint_data = {
            "phase": "done",
            "lines_written": count,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report(
            export_meta={"fhir_source": server_url, "patient_id": patient_id}
        )
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as fh:
                    fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _worker_log.debug(
                    "patient_export_audit_write_failed job=%s", job.id, exc_info=True
                )
        save_checkpoint(store, job, checkpoint_data)
        try:
            from api.services.scoring_helpers import persist_run_sync

            persist_run_sync(
                endpoint="patient_export",
                config_profile=profile,
                resource_count=summary_dict.get("total_resources", count),
                error_count=summary_dict.get("error_count", 0),
                duration_ms=int((time.monotonic() - _t0) * 1000),
                input_type="ndjson",
                summary=summary_dict,
                score=summary_dict.get("score"),
            )
        except Exception:
            _worker_log.debug("patient_export_persist_run_failed", exc_info=True)
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
    _mkdir_secure(_OUTPUT_DIR)

    checkpoint = load_checkpoint(job) or {}
    already_written = checkpoint.get("lines_written", 0)
    open_mode = "a" if already_written > 0 else "w"
    if already_written:
        _truncate_to_lines(output_path, already_written)
        _worker_log.info(
            "batch_patient_resume job=%s from_line=%d", job.id, already_written
        )

    _t0 = time.monotonic()
    save_checkpoint(store, job, {"phase": "fetching", "lines_written": already_written})
    gen = fetch_patients_everything(
        server_url, patient_ids, token=token, timeout=timeout
    )

    from pipeline.jobs.summary import JobSummaryCollector

    collector = JobSummaryCollector(
        config_profile=profile, settings=settings, job_id=job.id
    )

    with _secure_open(output_path, open_mode, encoding="utf-8") as fh:
        count, cancelled = stream_and_deidentify(
            skip_to(gen, already_written),
            settings,
            pseudonymizer,
            fh,
            already_written,
            store,
            job,
            "batch_patient_export",
            summary=collector,
        )

    if not cancelled:
        if _COMPRESS_RESULTS:
            output_path = compress_ndjson(output_path)
        from integrations.storage import store_result

        job.result_path = store_result(job.id, output_path)
        summary_dict = collector.to_dict(
            file_size_bytes=os.path.getsize(output_path),
            compressed=_COMPRESS_RESULTS,
        )
        checkpoint_data = {
            "phase": "done",
            "lines_written": count,
            "summary": summary_dict,
        }
        audit_report = collector.generate_audit_report(
            export_meta={"fhir_source": server_url}
        )
        if audit_report:
            try:
                audit_path = os.path.join(_OUTPUT_DIR, f"{job.id}_score_audit.md")
                with open(audit_path, "w", encoding="utf-8") as fh:
                    fh.write(audit_report)
                checkpoint_data["score_audit_path"] = audit_path
            except Exception:
                _worker_log.debug(
                    "batch_patient_export_audit_write_failed job=%s",
                    job.id,
                    exc_info=True,
                )
        save_checkpoint(store, job, checkpoint_data)
        try:
            from api.services.scoring_helpers import persist_run_sync

            persist_run_sync(
                endpoint="batch_patient_export",
                config_profile=profile,
                resource_count=summary_dict.get("total_resources", count),
                error_count=summary_dict.get("error_count", 0),
                duration_ms=int((time.monotonic() - _t0) * 1000),
                input_type="ndjson",
                summary=summary_dict,
                score=summary_dict.get("score"),
            )
        except Exception:
            _worker_log.debug("batch_patient_export_persist_run_failed", exc_info=True)
        _worker_log.info(
            "batch_patient_export_done job=%s patients=%d count=%d",
            job.id,
            len(patient_ids),
            count,
        )
