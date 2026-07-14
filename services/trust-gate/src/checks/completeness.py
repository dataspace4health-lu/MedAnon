"""Completeness category (Kahn 2016): is a variable present / are values recorded?

- required_elements (verification) — minimal required elements per resource type
- value_or_absent  (verification)  — Observation has value[x] or dataAbsentReason
- element_density  (verification)  — *frequency* of recommended-element population
                                     (Kahn completeness / OHDSI measureValueCompleteness)
"""

from __future__ import annotations

from constants import (
    OBSERVATION_VALUE_KEYS,
    RECOMMENDED_ELEMENTS,
    REQUIRED_ELEMENTS,
    VALUE_OR_ABSENT_TYPES,
    threshold_for,
)
from passport import CheckResult


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _missing(res: dict, fields: list[str]) -> bool:
    return any(_empty(res.get(f)) for f in fields)


def _present(res: dict, field: str) -> bool:
    """A recommended element counts as populated if any value[x]-style variant of
    it is present (e.g. ``effectiveDateTime`` satisfied by ``effectivePeriod``)."""
    if not _empty(res.get(field)):
        return True
    # Choice-type fallback: strip a trailing type suffix and match any sibling
    # that shares the base name (medicationCodeableConcept ↔ medicationReference).
    for base in (
        "effective",
        "medication",
        "onset",
        "occurrence",
        "performed",
        "value",
    ):
        if field.startswith(base):
            return any(k.startswith(base) and not _empty(res.get(k)) for k in res)
    return False


def evaluate(
    resources: list[dict], thresholds: dict[str, float] | None = None
) -> list[CheckResult]:
    # PIQI HDQT mapping: missing → availability/missing; unpopulated → availability/unpopulated;
    # incomplete → availability/incomplete
    req = CheckResult(
        check_id="completeness.required_elements",
        category="completeness",
        subcategory="completeness",
        context="verification",
        threshold=threshold_for("completeness.required_elements", thresholds),
        description="Minimal required elements are present per resource type.",
        recommendation="Populate the required elements for each resource type.",
        hdqt_category="availability",
        hdqt_dimension="missing",
    )
    val = CheckResult(
        check_id="completeness.value_or_absent",
        category="completeness",
        subcategory="completeness",
        context="verification",
        threshold=threshold_for("completeness.value_or_absent", thresholds),
        description="Observation carries a value[x] or a dataAbsentReason.",
        recommendation="Provide a value[x] or a dataAbsentReason on every Observation.",
        hdqt_category="availability",
        hdqt_dimension="incomplete",
    )
    # Element density: the *fraction* of recommended elements left empty across
    # the dataset. denominator = (recommended fields × applicable resources);
    # violations = empty ones. violation_fraction is the "sparseness".
    density = CheckResult(
        check_id="completeness.element_density",
        category="completeness",
        subcategory="completeness",
        context="verification",
        threshold=threshold_for("completeness.element_density", thresholds),
        description="Recommended elements are populated (richness/frequency).",
        recommendation="Increase population of recommended elements per resource type.",
        hdqt_category="availability",
        hdqt_dimension="unpopulated",
    )
    for res in resources:
        if not isinstance(res, dict):
            continue
        rtype = res.get("resourceType")
        required = REQUIRED_ELEMENTS.get(rtype)
        if required:
            req.applicable += 1
            missing = [f for f in required if _empty(res.get(f))]
            if missing:
                req.violations += 1
                req.add_detail(
                    resource_type=rtype,
                    resource_id=res.get("id", ""),
                    path=", ".join(missing),
                    detail=f"missing required element(s): {', '.join(missing)}",
                )
        # SAM prerequisite (PIQI HDQT): an Observation with no status is
        # structurally incomplete — required_elements already counts that defect.
        # value_or_absent is NA for that resource so one missing status is not
        # double-counted across two completeness checks.
        if rtype in VALUE_OR_ABSENT_TYPES and not _empty(res.get("status")):
            val.applicable += 1
            has_value = any(k in OBSERVATION_VALUE_KEYS for k in res)
            # Panel Observations (e.g. Blood Pressure) carry their results in
            # Observation.component[].value[x] (or a component dataAbsentReason),
            # not at the top level — those are NOT missing a value (FHIR R4 §Observation).
            if not has_value:
                components = res.get("component")
                if isinstance(components, list):
                    has_value = any(
                        isinstance(c, dict)
                        and (
                            any(k in OBSERVATION_VALUE_KEYS for k in c)
                            or "dataAbsentReason" in c
                        )
                        for c in components
                    )
            if not has_value and "dataAbsentReason" not in res:
                val.violations += 1
                val.add_detail(
                    resource_type=rtype,
                    resource_id=res.get("id", ""),
                    path="value[x] | component.value[x] | dataAbsentReason",
                    detail="Observation has no value[x], no component value, and no dataAbsentReason",
                )
        recommended = RECOMMENDED_ELEMENTS.get(rtype)
        if recommended:
            density.applicable += len(recommended)
            density.violations += sum(1 for f in recommended if not _present(res, f))
    return [req, val, density]
