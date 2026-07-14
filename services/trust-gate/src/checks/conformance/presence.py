"""Offline (verification) conformance checks: presence, primitive format, coding
structure. No external server  these always run and are the always-on structural
BLOCK authority (resource_type/id present, entered-in-error filter).
"""

from __future__ import annotations

from constants import ENTERED_IN_ERROR_RESOURCE_TYPES, REDACTED_SENTINELS, threshold_for
from passport import CheckResult, redact_token

from checks.conformance._shared import (
    _DATETIME_RE,
    _ID_RE,
    _iter_codings,
    _iter_temporal_values,
)


def _resource_type_present(resources: list[dict], thresholds) -> CheckResult:
    """Every resource must carry a non-empty resourceType string.

    Without it the pipeline cannot route or match rules; processing cannot
    proceed. This check is always first in the SAM chain.
    """
    chk = CheckResult(
        check_id="conformance.resource_type_present",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.resource_type_present", thresholds),
        critical=True,
        description="Every resource carries a non-empty resourceType.",
        recommendation="Ensure all resources have a valid FHIR resourceType field.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_format",
    )
    for idx, res in enumerate(resources):
        if not isinstance(res, dict):
            continue
        chk.applicable += 1
        rt = res.get("resourceType")
        if not (isinstance(rt, str) and rt.strip()):
            chk.violations += 1
            # enumerate(): list.index() is O(n²) and, worse, returns the first
            # *equal* dict's position  wrong precisely here, where violating
            # resources are often empty/identical. add_detail() also bounds the count.
            chk.add_detail(
                resource_index=idx,
                attribute="resourceType",
                hdqt_category="accuracy",
                hdqt_dimension="invalid_format",
                severity="critical",
                detail="resourceType absent or empty",
            )
    return chk


def _resource_id_present(resources: list[dict], thresholds) -> CheckResult:
    """Every resource must carry a non-empty id string.

    Without an id the de-identification engine has no pseudonymization target
    and cross-resource references cannot be resolved.
    """
    chk = CheckResult(
        check_id="conformance.resource_id_present",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.resource_id_present", thresholds),
        critical=True,
        description="Every resource carries a non-empty id.",
        recommendation="Ensure all resources have a valid FHIR id field.",
        hdqt_category="availability",
        hdqt_dimension="missing",
    )
    for res in resources:
        if not isinstance(res, dict) or not res.get("resourceType"):
            continue
        chk.applicable += 1
        rid = res.get("id")
        if not (isinstance(rid, str) and rid.strip()):
            chk.violations += 1
            chk.add_detail(
                resource_type=res.get("resourceType"),
                attribute="id",
                hdqt_category="availability",
                hdqt_dimension="missing",
                severity="critical",
                detail="id absent or empty",
            )
    return chk


def _status_not_entered_in_error(resources: list[dict], thresholds) -> CheckResult:
    """Clinical resources with status=entered-in-error must be filtered before
    de-identification.  Per FHIR Safety Checklist §4: entered-in-error resources
    are retracted; running them through de-id would create false pseudonymized
    records that appear valid.
    """
    chk = CheckResult(
        check_id="conformance.status_not_entered_in_error",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.status_not_entered_in_error", thresholds),
        critical=True,
        description=(
            "Clinical resources ("
            + ", ".join(sorted(ENTERED_IN_ERROR_RESOURCE_TYPES))
            + ") must not have status=entered-in-error."
        ),
        recommendation=(
            "Filter out entered-in-error resources before sending to the "
            "de-identification pipeline."
        ),
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
    )
    for res in resources:
        if not isinstance(res, dict):
            continue
        rt = res.get("resourceType")
        if rt not in ENTERED_IN_ERROR_RESOURCE_TYPES:
            continue
        chk.applicable += 1
        if res.get("status") == "entered-in-error":
            chk.violations += 1
            chk.violation_details.append(
                {
                    "check_id": chk.check_id,
                    "resource_type": rt,
                    "resource_id": res.get("id", ""),
                    "attribute": "status",
                    "value": "entered-in-error",
                    "hdqt_category": "accuracy",
                    "hdqt_dimension": "invalid_value",
                    "severity": "critical",
                    "detail": "status=entered-in-error must be filtered before de-identification",
                }
            )
    return chk


def _value_format(resources, thresholds):
    chk = CheckResult(
        check_id="conformance.value_format",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.value_format", thresholds),
        description="Primitive values match their FHIR format (date/dateTime, id).",
        recommendation="Emit FHIR-valid primitives (ISO-8601 dates, [A-Za-z0-9-.] ids).",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_format",
    )
    for res in resources:
        if not isinstance(res, dict) or not res.get("resourceType"):
            continue
        if "id" in res:
            chk.applicable += 1
            rid = res.get("id")
            if not (isinstance(rid, str) and _ID_RE.match(rid)):
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=str(res.get("id", ""))[:64],
                    path="id",
                    # A resource id can carry a real identifier (the MRN-as-id
                    # anti-pattern), so tokenize rather than surface it verbatim
                    # even though the rest of the value examples are PHI-safe.
                    value=redact_token(str(rid)[:64]),
                    detail="id is not a valid FHIR id ([A-Za-z0-9.-], max 64 chars)",
                )
        for _name, value in _iter_temporal_values(res):
            chk.applicable += 1
            if not (isinstance(value, str) and _DATETIME_RE.match(value)):
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=str(res.get("id", ""))[:64],
                    path=_name,
                    value=str(value)[:64],
                    detail=f"{_name} is not a valid FHIR date/dateTime",
                )
    return chk


def _coding_structure(resources, thresholds):
    chk = CheckResult(
        check_id="conformance.coding_structure",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.coding_structure", thresholds),
        description="Every coding carries a non-empty system + code (not a sentinel).",
        recommendation="Ensure codings keep system+code after de-identification.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
    )
    for res in resources:
        if not isinstance(res, dict):
            continue
        for coding in _iter_codings(res):
            chk.applicable += 1
            system, code = coding.get("system"), coding.get("code")
            if not (isinstance(system, str) and system) or not (
                isinstance(code, str) and code and code not in REDACTED_SENTINELS
            ):
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=res.get("id", ""),
                    path="coding",
                    detail="coding missing system+code or carries a redacted sentinel",
                )
    return chk
