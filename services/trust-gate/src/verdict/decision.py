"""Decision: the product fitness-for-use policy (NOT Kahn measurement) that turns
scores into PASS / CONDITIONAL_PASS / BLOCK, plus the per-resource-type threshold
calibration that feeds it. One shared policy (``decide``) is used by the headline
verdict and every sub-verdict (``subset_decision``) so sub-reports are never more
lenient than the overall verdict.
"""

from __future__ import annotations

import os

from constants import CATEGORY_MIN_RATE, PASS_MIN_RATE
from passport import (
    BLOCK,
    CONDITIONAL_PASS,
    PASS,
    CheckResult,
    RESULT_FAIL,
    RESULT_NA,
    RESULT_PASS,
)

from verdict.scoring import category_and_overall


def elevate_critical(
    det_checks: list[CheckResult], critical_check_ids: list[str] | None
) -> None:
    """Mark the use case's critical-to-quality checks as critical (RBQM)."""
    if not critical_check_ids:
        return
    ctq = set(critical_check_ids)
    for c in det_checks:
        if c.check_id in ctq:
            c.critical = True


def blockers(det_checks: list[CheckResult]) -> list[str]:
    """Plain-language blocker lines for failed critical checks (shared)."""
    return [
        f"{c.check_id}: {c.violations}/{c.applicable} violating "
        f"({round(c.violation_fraction * 100, 1)}%) — {c.recommendation}".strip()
        for c in det_checks
        if c.critical and c.result == RESULT_FAIL
    ]


