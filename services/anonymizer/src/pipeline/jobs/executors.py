"""FHIR bulk-operation executors — public re-export facade.

Implementation is split across focused sub-modules:
    executor_stream  — shared streaming infrastructure (pipeline, checkpoint writer)
    executor_export  — export jobs: bulk-export, cohort, patient-export, reprocess
    executor_import  — import job: bulk-import

All symbols are re-exported here so callers (worker.py, tests) continue to
import from this module without change.
"""

from __future__ import annotations

# Streaming infrastructure (used by staged_worker and tests)
from pipeline.jobs.executor_stream import (  # noqa: F401
    INFRA_RESOURCE_TYPES as _INFRA,
    AsyncCheckpointWriter as _AsyncCheckpointWriter,
    PipelinedProcessor as _PipelinedProcessor,
    batch_or_bisect as _batch_or_bisect,
    compress_ndjson as _compress_ndjson,
    cursor_tracking_gen as _cursor_tracking_gen,
    process_stream_chunked as _process_stream_chunked,
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
