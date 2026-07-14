"""Accuracy vs a source-of-truth reference (Kahn validation; ISO 25012 accuracy).

When the caller supplies a gold reference (expected field values keyed by
``ResourceType/id``), each present field is compared against it. A mismatch is an
accuracy violation. NA when no reference is supplied  accuracy against an external
benchmark cannot be assessed without one (never a false PASS).

PHI safety: only the field *name* is recorded in the audit detail, never the
actual or expected value (those can be PHI, e.g. birthDate).
"""

from __future__ import annotations

from constants import threshold_for
from passport import CheckResult


def evaluate_against_reference(
    resources: list[dict],
    reference: dict | None,
    thresholds: dict[str, float] | None = None,
) -> CheckResult:
    chk = CheckResult(
        check_id="conformance.source_of_truth",
        category="conformance",
        subcategory="value",
        context="validation",
        threshold=threshold_for("conformance.source_of_truth", thresholds),
        description="Field values match the supplied source-of-truth reference.",
        recommendation="Reconcile values that disagree with the gold reference.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
    )
    records = (reference or {}).get("records") if isinstance(reference, dict) else None
    if not isinstance(records, dict) or not records:
        return chk  # NA  no reference supplied

    for res in resources:
        if not isinstance(res, dict):
            continue
        key = f"{res.get('resourceType')}/{res.get('id')}"
        expected = records.get(key)
        if not isinstance(expected, dict):
            continue
        for field, exp_val in expected.items():
            chk.applicable += 1
            if res.get(field) != exp_val:
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=res.get("id", ""),
                    path=field,
                    detail=f"value for '{field}' disagrees with the source-of-truth reference",
                )
    return chk
