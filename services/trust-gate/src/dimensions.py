"""Data-quality dimension taxonomy + grading (DAMA DMBOK + ISO/IEC 25012).

Maps each atomic check to a recognized DQ *dimension* so the passport can present
a per-dimension scorecard with a letter grade — the consumer-facing "is this data
fit for secondary use?" view layered on top of the Kahn category measurement.

Dimensions are the standard set (DAMA DMBOK DQ dimensions; ISO/IEC 25012 data
quality characteristics), not invented. Built-in checks map by ``check_id``;
config-driven plausibility rules map by their ``kind``.
"""

from __future__ import annotations

from constants import CATEGORY_MIN_RATE, PASS_MIN_RATE

COMPLETENESS = "completeness"
CONFORMITY = "conformity"
CONSISTENCY = "consistency"
ACCURACY = "accuracy"
UNIQUENESS = "uniqueness"
INTEGRITY = "integrity"
CURRENCY = "currency"
PROVENANCE = "provenance"
# Believability of values (Wang & Strong 1996 "believability"; Kahn 2016
# plausibility). A distinct dimension because a statistical/range implausibility
# is NOT accuracy (which requires agreement with a real-world gold standard) — the
# passport's own caveat states a plausibility PASS does not certify clinical truth.
PLAUSIBILITY = "plausibility"

ALL_DIMENSIONS: tuple[str, ...] = (
    COMPLETENESS,
    CONFORMITY,
    CONSISTENCY,
    ACCURACY,
    PLAUSIBILITY,
    UNIQUENESS,
    INTEGRITY,
    CURRENCY,
    PROVENANCE,
)

_BUILTIN_DIM: dict[str, str] = {
    # Conformance to formats / structures / code systems → conformity.
    "conformance.resource_type_present": CONFORMITY,
    "conformance.resource_id_present": CONFORMITY,
    "conformance.status_not_entered_in_error": CONFORMITY,
    "conformance.structural": CONFORMITY,
    "conformance.profile": CONFORMITY,
    "conformance.ig_profile": CONFORMITY,
    "conformance.value_format": CONFORMITY,
    "conformance.coding_structure": CONFORMITY,
    "conformance.code_wellformed": CONFORMITY,
    # Code membership in a terminology/value set is validity/conformity (does the
    # value conform to its definition domain), NOT accuracy (the *correct* code for
    # this patient, which a terminology server cannot assess).
    "conformance.terminology": CONFORMITY,
    "conformance.reference_integrity": INTEGRITY,
    "conformance.provenance_present": PROVENANCE,
    "completeness.required_elements": COMPLETENESS,
    "completeness.value_or_absent": COMPLETENESS,
    "completeness.element_density": COMPLETENESS,
    "plausibility.uniqueness": UNIQUENESS,
    "plausibility.patient_identity_stable": UNIQUENESS,
    # Believability of values (outliers, definitional bounds) → plausibility, not
    # accuracy: these flag implausible values, not disagreement with a gold standard.
    "plausibility.value_outlier": PLAUSIBILITY,
    "plausibility.definitional_bounds": PLAUSIBILITY,
    # Cross-field agreement → consistency.
    "plausibility.concordance": CONSISTENCY,
    # Cross-batch distribution drift → consistency (across time/sources).
    "plausibility.distribution_drift": CONSISTENCY,
    # Timeliness measures → currency.
    "plausibility.record_lag": CURRENCY,
    "plausibility.currency": CURRENCY,
    # Value agreement with a gold reference → accuracy (the one true accuracy check).
    "conformance.source_of_truth": ACCURACY,
}

# Plausibility-rule kind → dimension.
_KIND_DIM: dict[str, str] = {
    "date_order": CONSISTENCY,
    "period_order": CONSISTENCY,
    "date_after_birth": CONSISTENCY,
    "date_before_death": CONSISTENCY,
    "not_in_future": CURRENCY,  # a future-dated record is a timeliness/currency defect
    "value_range": PLAUSIBILITY,  # a clinical range check is believability, not accuracy
}


def dimension_for_builtin(check_id: str) -> str:
    return _BUILTIN_DIM.get(check_id, "")


def dimension_for_kind(kind: str) -> str:
    # Unknown kinds are left UNMAPPED ("") — mirroring dimension_for_builtin — so a
    # novel custom rule does not silently inflate a dimension (the old ACCURACY
    # default did exactly that).
    return _KIND_DIM.get(kind, "")


def grade_for(score: float | None) -> str | None:
    """Letter grade for a 0–100 pass-rate, or None when not assessed.

    Bands are ALIGNED to the gate's decision floors so the grade and the verdict
    tell one story (previously a score of 88 read as "grade B" yet was a
    CONDITIONAL_PASS, which misleads a consumer who reads B as "good"):

      A  ≥ 95                    excellent (conventional)
      B  ≥ PASS_MIN_RATE (90)    meets the overall pass floor → a clean PASS sits here
      C  ≥ CATEGORY_MIN_RATE(80) meets the per-category floor but below overall pass
      D  ≥ 60                    marginal (conventional)
      F  < 60

    The two cited cut-points are the policy floors, not magic numbers; A and D are
    conventional excellent/marginal marks. The authoritative verdict remains the
    decision + fitness statement — the grade is the descriptive summary.
    """
    if score is None:
        return None
    if score >= 95:
        return "A"
    if score >= PASS_MIN_RATE:
        return "B"
    if score >= CATEGORY_MIN_RATE:
        return "C"
    if score >= 60:
        return "D"
    return "F"
