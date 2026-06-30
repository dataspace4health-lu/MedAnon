"""Trust Gate constants, thresholds, and check configuration.

The scoring is grounded in Kahn et al. (2016) + the OHDSI Data Quality Dashboard
methodology (violation-rate vs per-check threshold). There are **no arbitrary
domain weights** — the headline metric is % of checks passing per Kahn category.

Per-check thresholds and the decision-policy cut-offs are config-driven
(``config/checks.yaml`` + env) so they are tunable and auditable rather than
hardcoded magic numbers.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("trust_gate.constants")

# ---------------------------------------------------------------------------
# Decision-policy thresholds (product layer, NOT Kahn). Env-overridable.
# ---------------------------------------------------------------------------

# Minimum overall check pass-rate (%) for a clean PASS. Below → CONDITIONAL_PASS.
PASS_MIN_RATE: float = float(os.environ.get("TRUST_GATE_PASS_MIN_RATE", "90"))

# Per-category pass-rate (%) below which the dataset cannot be a clean PASS.
CATEGORY_MIN_RATE: float = float(os.environ.get("TRUST_GATE_CATEGORY_MIN_RATE", "80"))

# ---------------------------------------------------------------------------
# Default per-check violation thresholds (max acceptable violation fraction).
# 0.0 = zero tolerance (any violating row fails the check). Overridable per
# check_id in config/checks.yaml under `thresholds:`.
# ---------------------------------------------------------------------------

DEFAULT_THRESHOLDS: dict[str, float] = {
    # Conformance — SAM prerequisite block checks (always zero-tolerance, critical).
    "conformance.resource_type_present": 0.0,
    "conformance.resource_id_present": 0.0,
    "conformance.status_not_entered_in_error": 0.0,
    # Conformance — structural validity is zero-tolerance and critical.
    "conformance.structural": 0.0,
    "conformance.profile": 0.0,
    # Implementation-Guide (US Core etc.) conformance: opt-in, non-critical, so it
    # shapes the conformance score without hard-blocking. Zero-tolerance because a
    # caller only selects an IG when they require conformance to it.
    "conformance.ig_profile": 0.0,
    "conformance.coding_structure": 0.0,
    "conformance.value_format": 0.0,
    # Offline code well-formedness (format + check digit). Zero-tolerance: a
    # malformed code is always an error.
    "conformance.code_wellformed": 0.0,
    "conformance.terminology": 0.0,
    "conformance.reference_integrity": 0.0,
    # Completeness — small tolerance is acceptable for non-required richness.
    "completeness.required_elements": 0.0,
    "completeness.value_or_absent": 0.0,
    # Element density is a *frequency* metric (Kahn completeness / OHDSI
    # measureValueCompleteness): recommended-but-optional richness. A non-zero
    # tolerance is correct — optional fields are not expected to be 100% present.
    "completeness.element_density": 0.5,
    # Plausibility — uniqueness is zero-tolerance; temporal/atemporal configurable.
    "plausibility.uniqueness": 0.0,
    # Value-outlier tolerance: flag a (code,unit) group only when >1% of its values
    # are extreme outliers — a systemic error signal, not a few genuine extremes.
    "plausibility.value_outlier": 0.01,
    # Definitional bounds are zero-tolerance: a value outside its unit's
    # mathematical definition (e.g. >100%) is always an error.
    "plausibility.definitional_bounds": 0.0,
    # Concordance: any cross-field contradiction is a violation.
    "plausibility.concordance": 0.0,
    # Cross-batch distribution drift (consistency): any drifted (code,unit) group.
    "plausibility.distribution_drift": 0.0,
    # Timeliness (currency): record lag / freshness — opt-in via env thresholds.
    "plausibility.record_lag": 0.0,
    "plausibility.currency": 0.0,
    # Accuracy vs a supplied source-of-truth reference.
    "conformance.source_of_truth": 0.0,
}


def threshold_for(check_id: str, overrides: dict[str, float] | None = None) -> float:
    if overrides and check_id in overrides:
        return float(overrides[check_id])
    return DEFAULT_THRESHOLDS.get(check_id, 0.0)


# ---------------------------------------------------------------------------
# Clinical code system URIs validated by the terminology domain.
# ---------------------------------------------------------------------------

# Resource types where status=entered-in-error must block de-identification.
# Per FHIR Safety Checklist §4: these resources carry clinical data that must
# be filtered before privacy transformation — passing them through would produce
# false pseudonymized records that appear valid.
ENTERED_IN_ERROR_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "Observation",
        "Condition",
        "MedicationRequest",
        "AllergyIntolerance",
        "Encounter",
        "MedicationStatement",
        "DiagnosticReport",
        "Procedure",
    }
)

CLINICAL_CODE_SYSTEMS: frozenset[str] = frozenset(
    {
        # International + US
        "http://loinc.org",
        "http://snomed.info/sct",
        "http://hl7.org/fhir/sid/icd-10",
        "http://hl7.org/fhir/sid/icd-10-cm",
        "http://www.nlm.nih.gov/research/umls/rxnorm",
        "http://unitsofmeasure.org",
        # EU (additive): ATC medications, EDQM Standard Terms, ICD-10-GM diagnoses
        "http://www.whocc.no/atc",
        "http://standardterms.edqm.eu",
        "http://fhir.de/CodeSystem/bfarm/icd-10-gm",
    }
)

REDACTED_SENTINELS: frozenset[str] = frozenset(
    {"", "[REDACTED]", "REDACTED", "redacted", "unknown", "UNKNOWN"}
)

# ---------------------------------------------------------------------------
# Minimal required elements per resource type (completeness, verification).
# ---------------------------------------------------------------------------

REQUIRED_ELEMENTS: dict[str, list[str]] = {
    "Patient": ["gender"],
    "Observation": ["status", "code", "subject"],
    "Condition": ["code", "subject"],
    "Encounter": ["status", "class"],
    "MedicationRequest": ["status", "intent", "subject"],
    "Procedure": ["status", "subject"],
    "DiagnosticReport": ["status", "code"],
    "AllergyIntolerance": ["patient"],
    "Immunization": ["status", "vaccineCode", "patient"],
}

# Resource types where a value[x] or dataAbsentReason is expected.
VALUE_OR_ABSENT_TYPES: frozenset[str] = frozenset({"Observation"})

# The closed set of FHIR R4 Observation.value[x] choice-type keys. Used instead
# of a loose ``startswith("value")`` scan so non-standard keys (e.g. valueSet on
# a contained resource, or a custom extension field) cannot masquerade as a
# populated observation value.
OBSERVATION_VALUE_KEYS: frozenset[str] = frozenset(
    {
        "valueQuantity",
        "valueCodeableConcept",
        "valueString",
        "valueBoolean",
        "valueInteger",
        "valueRange",
        "valueRatio",
        "valueSampledData",
        "valueTime",
        "valueDateTime",
        "valuePeriod",
    }
)

# ---------------------------------------------------------------------------
# Recommended (not required) elements per resource type — the "richness" set for
# the completeness *frequency* metric (Kahn completeness / OHDSI
# measureValueCompleteness). Unlike REQUIRED_ELEMENTS (binary present/absent),
# element_density reports the *fraction* of these that are populated; missing
# optional richness is tolerated up to completeness.element_density's threshold.
# ---------------------------------------------------------------------------

RECOMMENDED_ELEMENTS: dict[str, list[str]] = {
    "Patient": ["gender", "birthDate", "name", "identifier", "address"],
    "Observation": ["status", "code", "subject", "effectiveDateTime", "category"],
    "Condition": ["code", "subject", "clinicalStatus", "verificationStatus", "category"],
    "Encounter": ["status", "class", "subject", "period", "type"],
    "MedicationRequest": ["status", "intent", "subject", "medicationCodeableConcept", "authoredOn"],
    "Procedure": ["status", "subject", "code", "performedDateTime"],
    "DiagnosticReport": ["status", "code", "subject", "effectiveDateTime", "category"],
    "AllergyIntolerance": ["patient", "code", "clinicalStatus", "verificationStatus"],
    "Immunization": ["status", "vaccineCode", "patient", "occurrenceDateTime"],
}

# ---------------------------------------------------------------------------
# FHIR primitive format conformance (value/verification) — checked WITHOUT an
# external validator so conformance is never fully "not assessed". Regexes are
# the official FHIR R4 primitive patterns.
# ---------------------------------------------------------------------------

# FHIR dateTime accepts a date-only value, so it safely covers `date` fields too.
FHIR_DATETIME_PATTERN: str = (
    r"^([0-9]([0-9]([0-9][1-9]|[1-9]0)|[1-9]00)|[1-9]000)"
    r"(-(0[1-9]|1[0-2])(-(0[1-9]|[1-2][0-9]|3[0-1])"
    r"(T([01][0-9]|2[0-3]):[0-5][0-9]:([0-5][0-9]|60)(\.[0-9]+)?"
    r"(Z|(\+|-)((0[0-9]|1[0-3]):[0-5][0-9]|14:00)))?)?)?$"
)
# FHIR id: 1-64 chars of [A-Za-z0-9-.].
FHIR_ID_PATTERN: str = r"^[A-Za-z0-9\-.]{1,64}$"

# Unambiguous temporal primitive field names to format-check (date/dateTime/
# instant). Period.start/.end are handled separately (they live under a `period`).
TEMPORAL_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "birthDate",
        "deceasedDateTime",
        "effectiveDateTime",
        "issued",
        "authoredOn",
        "recordedDate",
        "onsetDateTime",
        "abatementDateTime",
        "occurrenceDateTime",
        "performedDateTime",
        "lastUpdated",
    }
)

# Dataset-level provenance evidence required for auditability (policy layer).
REQUIRED_PROVENANCE_KEYS: tuple[str, ...] = ("source_system", "extraction_time")

# ---------------------------------------------------------------------------
# Descriptive profiling (data-analysis support — NOT scored). Numeric value
# stats are computed for Observations carrying these LOINC codes (extensible).
# ---------------------------------------------------------------------------

PROFILE_MAX_DISTINCT: int = 15  # cap distinct values shown in categorical distributions

# Numeric value distributions (the analyst-facing clinical "data cards") are NOT
# capped: every distinct (concept, unit) the data carries gets a card so the whole
# clinical panel is visible. They are bounded naturally by the number of distinct
# LOINC/unit pairs in the source, not by an arbitrary limit.

# ---------------------------------------------------------------------------
# Data-driven value-outlier detection (plausibility / atemporal / verification).
#
# Rationale: OHDSI removed most hardcoded plausibleValueLow/High ranges because
# universal bounds caused false failures, and no authoritative machine-readable
# LOINC→range table exists. So instead of inventing per-code limits we flag
# values that are extreme *relative to the dataset's own distribution* for the
# same (code, unit) — the lab2clean / Tukey-fence approach. These are statistical
# METHOD parameters (citable), not invented clinical thresholds, and are all
# env-overridable.
#   fence = [Q1 - K*IQR, Q3 + K*IQR];  K=3.0 is Tukey's "far out" (extreme) fence.
# A group is only assessed when it has >= MIN_SAMPLE values (you cannot learn a
# distribution from a handful of points → otherwise the check is NA, never a
# false flag).
# ---------------------------------------------------------------------------

OUTLIER_IQR_K: float = float(os.environ.get("TRUST_GATE_OUTLIER_IQR_K", "3.0"))
OUTLIER_MIN_SAMPLE: int = int(os.environ.get("TRUST_GATE_OUTLIER_MIN_SAMPLE", "20"))
# Modified z-score cut-off (Iglewicz & Hoaglin): |0.6745·(x−median)/MAD| > 3.5.
OUTLIER_MODZ_CUTOFF: float = float(os.environ.get("TRUST_GATE_OUTLIER_MODZ", "3.5"))
# Cross-batch drift: flag a (code,unit) when its batch median sits more than this
# modified-z (vs the accumulated baseline median/MAD) away — a distribution shift.
DRIFT_MODZ_CUTOFF: float = float(os.environ.get("TRUST_GATE_DRIFT_MODZ", "3.5"))
# When the baseline MAD is 0 (a degenerate, all-identical baseline) the modified-z
# is undefined; fall back to a RELATIVE shift tolerance instead of exact float
# equality (which hair-triggers on a 1-ULP difference). Flag only when the batch
# median differs from the baseline median by more than this fraction.
DRIFT_ZERO_MAD_REL_TOL: float = float(
    os.environ.get("TRUST_GATE_DRIFT_ZERO_MAD_REL_TOL", "0.05")
)
# Reservoir size per (code, unit) for the persisted cross-batch baseline.
BASELINE_RESERVOIR_SIZE: int = int(os.environ.get("TRUST_GATE_BASELINE_RESERVOIR", "500"))
# Seed for the reservoir sampler so cross-batch baselines (and therefore the
# statistical outlier/drift signal) are reproducible across runs (Phase 1.5
# determinism). The sampling is for an unbiased reservoir, not security.
SAMPLER_SEED: int = int(os.environ.get("TRUST_GATE_SAMPLER_SEED", "1337"))

# Statistical / batch-relative checks. These are non-deterministic by nature
# (random reservoir + batch-relative distribution) and so are REPORTED as an
# advisory but never drive the BLOCK/PASS/CONDITIONAL verdict — which must be
# reproducible and defensible. The deterministic check set (structural, format,
# code, completeness, referential, temporal, concordance, definitional bounds,
# uniqueness) is the sole basis of the decision.
ADVISORY_CHECK_IDS: frozenset = frozenset(
    {
        "plausibility.value_outlier",
        "plausibility.distribution_drift",
        "plausibility.value_outlier_stratified",
    }
)

# Determinism classification (Phase 1.5, fail-safe variant). The advisory/verdict
# split must default the *safe* way: a check that nobody has classified yet stays
# advisory and so can never silently move the gate. Classification is therefore
# an explicit allow-list of deterministic ids — anything not on it (a new
# statistical check, a typo, a future addition) is treated as advisory by
# ``is_deterministic`` below. ``DETERMINISTIC_CHECK_IDS`` enumerates the fixed
# built-in checks; ``DETERMINISTIC_PREFIXES`` covers ids generated dynamically
# (OMOP DQD checks are ``dqd.<table>.<col>.<kind>``); config-supplied plausibility
# rule ids are passed in per call (they are deterministic date/range rules).
DETERMINISTIC_CHECK_IDS: frozenset = frozenset(
    {
        "clinical.measurement_after_birth",
        "clinical.event_after_death",
        "completeness.element_density",
        "completeness.required_elements",
        "completeness.value_or_absent",
        "conformance.code_wellformed",
        "conformance.coding_structure",
        "conformance.ig_profile",
        "conformance.profile",
        "conformance.provenance_present",
        "conformance.reference_integrity",
        "conformance.resource_id_present",
        "conformance.resource_type_present",
        "conformance.source_of_truth",
        "conformance.status_not_entered_in_error",
        "conformance.structural",
        "conformance.terminology",
        "conformance.value_format",
        "plausibility.concordance",
        "plausibility.currency",
        "plausibility.definitional_bounds",
        "plausibility.patient_identity_stable",
        "plausibility.record_lag",
        "plausibility.uniqueness",
    }
)

# Prefixes for deterministic ids generated at runtime (cannot be enumerated).
DETERMINISTIC_PREFIXES: tuple[str, ...] = ("dqd.",)


def is_classified(check_id: str, rule_ids: frozenset = frozenset()) -> bool:
    """True iff *check_id* is in an explicit registry (advisory or deterministic).

    Used by the determinism coverage test: an id that is in neither registry
    falls through ``is_deterministic`` to the advisory fail-safe, which silently
    shrinks the verdict basis. The test asserts every emitted id is classified.
    """
    return (
        check_id in ADVISORY_CHECK_IDS
        or check_id in DETERMINISTIC_CHECK_IDS
        or check_id in rule_ids
        or any(check_id.startswith(p) for p in DETERMINISTIC_PREFIXES)
    )


def is_deterministic(check_id: str, rule_ids: frozenset = frozenset()) -> bool:
    """Whether *check_id* may drive the verdict. Unknown ids are advisory (safe)."""
    if check_id in ADVISORY_CHECK_IDS:
        return False
    if check_id in DETERMINISTIC_CHECK_IDS or check_id in rule_ids:
        return True
    return any(check_id.startswith(p) for p in DETERMINISTIC_PREFIXES)

# ---------------------------------------------------------------------------
# Definitional unit bounds (plausibility / atemporal / verification).
#
# These follow from the UNIT'S OWN DEFINITION, not from clinical knowledge, so
# they are universally true and citable — unlike invented per-measurement
# "normal" ranges. A percentage cannot mathematically exceed 100 or be negative.
# This catches whole-batch corruption (e.g. an SpO2 feed in the wrong unit, or a
# sign-flipped lab value) that the distribution-relative outlier check (advisory,
# batch-relative) cannot fail on its own. Operators may add/override unit bounds
# in config (`definitional_unit_bounds:`) for their context.
# ---------------------------------------------------------------------------

# Units whose denoted physical quantity is non-negative by the quantity KIND the
# unit expresses: a count is a cardinality (>= 0); a mass concentration is mass
# per volume with mass >= 0; Kelvin is bounded below by absolute zero. These are
# definitional, not clinical "normal ranges", so they stay universally true and
# citable. Shipped as one-sided (0, inf) bounds so a sign-flipped / mis-keyed
# value is caught deterministically.
#
# Deliberately EXCLUDES absolute mass / length / volume units (kg, g, cm, m, mL,
# L): legitimate delta / balance observations (weight change, net fluid balance)
# are signed in those units, so a blanket non-negativity rule would false-flag
# them. Non-negativity is a property of the concept, not those units — so those
# stay config-driven, not bundled.
NON_NEGATIVE_UNITS: frozenset[str] = frozenset(
    {
        # counts / count-concentrations (cardinality >= 0)
        "/uL", "/mm3", "10*3/uL", "10*6/uL", "10*9/L", "10*12/L", "10*3/mL",
        # mass concentrations (mass / volume, mass >= 0)
        "mg/dL", "g/dL", "g/L", "mg/L", "ug/dL", "ug/L", "ng/mL", "pg/mL",
        "ug/mL", "ng/dL",
        # absolute temperature (>= absolute zero)
        "K",
    }
)

DEFINITIONAL_UNIT_BOUNDS: dict[str, tuple[float, float]] = {
    "%": (0.0, 100.0),
    **{u: (0.0, float("inf")) for u in NON_NEGATIVE_UNITS},
}
