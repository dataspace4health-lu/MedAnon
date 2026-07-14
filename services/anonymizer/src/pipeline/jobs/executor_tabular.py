"""Tabular-batch executor  de-identify many CSV/Excel/Parquet files as one job.

The user uploads N files of the same tabular format plus a saved config profile
(which must contain ``column:<name>`` rules).  At submit time the API stages the
uploaded files into a per-job directory under ``MEDANON_OUTPUT_DIR`` (shared
between the API and worker containers).  This executor reads each staged file,
de-identifies it through the column-rule engine, and writes all results into a
single ZIP archive that the user downloads when the job completes.

Job params (set by the submit endpoint):
    staged_dir       directory holding the uploaded files (under _OUTPUT_DIR)
    file_format      "csv" | "xlsx" | "parquet"
    config_profile   profile whose column: rules to apply
    file_names       original filenames, in upload order

Per-file isolation: a file that fails to parse/process is recorded as an
``<name>.error.txt`` entry in the ZIP rather than aborting the whole batch.
"""

from __future__ import annotations

import logging
import os
import zipfile

from domain.jobs import JobStatus
from pipeline.jobs.checkpoint import save_checkpoint
from pipeline.jobs.result_publisher import publish_result

_log = logging.getLogger("medanon.worker")


def _output_dir() -> str:
    """Resolve the result-output directory at call time (respects live env)."""
    return os.environ.get("MEDANON_OUTPUT_DIR", "/output")


def execute_tabular_batch(job, store, staging=None) -> None:
    """De-identify a batch of staged tabular files into one ZIP.

    Synchronous (runs in a worker thread).  Sets ``job.result_path`` to the
    stored ZIP and a summary checkpoint with per-file outcomes.
    """
    import json

    from pipeline.config.service import get_settings
    from pipeline.sources import (
        TabularAdapter,
        apply_column_rules,
        resolve_column_manifest,
    )

    params = job.params or {}
    staged_dir = params.get("staged_dir")
    file_format = (params.get("file_format") or "csv").lower()
    config_profile = params.get("config_profile") or "auto"
    file_names = params.get("file_names") or []

    if not staged_dir or not os.path.isdir(staged_dir):
        raise ValueError(f"staged_dir missing or not a directory: {staged_dir!r}")

    settings = get_settings(config_profile)

    save_checkpoint(
        store,
        job,
        {"phase": "processing", "staged_count": len(file_names), "processed": 0},
    )

    output_path = os.path.join(_output_dir(), f"{job.id}.zip")
    # The transformation manifest is released as a SEPARATE artifact (one line per
    # file: which columns got which rule), never mixed into the de-identified zip.
    manifest_path = f"{output_path}.manifest.ndjson"
    succeeded = 0
    failed = 0
    file_results: list[dict] = []

    with (
        zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf,
        open(manifest_path, "w", encoding="utf-8") as mfh,
    ):
        for idx, name in enumerate(file_names):
            # Cancellation check between files so a long batch stays responsive.
            fresh = store.get(job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                _log.info("tabular_batch_cancelled job=%s at=%d", job.id, idx)
                return

            src_path = os.path.join(staged_dir, f"{idx:06d}")
            try:
                with open(src_path, "rb") as fh:
                    raw = fh.read()
                adapter = TabularAdapter(file_format)
                rows = adapter.parse(raw)
                columns = list(rows[0].keys()) if rows else []
                apply_column_rules(rows, settings)
                out_bytes = adapter.serialize(rows)
                zf.writestr(_safe_member(name), out_bytes)
                mfh.write(
                    json.dumps(
                        {
                            "file": _safe_member(name),
                            "format": "tabular",
                            "transformations": resolve_column_manifest(
                                settings, columns
                            ),
                        }
                    )
                    + "\n"
                )
                succeeded += 1
                file_results.append({"file": name, "status": "ok"})
            except Exception as exc:
                _log.warning(
                    "tabular_batch_file_failed job=%s file=%s: %s", job.id, name, exc
                )
                zf.writestr(
                    f"{_safe_member(name)}.error.txt", f"Processing failed: {exc}"
                )
                failed += 1
                file_results.append(
                    {"file": name, "status": "error", "error": str(exc)}
                )

            if (idx + 1) % 5 == 0 or idx + 1 == len(file_names):
                save_checkpoint(
                    store,
                    job,
                    {
                        "phase": "processing",
                        "staged_count": len(file_names),
                        "processed": idx + 1,
                    },
                )

    summary_dict = {
        "total_files": len(file_names),
        "succeeded": succeeded,
        "failed": failed,
        "files": file_results,
    }
    job.result_path = publish_result(
        job, output_path, manifest_path=manifest_path, audit=summary_dict
    )

    # Best-effort cleanup of the staged inputs (results are now in the ZIP).
    _cleanup_staged_dir(staged_dir)

    save_checkpoint(
        store,
        job,
        {
            "phase": "done",
            "staged_count": len(file_names),
            "processed": succeeded + failed,
            "summary": summary_dict,
        },
    )
    _log.info(
        "tabular_batch_done job=%s total=%d ok=%d failed=%d",
        job.id,
        len(file_names),
        succeeded,
        failed,
    )


def _safe_member(name: str) -> str:
    """Sanitise a filename for safe use as a ZIP member (no path traversal)."""
    base = os.path.basename(str(name)) or "file"
    return base.replace("/", "_").replace("\\", "_")


def _cleanup_staged_dir(staged_dir: str) -> None:
    import shutil

    try:
        shutil.rmtree(staged_dir, ignore_errors=True)
    except Exception:
        _log.debug("tabular_batch_cleanup_failed dir=%s", staged_dir, exc_info=True)
