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
        _post_bundle_batch,
        _rewrite_references,
        _sanitise_resource_id,
    )
    from integrations.storage import get_result_storage
    from utils.thread_pool import get_executor

    params = job.params
    source_job_id = params.get("job_id")
    ndjson_path = params.get("ndjson_path")
    target_url = params["target_url"]
    target_token = params.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = float(params.get("timeout", 30))
    parallel = int(
        params.get("parallel", os.environ.get("MEDANON_UPLOAD_PARALLEL", "4"))
    )
    batch_size = int(
        params.get("batch_size", os.environ.get("MEDANON_UPLOAD_BATCH_SIZE", "500"))
    )

    if source_job_id:
        src = store.get(source_job_id)
        if src is None or not src.result_path:
            raise ValueError(f"Source job {source_job_id!r} not found or has no result")
        ndjson_path = src.result_path

    if not ndjson_path:
        raise ValueError("bulk-import job requires 'job_id' or 'ndjson_path' in params")

    save_checkpoint(store, job, {"phase": "loading"})
    base = target_url.rstrip("/")

    # Single-pass streaming scan.
    #
    # The previous implementation kept the entire NDJSON parsed in memory as
    # ``full_objs`` so that the topological tier helper and the ID-sanitisation
    # helper could iterate it twice.  For a 318k-resource export that means
    # holding ~318k Python dicts simultaneously — the dominant memory cost.
    #
    # The streaming variant below extracts only the small metadata each helper
    # actually needs (raw line, resourceType, id, raw reference types), then
    # drops the parsed dict immediately.  Memory now scales with #unique types
    # (~150 in FHIR R4) instead of #resources.
    storage = get_result_storage()
    lines_with_meta: list[tuple[str, str, str]] = []  # (raw_line, resourceType, id)
    present: set[str] = set()
    raw_deps: dict[str, set[str]] = {}  # rt -> set of *referenced bare types*
    id_map: dict[tuple[str, str], str] = {}

    def _scan_refs(value, dep_set: set[str]) -> None:
        """Mirror of _infer_upload_tiers' top-3-levels reference scan."""
        if isinstance(value, dict):
            ref = value.get("reference")
            if isinstance(ref, str) and "/" in ref:
                dep_set.add(ref.split("/", 1)[0])
            for v in value.values():
                if isinstance(v, dict):
                    r = v.get("reference")
                    if isinstance(r, str) and "/" in r:
                        dep_set.add(r.split("/", 1)[0])
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict):
                            r = item.get("reference")
                            if isinstance(r, str) and "/" in r:
                                dep_set.add(r.split("/", 1)[0])
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    r = item.get("reference")
                    if isinstance(r, str) and "/" in r:
                        dep_set.add(r.split("/", 1)[0])
                    for vv in item.values():
                        if isinstance(vv, dict):
                            r2 = vv.get("reference")
                            if isinstance(r2, str) and "/" in r2:
                                dep_set.add(r2.split("/", 1)[0])

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
            if not (
                isinstance(obj, dict) and obj.get("resourceType") and "error" not in obj
            ):
                continue
            rt = obj["resourceType"]
            rid = str(obj.get("id", ""))
            present.add(rt)
            lines_with_meta.append((line, rt, rid))
            # Collect dependency types for this resource (filtered against
            # ``present`` after the full pass; we don't yet know all types).
            dep_set = raw_deps.setdefault(rt, set())
            for v in obj.values():
                _scan_refs(v, dep_set)
            # Inline ID sanitisation: only record changed IDs to keep the map small.
            if rid:
                sanitised = _sanitise_resource_id(rid)
                if sanitised != rid:
                    id_map[(rt, rid)] = sanitised
            # ``obj`` goes out of scope here — reclaimed on next iteration.
    finally:
        if hasattr(stream, "close"):
            stream.close()

    # Compute upload tiers from the collected (present, deps) using the same
    # iterative-relaxation algorithm as _infer_upload_tiers.
    deps: dict[str, set[str]] = {
        rt: {d for d in raw_deps.get(rt, ()) if d in present and d != rt}
        for rt in present
    }
    tiers: dict[str, int] = {rt: 0 for rt in present}
    for _ in range(len(present)):
        updated = False
        for rt, dep_types in deps.items():
            if dep_types:
                required = max(tiers[d] for d in dep_types) + 1
                if required > tiers[rt]:
                    tiers[rt] = required
                    updated = True
        if not updated:
            break

    tier_raw: defaultdict[int, list[str]] = defaultdict(list)
    for raw_line, rt, _rid in lines_with_meta:
        tier_raw[tiers.get(rt, 0)].append(raw_line)
    del lines_with_meta
    total = sum(len(v) for v in tier_raw.values())

    _worker_log.info(
        "bulk_import_start job=%s source=%s total=%d tiers=%d parallel=%d batch_size=%d",
        job.id,
        source_job_id or ndjson_path,
        total,
        len(tier_raw),
        parallel,
        batch_size,
    )
    save_checkpoint(
        store, job, {"phase": "uploading", "lines_written": 0, "staged_count": total}
    )

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
                    error_details.append(
                        {
                            "resourceType": result.get("resourceType", "Unknown"),
                            "error": (result.get("error") or "unknown error")[:200],
                        }
                    )

    def _checkpoint_progress() -> None:
        save_checkpoint(
            store,
            job,
            {
                "phase": "uploading",
                "lines_written": uploaded + errors,
                "staged_count": total,
            },
        )

    for tier_level in sorted(tier_raw.keys()):
        tier_lines = tier_raw.pop(tier_level)
        chunks = [
            tier_lines[i : i + batch_size]
            for i in range(0, len(tier_lines), batch_size)
        ]
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

    save_checkpoint(
        store,
        job,
        {
            "phase": "done",
            "lines_written": uploaded + errors,
            "uploaded": uploaded,
            "staged_count": total,
            "errors": errors,
            "error_details": error_details,
        },
    )
    job.result_path = ndjson_path
    store.update(job)
    _worker_log.info(
        "bulk_import_done job=%s uploaded=%d errors=%d", job.id, uploaded, errors
    )

    # Surface a hard failure when no resource was successfully uploaded.  The
    # job runner above us treats raised exceptions as job failure; without this,
    # a 100 %-failure run would persist as ``status=done`` and the API would
    # serve up a misleadingly successful result.
    if total > 0 and uploaded == 0:
        raise RuntimeError(
            f"bulk_import_failed: 0/{total} resources uploaded "
            f"({errors} errors); see error_details in checkpoint"
        )
