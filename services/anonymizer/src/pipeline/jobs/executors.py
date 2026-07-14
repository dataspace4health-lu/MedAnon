"""FHIR bulk-operation executors  public re-export facade.

Implementation is split across focused sub-modules:
    executor_stream   shared streaming infrastructure (pipeline, checkpoint writer)
    executor_export   export jobs: bulk-export, cohort, patient-export, reprocess
    executor_import   import job: bulk-import

All symbols are re-exported here so callers (worker.py, tests) continue to
import from this module without change.
"""

from __future__ import annotations

# Streaming infrastructure (used by staged_worker and tests)
from pipeline.jobs.executor_stream import (  # noqa: F401
    INFRA_RESOURCE_TYPES as _INFRA,
    CheckpointWriter as _CheckpointWriter,
    DeidentificationPipeline as _DeidentificationPipeline,
    process_with_bisect_fallback as _process_with_bisect_fallback,
    compress_ndjson as _compress_ndjson,
    cursor_tracking_gen as _cursor_tracking_gen,
    stream_and_deidentify as _stream_and_deidentify,
    skip_to as _skip_to,
)

# Export executors
from pipeline.jobs.executor_export import (  # noqa: F401
    _execute_batch_patient_export,
    _execute_bulk_export,
    _execute_cohort,
    _execute_patient_export,
    _execute_reprocess,
)

# Import executor
from pipeline.jobs.executor_import import _execute_bulk_import  # noqa: F401

# Risk-driven adaptive generalization executor
from pipeline.jobs.staged_worker import (  # noqa: F401
    execute_risk_driven_export_staged as _execute_risk_driven_export,
)
