"""FHIR bulk-import job executor.

Reads a completed NDJSON export, computes topological upload tiers, and
posts resources to a target FHIR server using parallel batch Bundles.
"""

from __future__ import annotations

import logging
import os

from domain.jobs import Job
from pipeline.jobs.checkpoint import save_checkpoint
from utils.json_fast import loads as _json_loads

_worker_log = logging.getLogger("medanon.worker")


def _execute_bulk_import(job: Job, store, staging) -> None:
    """Read a completed NDJSON result and upload resources to a target FHIR server.

    Accepts job params:
        ``job_id``       — source export Job whose result_path is used.
        ``ndjson_path``  — explicit NDJSON path (local or s3://); used when job_id absent.
        ``target_url``   — target FHIR server base URL (required).
        ``target_token`` — bearer token for the target (optional).
        ``timeout``      — per-request HTTP timeout in seconds (default 30).
        ``parallel``     — concurrent FHIR batch Bundle POSTs per tier (default 4).
        ``batch_size``   — resources per FHIR batch Bundle (default 500).

    Streams the NDJSON in a single read pass to extract (resourceType, id) metadata
    for topological tier ordering and ID sanitisation, keeping raw JSON strings
    bucketed by tier.  Each tier is parsed and uploaded sequentially so only one
    tier's dict objects reside in memory at a time.
    """
    from collections import defaultdict
    from concurrent.futures import FIRST_COMPLETED, wait as cf_wait

    from integrations.fhir.writer import (
        _compute_id_map,
        _infer_upload_tiers,
        _post_bundle_batch,
        _rewrite_references,
    )
    from integrations.storage import get_result_storage
    from utils.thread_pool import get_executor

    params = job.params
    source_job_id = params.get("job_id")
    ndjson_path = params.get("ndjson_path")
    target_url = params["target_url"]
    target_token = params.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = float(params.get("timeout", 30))
    parallel = int(params.get("parallel", os.environ.get("MEDANON_UPLOAD_PARALLEL", "4")))
    batch_size = int(params.get("batch_size", os.environ.get("MEDANON_UPLOAD_BATCH_SIZE", "500")))

    if source_job_id:
        src = store.get(source_job_id)
        if src is None or not src.result_path:
            raise ValueError(f"Source job {source_job_id!r} not found or has no result")
        ndjson_path = src.result_path

    if not ndjson_path:
        raise ValueError("bulk-import job requires 'job_id' or 'ndjson_path' in params")

    save_checkpoint(store, job, {"phase": "loading"})
    base = target_url.rstrip("/")

    # Single-pass scan: keep raw line strings + full parsed objects for tier/id_map
    # computation.  Full objects are needed so _infer_upload_tiers can see reference
    # fields and produce the correct topological upload order (Patients before
    # Observations, Encounters before Observations, etc.).  After tier+id_map are
    # computed we drop the parsed objects; only raw strings are kept for upload.
    storage = get_result_storage()
    lines_with_meta: list[tuple[str, str, str]] = []  # (raw_line, resourceType, id)
    full_objs: list[dict] = []  # kept only until tiers/id_map are computed
    stream = storage.open_stream(ndjson_path)
    try:
        for raw_line in stream:
            line = (
                raw_line.decode("utf-8")
                if isinstance(raw_line, (bytes, bytearray))
                else raw_line
            )
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json_loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict) and obj.get("resourceType") and "error" not in obj:
                lines_with_meta.append((line, obj["resourceType"], str(obj.get("id", ""))))
                full_objs.append(obj)
    finally:
        if hasattr(stream, "close"):
            stream.close()

    tiers = _infer_upload_tiers(full_objs)
    id_map = _compute_id_map(full_objs)
    del full_objs  # release memory before upload phase

    tier_raw: defaultdict[int, list[str]] = defaultdict(list)
    for raw_line, rt, _rid in lines_with_meta:
        tier_raw[tiers.get(rt, 0)].append(raw_line)
    del lines_with_meta
    total = sum(len(v) for v in tier_raw.values())

    _worker_log.info(
        "bulk_import_start job=%s source=%s total=%d tiers=%d parallel=%d batch_size=%d",
        job.id, source_job_id or ndjson_path, total, len(tier_raw), parallel, batch_size,
    )
    save_checkpoint(store, job, {"phase": "uploading", "lines_written": 0, "staged_count": total})

    def _parse_and_rewrite(raw: str) -> dict:
        obj = _json_loads(raw)
        if id_map:
            obj = _rewrite_references(obj, id_map)
        return obj

    def _upload_chunk(chunk_lines: list[str]) -> list[dict]:
        return _post_bundle_batch(
            base, [_parse_and_rewrite(r) for r in chunk_lines], target_token, timeout
        )

    _MAX_ERROR_DETAILS = 50  # cap stored error details to avoid bloating the checkpoint
    uploaded = errors = 0
    error_details: list[dict] = []  # [{resourceType, error}] — first N failures
    pool = get_executor() if parallel > 1 else None

    def _tally(results: list[dict]) -> None:
        """Count results and collect up to _MAX_ERROR_DETAILS error samples."""
        nonlocal uploaded, errors
        for result in results:
            if result.get("success"):
                uploaded += 1
            else:
                errors += 1
                if len(error_details) < _MAX_ERROR_DETAILS:
                    error_details.append({
                        "resourceType": result.get("resourceType", "Unknown"),
                        "error": (result.get("error") or "unknown error")[:200],
                    })

    def _checkpoint_progress() -> None:
        save_checkpoint(store, job, {
            "phase": "uploading",
            "lines_written": uploaded + errors,
            "staged_count": total,
        })

    for tier_level in sorted(tier_raw.keys()):
        tier_lines = tier_raw.pop(tier_level)
        chunks = [tier_lines[i: i + batch_size] for i in range(0, len(tier_lines), batch_size)]
        del tier_lines

        if pool is not None and len(chunks) > 1:
            # Sliding window: keep `parallel` futures in-flight, drain on each fill.
            # Checkpoint after each drain so the frontend sees progress every
            # ~(parallel × batch_size) resources rather than once per tier.
            active: set = set()
            for chunk in chunks:
                fut = pool.submit(_upload_chunk, chunk)
                active.add(fut)
                if len(active) >= parallel:
                    done_futs, active = cf_wait(active, return_when=FIRST_COMPLETED)
                    for f in done_futs:
                        _tally(f.result())
                    _checkpoint_progress()
            # Drain remaining futures
            done_futs, _ = cf_wait(active)
            for f in done_futs:
                _tally(f.result())
            _checkpoint_progress()
        else:
            # Sequential: checkpoint after every chunk (= batch_size resources).
            for chunk in chunks:
                _tally(_upload_chunk(chunk))
                _checkpoint_progress()

    save_checkpoint(store, job, {
        "phase": "done",
        "lines_written": uploaded + errors,
        "uploaded": uploaded,
        "staged_count": total,
        "errors": errors,
        "error_details": error_details,
    })
    job.result_path = ndjson_path
    store.update(job)
    _worker_log.info("bulk_import_done job=%s uploaded=%d errors=%d", job.id, uploaded, errors)
