"""Coverage: assessment-coverage transparency. Reports how much of the suite
actually ran (assessed/total, selected vs descoped phases, verification-vs-validation
breakdown) and a one-line caveat, so a headline grade computed over a thin subset
cannot read as a comprehensive, externally-validated certification.
"""

from __future__ import annotations

from passport import CheckResult, RESULT_NA
from phases import (
    ALL_PHASES,
    SOURCE_ACCURACY,
    STRUCTURAL,
    TEMPORAL_PLAUSIBILITY,
    TERMINOLOGY,
    VALUE_PLAUSIBILITY,
)

from verdict.context import AssessmentContext

# Deterministic "depth" capabilities: each, when enabled, contributes
# verdict-driving checks beyond the always-on structural/format/completeness core.
# Their absence is exactly what a thin assessment hides, so the passport reports it
# rather than letting the headline grade imply full coverage.
_DEPTH_CAPABILITIES: tuple[str, ...] = (
    "structural_validation",
    "terminology_validation",
    "reference_accuracy",
    "cross_field_concordance",
    "clinical_rule_pack",
)


def compute_coverage(det_checks: list[CheckResult], ctx: AssessmentContext) -> dict:
    """Assessment-coverage transparency: how much was actually evaluated.

    A grade computed over 7 of 22 checks must not read like one over 22 of 22.
    Reports assessed/total counts plus the deterministic *depth* capabilities that
    were NOT exercised because their dependency/input was absent, the phases that
    were descoped, and the verification/validation breakdown. Fully deterministic
    (derived from check results + which inputs were supplied), so it never affects
    reproducibility.
    """
    selection = ctx.selection
    assessed = sum(1 for c in det_checks if c.result != RESULT_NA)
    total = len(det_checks)

    not_exercised: list[str] = []
    if STRUCTURAL in selection and (
        ctx.validator is None or not ctx.external_validation
    ):
        not_exercised.append("structural_validation")
    if TERMINOLOGY in selection and ctx.terminology_client is None:
        not_exercised.append("terminology_validation")
    has_reference = isinstance(ctx.reference, dict) and bool(
        ctx.reference.get("records")
    )
    if SOURCE_ACCURACY in selection and not has_reference:
        not_exercised.append("reference_accuracy")
    if VALUE_PLAUSIBILITY in selection and not ctx.concordance_rules:
        not_exercised.append("cross_field_concordance")
    if TEMPORAL_PLAUSIBILITY in selection and not ctx.plausibility_rules:
        not_exercised.append("clinical_rule_pack")

    # Suite-wide scoping: phases NOT selected are a real coverage reduction. By
    # reporting them against the full ALL_PHASES suite, narrowing scope visibly
    # LOWERS coverage instead of (as before) raising assessed_fraction, which was
    # measured only over the post-filter check set.
    descoped_phases = sorted(p for p in ALL_PHASES if p not in selection)

    exercised = [cap for cap in _DEPTH_CAPABILITIES if cap not in not_exercised]
    # "comprehensive" now requires the FULL phase suite AND every depth capability,
    # so a deliberately narrowed run can no longer read as comprehensive.
    if not not_exercised and not descoped_phases:
        depth = "comprehensive"
    elif not exercised and not descoped_phases:
        depth = "structural_only"
    else:
        depth = "partial"

    # Verification (internal consistency) vs validation (against an external
    # reference) roll-up (Kahn 2016). In the default deployment every
    # validation-context check is NA (no validator/terminology/reference), so the
    # verdict is verification-only  surface that rather than letting the grade
    # read as external certification.
    verification_assessed = sum(
        1 for c in det_checks if c.context == "verification" and c.result != RESULT_NA
    )
    validation_assessed = sum(
        1 for c in det_checks if c.context == "validation" and c.result != RESULT_NA
    )

    cov: dict = {
        "checks_assessed": assessed,
        "checks_total": total,
        "assessed_fraction": round(assessed / total, 3) if total else None,
        "phases_selected": len(selection),
        "phases_total": len(ALL_PHASES),
        "descoped_phases": descoped_phases,
        "validation_depth": depth,
        "not_exercised": not_exercised,
        "context_breakdown": {
            "verification_assessed": verification_assessed,
            "validation_assessed": validation_assessed,
        },
        "external_validation_performed": validation_assessed > 0,
    }
    # Structural conformance validates every resource (RC4)  disclose how many
    # were actually validated (the structural check's applicable count) so a PASS
    # is read as whole-dataset, not sampled.
    if (
        STRUCTURAL in selection
        and ctx.validator is not None
        and ctx.external_validation
    ):
        struct_chk = next(
            (c for c in det_checks if c.check_id == "conformance.structural"), None
        )
        if struct_chk is not None and struct_chk.result != RESULT_NA:
            cov["structural_validated_count"] = struct_chk.applicable
    cov["caveat"] = coverage_caveat(cov)
    return cov


def coverage_caveat(coverage: dict | None) -> str:
    """Fitness caveat naming what limited the assessment: depth capabilities that
    did not run, phases that were descoped, and  most importantly  whether ANY
    external validation occurred. Without this, a grade computed entirely from
    verification (internal-consistency) checks reads as external certification.
    """
    if not coverage:
        return ""
    parts: list[str] = []
    missing = coverage.get("not_exercised") or []
    depth = coverage.get("validation_depth")
    if missing:
        pretty = ", ".join(m.replace("_", " ") for m in missing)
        if depth == "structural_only":
            parts.append(
                "Coverage: this verdict reflects structural, format, completeness "
                f"and uniqueness checks only; not exercised: {pretty}."
            )
        else:
            parts.append(f"Coverage: partial; not exercised: {pretty}.")
    descoped = coverage.get("descoped_phases") or []
    if descoped:
        pretty_d = ", ".join(d.replace("_", " ") for d in descoped)
        parts.append(
            f"Scope: {coverage.get('phases_selected')} of "
            f"{coverage.get('phases_total')} phases assessed; descoped: {pretty_d}."
        )
    if coverage.get("external_validation_performed") is False:
        parts.append(
            "No external validation performed (verification-only): the verdict "
            "rests on internal-consistency checks; no terminology, profile, or "
            "reference validation was assessable."
        )
    return " ".join(parts)
