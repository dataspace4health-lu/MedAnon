"""Audit phase taxonomy  groups atomic checks into selectable suites.

A *phase* is a named quality concern the user can enable/disable independently so
the pipeline measures exactly what they want, rather than one generic pass
(Great Expectations 'suites' / OHDSI DQD check groups). Built-in checks map to a
phase by ``check_id``; config-driven plausibility rules map by their ``kind``.

Selecting a subset of phases also lets the engine skip the expensive external
calls (FHIR validator, terminology server) whose phase was deselected.
"""

from __future__ import annotations

STRUCTURAL = "structural_conformance"
TERMINOLOGY = "terminology_validity"
REFERENTIAL = "referential_integrity"
COMPLETENESS_CORE = "completeness_core"
COMPLETENESS_RICHNESS = "completeness_richness"
VALUE_PLAUSIBILITY = "value_plausibility"
TEMPORAL_PLAUSIBILITY = "temporal_plausibility"
IDENTITY_INTEGRITY = "identity_integrity"
PROVENANCE = "provenance_auditability"
TIMELINESS = "timeliness"
SOURCE_ACCURACY = "source_accuracy"

ALL_PHASES: tuple[str, ...] = (
    STRUCTURAL,
    TERMINOLOGY,
    REFERENTIAL,
    COMPLETENESS_CORE,
    COMPLETENESS_RICHNESS,
    VALUE_PLAUSIBILITY,
    TEMPORAL_PLAUSIBILITY,
    IDENTITY_INTEGRITY,
    PROVENANCE,
    TIMELINESS,
    SOURCE_ACCURACY,
)

# Built-in (non-rule) check_id → phase.
_BUILTIN_PHASE: dict[str, str] = {
    "conformance.resource_type_present": STRUCTURAL,
    "conformance.resource_id_present": STRUCTURAL,
    "conformance.status_not_entered_in_error": STRUCTURAL,
    "conformance.structural": STRUCTURAL,
    "conformance.profile": STRUCTURAL,
    "conformance.ig_profile": STRUCTURAL,
    "conformance.value_format": STRUCTURAL,
    "conformance.coding_structure": STRUCTURAL,
    "conformance.code_wellformed": TERMINOLOGY,
    "conformance.terminology": TERMINOLOGY,
    "conformance.reference_integrity": REFERENTIAL,
    "conformance.provenance_present": PROVENANCE,
    "completeness.required_elements": COMPLETENESS_CORE,
    "completeness.value_or_absent": COMPLETENESS_CORE,
    "completeness.element_density": COMPLETENESS_RICHNESS,
    "plausibility.uniqueness": IDENTITY_INTEGRITY,
    "plausibility.patient_identity_stable": IDENTITY_INTEGRITY,
    "plausibility.value_outlier": VALUE_PLAUSIBILITY,
    "plausibility.definitional_bounds": VALUE_PLAUSIBILITY,
    "plausibility.concordance": VALUE_PLAUSIBILITY,
    "plausibility.distribution_drift": VALUE_PLAUSIBILITY,
    "plausibility.record_lag": TIMELINESS,
    "plausibility.currency": TIMELINESS,
    "conformance.source_of_truth": SOURCE_ACCURACY,
}

# Plausibility-rule ``kind`` → phase (for config-driven rule checks whose
# check_id is the operator-chosen rule_id).
_KIND_PHASE: dict[str, str] = {
    "date_order": TEMPORAL_PLAUSIBILITY,
    "period_order": TEMPORAL_PLAUSIBILITY,
    "date_after_birth": TEMPORAL_PLAUSIBILITY,
    "date_before_death": TEMPORAL_PLAUSIBILITY,
    "not_in_future": TEMPORAL_PLAUSIBILITY,
    "value_range": VALUE_PLAUSIBILITY,
}


def phase_for_builtin(check_id: str) -> str:
    return _BUILTIN_PHASE.get(check_id, "")


def phase_for_kind(kind: str) -> str:
    return _KIND_PHASE.get(kind, VALUE_PLAUSIBILITY)


def normalize_selection(phases: list[str] | None) -> set[str]:
    """Resolve a requested phase list to a valid set; None/empty → all phases.

    Unknown phase names are dropped (a typo cannot silently disable everything).
    """
    if not phases:
        return set(ALL_PHASES)
    selected = {p for p in phases if p in ALL_PHASES}
    return selected or set(ALL_PHASES)
