"""Source-format adapters — one normalization seam for all input formats.

Every input format (FHIR JSON/NDJSON/XML today; CDA, HL7v2, DICOM, tabular in
future phases) is parsed into the common intermediate representation the rule
engine already understands (FHIR-shaped dicts), processed by the *same*
``process_data_batch`` pipeline, then serialized back to its source format.

The :class:`DataSourceAdapter` Protocol defines that seam; :class:`FhirAdapter`
is the identity adapter (FHIR is already the IR).  Non-FHIR adapters are added
in later phases and map their format onto FHIR resources.

Invariant enforced at the output stage: only adapters whose
``can_target_fhir_server()`` returns True (i.e. the FHIR adapter) may have their
output uploaded to the target FHIR server.  CDA/HL7v2/DICOM output returns to
the caller or object storage only.
"""

from __future__ import annotations

from pipeline.sources.protocol import DataSourceAdapter
from pipeline.sources.fhir_adapter import FhirAdapter
from pipeline.sources.hl7v2_adapter import Hl7v2Adapter
from pipeline.sources.cda_adapter import CdaAdapter
from pipeline.sources.dicom_adapter import DicomAdapter
from pipeline.sources.tabular_adapter import (
    TabularAdapter,
    apply_column_rules,
    recommend_column_action,
    rules_for_table,
)

__all__ = [
    "DataSourceAdapter",
    "FhirAdapter",
    "Hl7v2Adapter",
    "CdaAdapter",
    "DicomAdapter",
    "TabularAdapter",
    "apply_column_rules",
    "recommend_column_action",
    "rules_for_table",
]
