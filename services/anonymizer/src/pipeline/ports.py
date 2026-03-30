"""Port interfaces for the anonymizer pipeline.

Re-exported from ``medanon_core.ports`` for backward compatibility.
Canonical definitions live in the shared library.
"""

from medanon_core.ports import (  # noqa: F401
    FhirClientPort,
    JobStorePort,
    NlpDetectorPort,
    PseudonymizerPort,
)