def _regulated_mode() -> bool:
    """Whether ``MEDANON_REGULATED_MODE`` is enabled (read at call time)."""
    return os.environ.get("MEDANON_REGULATED_MODE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def regulated_blockers(det_checks: list[CheckResult]) -> list[str]:
    """Extra blockers imposed only in regulated mode (D7.2 assurance).

    The default posture degrades an un-runnable check to SKIPPED/NA (never a
    false PASS, never a false BLOCK on validator slowness). Regulated mode does
    not accept "not assessed" for the externally-validated conformance dimension:
    a *skipped* check that is critical or in the ``conformance`` category (its
    terminology server or FHIR validator was unavailable, timed out, or was not
    configured) must BLOCK — you cannot release regulated data on an assessment
    that could not run.
    """
    if not _regulated_mode():
        return []
    return [
        f"{c.check_id}: assessment could not run "
        f"({c.skip_reason or 'prerequisite unavailable'}) — regulated mode "
        f"requires this check to complete before release"
        for c in det_checks
        if c.skipped and (c.critical or c.category == "conformance")
    ]


def decide(
    category_scores: dict[str, float | None],
    overall: float,
    blocker_lines: list[str],
    rt_below: list[str],
    provenance_capped: bool,
) -> tuple[str, bool]:
    """The single decision policy (shared by both assess paths)."""
    below = [
        cat
        for cat, s in category_scores.items()
        if s is not None and s < CATEGORY_MIN_RATE
    ]
    if blocker_lines:
        return BLOCK, False
    if overall < PASS_MIN_RATE or below or rt_below or provenance_capped:
        return CONDITIONAL_PASS, True
    return PASS, True


def subset_decision(
    checks: list[CheckResult],
    *,
    provenance_capped: bool = False,
    rt_below: list[str] | None = None,
) -> tuple[str, float, bool]:
    """Decide over a *subset* of checks using the SAME policy as the headline.

    Sub-reports (per-sector, per-phase) must never be more lenient than the
    overall verdict. Routing them through the shared ``category_and_overall`` +
    ``decide`` (instead of an inline "blockers + below PASS_MIN_RATE" rule)
    guarantees the ``CATEGORY_MIN_RATE`` floor — and any provenance cap — applies
    identically. Returns (decision, overall_score, has_assessed).
    """
    category_scores, overall, has_assessed = category_and_overall(checks)
    decision, _allowed = decide(
        category_scores, overall, blockers(checks), rt_below or [], provenance_capped
    )
    return decision, overall, has_assessed


# FHIR observation-category codes → the resource_thresholds sub-type key suffix.
_OBSERVATION_SUBTYPE_KEYS = {
    "laboratory": "Observation_lab",
    "vital-signs": "Observation_vital_signs",
}


def _observation_threshold_key(resources: list[dict]) -> str | None:
    """Resolve the Observation sub-type threshold key for the batch.

    Inspects ``Observation.category`` codings. Returns ``Observation_lab`` or
    ``Observation_vital_signs`` only when *every* Observation in the batch shares
    that single category (a homogeneous batch — the common bulk-export case);
    returns None for an empty or mixed batch so the caller falls back to the
    plain ``Observation`` key, then ``default``. This keeps the sub-type keys
    reachable without misattributing a mixed pool to one threshold.
    """
    seen: set[str] = set()
    for res in resources:
        if not isinstance(res, dict) or res.get("resourceType") != "Observation":
            continue
        codes = set()
        for cat in res.get("category", []) or []:
            if not isinstance(cat, dict):
                continue
            for coding in cat.get("coding", []) or []:
                if isinstance(coding, dict) and isinstance(coding.get("code"), str):
                    codes.add(coding["code"])
        mapped = {
            _OBSERVATION_SUBTYPE_KEYS[c]
            for c in codes
            if c in _OBSERVATION_SUBTYPE_KEYS
        }
        if len(mapped) == 1:
            seen.add(next(iter(mapped)))
        else:
            return None  # uncategorized or multi-category Observation → mixed
    return next(iter(seen)) if len(seen) == 1 else None


def _resolve_rt_threshold(
    rt: str,
    resources: list[dict],
    resource_thresholds: dict[str, float],
) -> float:
    """Pick the configured pass-rate threshold (percentage) for resource type *rt*.

    For Observation, resolve the lab / vital-signs sub-type key when the batch is
    homogeneous; otherwise fall back to the plain type key, then ``default``,
    then the global PASS_MIN_RATE.
    """
    default_threshold = resource_thresholds.get("default", PASS_MIN_RATE)
    if rt == "Observation":
        sub_key = _observation_threshold_key(resources)
        if sub_key and sub_key in resource_thresholds:
            return resource_thresholds[sub_key]
    return resource_thresholds.get(rt, default_threshold)


def check_resource_type_thresholds(
    resources: list[dict],
    checks: list[CheckResult],
    resource_thresholds: dict[str, float],
) -> list[str]:
    """Return resource types whose genuine per-type pass-rate is below threshold
    (Phase 2A calibration — ONC ASTP Dec 2024).

    Per-type pass-rate = % of assessed checks passing among (a) checks scoped to
    that resource type (``check.resource_type == rt`` — e.g. vital-sign range
    rules, the Patient identity SAM) plus (b) batch-wide checks
    (``check.resource_type == ""`` — structural, terminology, reference integrity)
    which apply to every type. NA (skipped) checks are excluded from the
    denominator. Thresholds are on the percentage scale (normalized at load).

    This is a real per-type measurement, not the overall-rate proxy: a vital-sign
    range failure pulls down Observation's rate without touching Patient's.
    """
    if not resource_thresholds:
        return []
    resource_types = {
        r.get("resourceType")
        for r in resources
        if isinstance(r, dict) and r.get("resourceType")
    }
    below: list[str] = []
    for rt in sorted(t for t in resource_types if t):
        relevant = [
            c for c in checks if c.result != RESULT_NA and c.resource_type in ("", rt)
        ]
        if not relevant:
            continue
        passed = sum(1 for c in relevant if c.result == RESULT_PASS)
        rate = 100.0 * passed / len(relevant)
        threshold = _resolve_rt_threshold(rt, resources, resource_thresholds)
        if rate < threshold:
            below.append(
                f"{rt} ({round(rate, 1)}% < {threshold}% over {len(relevant)} checks)"
            )
    return below
