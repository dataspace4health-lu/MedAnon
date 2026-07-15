"""FHIR domain constants shared across layers.

Pure, dependency-free FHIR knowledge so both the pipeline (application) and the
FHIR adapters (integrations) can depend on it downward, rather than an adapter
reaching up into a worker module for a constant.
"""

from __future__ import annotations

# FHIR "infrastructure" resource types: conformance/terminology/metadata
# resources that are never clinical patient data. Excluded from clinical
# export enumeration and from re-identification risk analysis. Previously
# duplicated as ``_INFRA`` (staged_worker._core) and ``INFRA_RESOURCE_TYPES``
# (executor_stream); this is the single source of truth.
INFRA_RESOURCE_TYPES = frozenset(
    {
        "CapabilityStatement",
        "OperationDefinition",
        "SearchParameter",
        "StructureDefinition",
        "CompartmentDefinition",
        "ImplementationGuide",
        "CodeSystem",
        "ValueSet",
        "ConceptMap",
        "NamingSystem",
        "OperationOutcome",
        "Bundle",
    }
)
