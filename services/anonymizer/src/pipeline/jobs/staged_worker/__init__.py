"""staged_worker  two-phase staged bulk-export and cohort executors.

Phase 1  Fetch:   Stream FHIR resources into ``medanon.staged_resources``
                   (PostgreSQL) with pagination-cursor checkpoints for crash
                   recovery without re-fetching.

Phase 2  Process: Read pending rows in batches, run gPAS/NLP de-identification
                   once per batch, write NDJSON, atomically mark rows done.

Package layout:
    _core.py        shared constants and private helpers
    _executors.py   standard executors (bulk, cohort, patient, reprocess)
    _risk.py        risk-driven k-anonymity executor

Public API:
    execute_bulk_export_staged(job, store, staging)
    execute_cohort_staged(job, store, staging)
    execute_patient_export_staged(job, store, staging)
    execute_batch_patient_export_staged(job, store, staging)
    execute_reprocess_staged(job, store, staging)
    execute_risk_driven_export_staged(job, store, staging)
"""

from pipeline.jobs.staged_worker._executors import (
    execute_bulk_export_staged,
    execute_cohort_staged,
    execute_patient_export_staged,
    execute_batch_patient_export_staged,
    execute_reprocess_staged,
)

try:
    from pipeline.jobs.staged_worker._risk import (
        execute_risk_driven_export_staged,
    )
except ImportError:

    def execute_risk_driven_export_staged(job, store, staging):
        raise NotImplementedError(
            "risk-driven export requires pipeline.jobs.staged_worker._risk "
            "which is not installed in this build"
        )


__all__ = [
    "execute_bulk_export_staged",
    "execute_cohort_staged",
    "execute_patient_export_staged",
    "execute_batch_patient_export_staged",
    "execute_reprocess_staged",
    "execute_risk_driven_export_staged",
]
