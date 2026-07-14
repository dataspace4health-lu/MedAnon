"""Audit artifact  the machine-readable record released alongside every export.

One ``*.audit.json`` accompanies the de-identified data and the transformation
manifest in S3 (distinct ``audit/`` prefix, shared job-id stem). It answers *what
was released, from where, under which rules, and how safe it scored* without
containing any PHI: only the job's own metadata, resource-type counts, the
config profile, and the (already PHI-free) score summary + block report.

Kept separate from the manifest so the two can carry different access controls.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

AUDIT_SCHEMA = "medanon.audit/v1"

# Never copy secrets into the audit record; drop these params defensively.
_SECRET_PARAM_KEYS = frozenset(
    {"token", "target_token", "secret_key", "password", "authorization"}
)


def _toolkit_version() -> str:
    return os.environ.get("MEDANON_VERSION", "dev")


def _safe_params(params: dict) -> dict:
    """Job params with secrets stripped (audit is releasable, not privileged)."""
    return {
        k: v
        for k, v in (params or {}).items()
        # delivered_to is surfaced under `artifacts`; secrets are never included.
        if k not in _SECRET_PARAM_KEYS and k != "delivered_to"
    }


def build_audit(
    job,
    summary: dict | None = None,
    delivered: dict | None = None,
) -> dict:
    """Build the audit record for *job*.

    ``summary`` is the job summary (``JobSummaryCollector.to_dict()``) when the
    caller has it  its ``score``/``block_report`` are carried through. Falls
    back to the job's stored ``checkpoint_data['summary']`` otherwise.
    ``delivered`` is the ``{data,manifest,audit}`` s3 key map recorded on the job.
    """
    params = job.params or {}
    if summary is None:
        summary = (getattr(job, "checkpoint_data", None) or {}).get("summary") or {}

    audit = {
        "schema": AUDIT_SCHEMA,
        "job_id": job.id,
        "job_type": getattr(job, "type", None),
        "status": str(getattr(getattr(job, "status", None), "value", "")) or None,
        "created_at": getattr(job, "created_at", None),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "toolkit_version": _toolkit_version(),
        "config_profile": params.get("config_profile") or summary.get("config_profile"),
        "actor": params.get("actor") or params.get("subject"),
        "permit_id": params.get("permit_id"),
        "source": {
            "server_url": params.get("server_url"),
            "source_id": params.get("source_id"),
        },
        "artifacts": delivered or params.get("delivered_to") or {},
        "counts": {
            "total_resources": summary.get("total_resources"),
            "error_count": summary.get("error_count"),
            "by_resource_type": summary.get("resource_type_counts"),
        },
        "file_size_bytes": summary.get("file_size_bytes"),
        "compressed": summary.get("compressed"),
        # Already PHI-free (rule/coverage metrics, no values).
        "scoring": summary.get("score"),
        "block_report": summary.get("block_report"),
        "transformation_passport": summary.get("transformation_passport"),
        "request_params": _safe_params(params),
    }
    # Drop null top-level keys to keep the record compact.
    return {k: v for k, v in audit.items() if v not in (None, {}, [])}
