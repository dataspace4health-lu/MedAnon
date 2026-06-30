"""Trust Gate engine — Kahn measurement + OHDSI-DQD scoring + policy decision.

Measurement (Kahn 2016 / OHDSI DQD): run conformance/completeness/plausibility
checks, each scored as a violation fraction vs a per-check threshold. The
category and overall scores are the **% of applicable checks passing** — there
are no arbitrary domain weights.

Decision (product fitness-for-use policy, NOT Kahn):
  - a FAILed **critical** check (structural / profile invalidity) → BLOCK
    (the data is not trustworthy enough to transform);
  - else overall pass-rate < ``PASS_MIN_RATE``, any Kahn category below
    ``CATEGORY_MIN_RATE``, or missing dataset provenance → CONDITIONAL_PASS;
  - else PASS.
All cut-offs are env/config-driven (see ``constants``).
"""

from __future__ import annotations

import logging
import math
import os

from checks import (
    accuracy,
    clinical_eval,
    completeness,
    conformance,
    dqd,
    governance,
    identity,
    plausibility,
    timeliness,
)
from constants import CATEGORY_MIN_RATE, PASS_MIN_RATE, SAMPLER_SEED, is_deterministic
from passport import (
    BLOCK,
    CATEGORIES,
    CONDITIONAL_PASS,
    PASS,
    RESULT_FAIL,
    RESULT_NA,
    RESULT_PASS,
    CheckResult,
    QualityPassport,
)
from dimensions import (
    ALL_DIMENSIONS,
    dimension_for_builtin,
    dimension_for_kind,
    grade_for,
)
from phases import (
    ALL_PHASES,
    PROVENANCE,
    SOURCE_ACCURACY,
    STRUCTURAL,
    TEMPORAL_PLAUSIBILITY,
    TERMINOLOGY,
    VALUE_PLAUSIBILITY,
    normalize_selection,
    phase_for_builtin,
    phase_for_kind,
)
from profiling import profile as build_profile
from rules import PlausibilityRule
from terminology_client import TerminologyClient
from validator_client import ValidatorClient

_log = logging.getLogger("trust_gate.engine")

# Data-driven OMOP value/completeness checks (loaded once; small static config).
_DQD_VALUE_CHECKS = dqd.load_dqd_value_checks()


def _category_and_overall(
    det_checks: list[CheckResult],
) -> tuple[dict[str, float | None], float, bool]:
    """Kahn category pass-rates + overall pass-rate over deterministic checks.

    Shared by the FHIR (``assess``) and OMOP (``assess_omop``) paths so the
    scoring is identical. Returns (category_scores, overall, has_assessed).
    """
    category_scores: dict[str, float | None] = {}
    for cat in CATEGORIES:
        cat_checks = [c for c in det_checks if c.category == cat]
        assessed = [c for c in cat_checks if c.result != RESULT_NA]
        if not assessed:
            category_scores[cat] = None
        else:
            passed = sum(1 for c in assessed if c.result == RESULT_PASS)
            category_scores[cat] = 100.0 * passed / len(assessed)
    assessed_all = [c for c in det_checks if c.result != RESULT_NA]
    passed_all = sum(1 for c in assessed_all if c.result == RESULT_PASS)
    overall = 100.0 * passed_all / len(assessed_all) if assessed_all else 100.0
    return category_scores, overall, bool(assessed_all)


def _elevate_critical(
    det_checks: list[CheckResult], critical_check_ids: list[str] | None
) -> None:
    """Mark the use case's critical-to-quality checks as critical (RBQM)."""
    if not critical_check_ids:
        return
    ctq = set(critical_check_ids)
    for c in det_checks:
        if c.check_id in ctq:
            c.critical = True


def _blockers(det_checks: list[CheckResult]) -> list[str]:
    """Plain-language blocker lines for failed critical checks (shared)."""
    return [
        f"{c.check_id}: {c.violations}/{c.applicable} violating "
        f"({round(c.violation_fraction * 100, 1)}%) — {c.recommendation}".strip()
        for c in det_checks
        if c.critical and c.result == RESULT_FAIL
    ]


def _decide(
    category_scores: dict[str, float | None],
    overall: float,
    blockers: list[str],
    rt_below: list[str],
    provenance_capped: bool,
) -> tuple[str, bool]:
    """The single decision policy (shared by both assess paths)."""
    below = [
        cat for cat, s in category_scores.items()
        if s is not None and s < CATEGORY_MIN_RATE
    ]
    if blockers:
        return BLOCK, False
    if overall < PASS_MIN_RATE or below or rt_below or provenance_capped:
        return CONDITIONAL_PASS, True
    return PASS, True


def _subset_decision(
    checks: list[CheckResult],
    *,
    provenance_capped: bool = False,
    rt_below: list[str] | None = None,
) -> tuple[str, float, bool]:
    """Decide over a *subset* of checks using the SAME policy as the headline.

    Sub-reports (per-sector, per-phase) must never be more lenient than the
    overall verdict. Routing them through the shared ``_category_and_overall`` +
    ``_decide`` (instead of an inline "blockers + below PASS_MIN_RATE" rule)
    guarantees the ``CATEGORY_MIN_RATE`` floor — and any provenance cap — applies
    identically. Returns (decision, overall_score, has_assessed).
    """
    category_scores, overall, has_assessed = _category_and_overall(checks)
    decision, _allowed = _decide(
        category_scores, overall, _blockers(checks), rt_below or [], provenance_capped
    )
    return decision, overall, has_assessed


def assess(
    resources: list[dict],
    *,
    dataset_id: str = "dataset",
    source_types: list[str] | None = None,
    config_profile: str = "auto",
    provenance: dict | None = None,
    plausibility_rules: list[PlausibilityRule] | None = None,
    threshold_overrides: dict[str, float] | None = None,
    validator: ValidatorClient | None = None,
    terminology_client: TerminologyClient | None = None,
    baseline_store=None,
    definitional_bounds: dict | None = None,
    concordance_rules: list[dict] | None = None,
    resource_thresholds: dict[str, float] | None = None,
    phases: list[str] | None = None,
    full_urls: list[str] | None = None,
    targets: list[dict] | None = None,
    intended_use: str | None = None,
    reference: dict | None = None,
    lifecycle_stage: str = "operation",
    org_role: str = "data-receiving",
    critical_check_ids: list[str] | None = None,
    ig: str | None = None,
    external_validation: bool = True,
) -> QualityPassport:
    source_types = source_types or ["fhir"]
    provenance = provenance or {}
    extraction_time = provenance.get("extraction_time") if isinstance(provenance, dict) else None
    # Single stable "now" anchor for time-relative plausibility (not_in_future):
    # prefer provenance.extraction_time (part of the input → fully reproducible
    # verdict), else one assessment timestamp captured here and held constant for
    # the whole pass (no per-resource clock drift; recorded for audit below).
    reference_time = extraction_time or QualityPassport.now_iso()
    # Implementation-Guide (US Core etc.) conformance is opt-in: resolved from the
    # explicit ``ig`` arg or TRUST_GATE_IG_PROFILE. None → the IG check is inert.
    ig_profiles = conformance.resolve_ig_profiles(ig or os.environ.get("TRUST_GATE_IG_PROFILE"))

    # Selectable-suite tuning: run only the phases the caller asked for (None/empty
    # → all). Deselecting the structural/terminology phase also skips the two slow
    # external calls (FHIR validator / terminology server).
    selection = normalize_selection(phases)

    checks = _run_checks(
        resources,
        selection,
        validator=validator,
        terminology_client=terminology_client,
        threshold_overrides=threshold_overrides,
        plausibility_rules=plausibility_rules,
        baseline_store=baseline_store,
        definitional_bounds=definitional_bounds,
        concordance_rules=concordance_rules,
        full_urls=full_urls,
        extraction_time=extraction_time,
        reference_time=reference_time,
        reference=reference,
        ig_profiles=ig_profiles,
        external_validation=external_validation,
    )

    auditability = governance.evaluate(resources, provenance)

    # Determinism split (Phase 1.5): the verdict is computed ONLY from the
    # deterministic check set. Statistical/batch-relative checks (outlier, drift)
    # are non-deterministic, so they are reported as an advisory and never move
    # the BLOCK/PASS/CONDITIONAL decision, the category scores, or the scorecard.
    det_checks = [c for c in checks if not c.advisory]
    adv_checks = [c for c in checks if c.advisory]

    # RBQM critical-to-quality (Phase 6.2): elevate the use case's CtQ checks so a
    # failure there blocks (ICH E6(R3): check critical data deeply).
    _elevate_critical(det_checks, critical_check_ids)

    # Category pass-rates + overall (Kahn roll-up over deterministic checks).
    category_scores, overall, has_assessed = _category_and_overall(det_checks)

    # Per-resource-type threshold check (Phase 2A — ONC ASTP calibration).
    # For each resource type present in the batch, compute the Kahn pass-rate
    # for checks that apply to that type and compare against its tuned threshold.
    rt_below: list[str] = []
    if resource_thresholds:
        rt_below = _check_resource_type_thresholds(resources, det_checks, resource_thresholds)

    # Decision policy (deterministic checks only).
    blockers = _blockers(det_checks)
    # Provenance only caps the verdict when its phase is actually being audited.
    provenance_capped = PROVENANCE in selection and not auditability.get(
        "provenance_present", False
    )
    decision, allowed = _decide(category_scores, overall, blockers, rt_below, provenance_capped)

    phase_report = _phase_report(det_checks, selection)

    # Per-sector audit: re-run the selected phases scoped to each target so each
    # sector (resource type / code system) gets its own independent verdict. The
    # baseline reservoir was already updated over the full batch above, so target
    # runs are read-only against it (baseline_store=None) to avoid double-counting.
    targets_report = _targets_report(
        resources,
        targets or [],
        selection,
        provenance_capped=provenance_capped,
        validator=validator,
        terminology_client=terminology_client,
        threshold_overrides=threshold_overrides,
        plausibility_rules=plausibility_rules,
        definitional_bounds=definitional_bounds,
        concordance_rules=concordance_rules,
        full_urls=full_urls,
        extraction_time=extraction_time,
        reference_time=reference_time,
        reference=reference,
        ig_profiles=ig_profiles,
        external_validation=external_validation,
    )

    # Assessment-coverage transparency (deterministic): how much of the suite
    # actually ran, so the headline grade/fitness cannot over-claim. Computed
    # BEFORE fitness so the use-bound approvals can require external validation.
    coverage = _coverage(
        det_checks,
        selection=selection,
        validator=validator,
        terminology_client=terminology_client,
        reference=reference,
        concordance_rules=concordance_rules,
        plausibility_rules=plausibility_rules,
        external_validation=external_validation,
    )

    approved_for, not_approved_for = _fitness(
        decision,
        auditability,
        category_scores=category_scores,
        blockers=blockers,
        intended_use=intended_use,
        external_validation_performed=coverage.get("external_validation_performed", False),
    )

    # Per-dimension scorecard + overall grade + purpose-bound fitness statement
    # (deterministic checks only — reproducible).
    scorecard = _scorecard(det_checks)
    overall_grade = _grade_with_floor(overall, det_checks) if has_assessed else None
    fitness = _fitness_statement(
        decision, overall, overall_grade, intended_use, coverage
    )

    # Statistical advisory + evaluation-environment provenance (Phase 1.5).
    advisory = _advisory_report(adv_checks)
    evaluation = {
        "decision_basis": "deterministic",
        "sampler_seed": SAMPLER_SEED,
        "advisory_check_ids": sorted({c.check_id for c in adv_checks}),
        "validator_used": validator is not None and STRUCTURAL in selection,
        "terminology_used": terminology_client is not None and TERMINOLOGY in selection,
        # The "now" anchor used for time-relative rules (not_in_future). When it
        # equals provenance.extraction_time the verdict is fully reproducible;
        # otherwise it is the recorded assessment timestamp, so a verdict can
        # still be explained and re-derived against the same anchor.
        "assessment_reference_time": reference_time,
        # Transparent-reporting attribution (Phase 4): where in the lifecycle and
        # by which organization role this assessment ran (Wassell et al. 2026).
        "lifecycle_stage": lifecycle_stage,
        "org_role": org_role,
        # Deequ-style suggested tolerances (advisory only — never auto-applied).
        "suggested_thresholds": _suggest_thresholds(det_checks),
    }

    # Surface per-resource-type calibration results in auditability.
    if rt_below:
        auditability = {**auditability, "resource_type_below_threshold": rt_below}

    return QualityPassport(
        profile=build_profile(resources),
        dataset_id=dataset_id,
        source_types=source_types,
        decision=decision,
        overall_score=overall,
        has_assessed=has_assessed,
        category_scores=category_scores,
        checks=checks,
        auditability=auditability,
        blockers=blockers,
        approved_for=approved_for,
        not_approved_for=not_approved_for,
        provenance=provenance,
        generated_at=QualityPassport.now_iso(),
        privacy_processing_allowed=allowed,
        resource_count=sum(1 for r in resources if isinstance(r, dict)),
        config_profile=config_profile,
        phases=phase_report,
        targets=targets_report,
        scorecard=scorecard,
        overall_grade=overall_grade,
        fitness=fitness,
        advisory=advisory,
        evaluation=evaluation,
        coverage=coverage,
    )


def assess_omop(
    omop,
    *,
    dataset_id: str = "dataset",
    source_types: list[str] | None = None,
    config_profile: str = "auto",
    provenance: dict | None = None,
    intended_use: str | None = None,
    value_checks: dict | None = None,
    lifecycle_stage: str = "operation",
    org_role: str = "data-receiving",
    critical_check_ids: list[str] | None = None,
) -> QualityPassport:
    """Assess an OMOP CDM dataset with the DQD-style check suite.

    Reuses the shared scoring/decision/scorecard machinery so an OMOP passport is
    structurally identical to a FHIR one. All DQD checks are deterministic, so the
    whole verdict is reproducible (Phase 1.5).
    """
    source_types = source_types or ["omop"]
    provenance = provenance or {}
    checks = dqd.run_dqd_checks(
        omop, value_checks if value_checks is not None else _DQD_VALUE_CHECKS
    )
    # Clinical evaluation (Phase 3B): demographic-stratified outlier advisory +
    # deterministic clinical-logic coherence over the OMOP measurement table.
    checks.extend(clinical_eval.evaluate(omop))
    for c in checks:
        c.advisory = not is_deterministic(c.check_id)
    det_checks = [c for c in checks if not c.advisory]
    adv_checks = [c for c in checks if c.advisory]
    _elevate_critical(det_checks, critical_check_ids)

    category_scores, overall, has_assessed = _category_and_overall(det_checks)
    blockers = _blockers(det_checks)
    decision, allowed = _decide(category_scores, overall, blockers, [], False)

    _omop_assessed = sum(1 for c in det_checks if c.result != RESULT_NA)
    coverage = {
        "checks_assessed": _omop_assessed,
        "checks_total": len(det_checks),
        "assessed_fraction": round(_omop_assessed / len(det_checks), 3)
        if det_checks
        else None,
        # The OHDSI-DQD suite is the model-appropriate comprehensive check set for
        # OMOP; there are no external validator/terminology gaps on this path.
        "validation_depth": "comprehensive",
        "not_exercised": [],
        "source_model": "omop",
    }
    scorecard = _scorecard(det_checks)
    overall_grade = _grade_with_floor(overall, det_checks) if has_assessed else None
    fitness = _fitness_statement(
        decision, overall, overall_grade, intended_use, coverage
    )
    approved_for, not_approved_for = _fitness(
        decision,
        {"provenance_present": bool(provenance)},
        category_scores=category_scores,
        blockers=blockers,
        intended_use=intended_use,
        # The OHDSI DQD suite is the model-appropriate validation for OMOP.
        external_validation_performed=True,
    )
    advisory = _advisory_report(adv_checks)
    evaluation = {
        "decision_basis": "deterministic",
        "sampler_seed": SAMPLER_SEED,
        "advisory_check_ids": sorted({c.check_id for c in adv_checks}),
        "validator_used": False,
        "terminology_used": False,
        "source_model": "omop",
        "lifecycle_stage": lifecycle_stage,
        "org_role": org_role,
        "suggested_thresholds": _suggest_thresholds(det_checks),
    }
    obs_stats, obs_stats_strat = clinical_eval.value_distributions(omop)
    profile = {
        "total_rows": omop.total_rows(),
        "table_row_counts": {t: len(omop.rows(t)) for t in omop.present_tables()},
        "observation_value_stats": obs_stats,
        "observation_value_stats_stratified": obs_stats_strat,
        # Coverage transparency: which submitted tables were not assessed, so a
        # PASS is not mistaken for a verdict over the whole submission.
        "unmodelled_submitted_tables": sorted(set(omop.ignored_tables)),
    }

    return QualityPassport(
        profile=profile,
        dataset_id=dataset_id,
        source_types=source_types,
        decision=decision,
        overall_score=overall,
        has_assessed=has_assessed,
        category_scores=category_scores,
        checks=checks,
        auditability={},
        blockers=blockers,
        approved_for=approved_for,
        not_approved_for=not_approved_for,
        provenance=provenance,
        generated_at=QualityPassport.now_iso(),
        privacy_processing_allowed=allowed,
        resource_count=len(omop.rows("person")),
        config_profile=config_profile,
        phases=_phase_report(det_checks, set(ALL_PHASES)),
        targets={},
        scorecard=scorecard,
        overall_grade=overall_grade,
        fitness=fitness,
        advisory=advisory,
        evaluation=evaluation,
        coverage=coverage,
    )


def _suggest_thresholds(det_checks: list[CheckResult]) -> dict[str, float]:
    """Deequ-style suggested tolerances, derived from the observed violation rate.

    For each assessed deterministic check that is currently failing (observed
    violation fraction above its threshold), suggest the smallest tolerance that
    would admit this batch: the observed fraction, rounded up. ADVISORY ONLY — the
    verdict and scoring still use the configured thresholds; this is a tuning hint
    a reviewer can adopt deliberately. Deterministic (no randomness), so it does
    not affect reproducibility.
    """
    suggestions: dict[str, float] = {}
    for c in det_checks:
        if c.result == RESULT_NA or c.applicable <= 0:
            continue
        observed = c.violations / c.applicable
        if observed > c.threshold:
            # Round up to 3 dp so re-running this exact batch would pass.
            suggestions[c.check_id] = math.ceil(observed * 1000) / 1000
    return suggestions


def _advisory_report(adv_checks: list[CheckResult]) -> dict:
    """Statistical, batch-relative findings (outlier / drift).

    Reported for analysis but explicitly NOT part of the verdict: these checks are
    non-deterministic (random reservoir + batch-relative distribution), so folding
    them into the decision would make it irreproducible (Phase 1.5).
    """
    flagged = [c.check_id for c in adv_checks if c.result == RESULT_FAIL]
    return {
        "note": (
            "Statistical, batch-relative signal. Reported for analysis; does not "
            "affect the PASS/CONDITIONAL/BLOCK verdict (non-deterministic)."
        ),
        "flagged": flagged,
        "checks": [
            {
                "check_id": c.check_id,
                "dimension": c.dimension,
                "result": c.result,
                "applicable": c.applicable,
                "violations": c.violations,
                "violation_fraction": round(c.violation_fraction, 4),
            }
            for c in adv_checks
        ],
    }


def _grade_with_floor(score: float | None, checks: list[CheckResult]) -> str | None:
    """Letter grade that is non-compensatory w.r.t. critical failures.

    A failed *critical* check (one that forces a BLOCK) must not be averaged away
    by unrelated passing checks — DAMA DMBOK and ISO/IEC 25012 treat an
    inherent-characteristic failure as non-compensable. When any critical check in
    *checks* FAILed, the grade floors to "F" regardless of the pass-rate; otherwise
    it follows the conventional band in ``grade_for``. This is what stops a
    headline (or dimension) grade reading "A" while the decision is BLOCK.
    """
    if score is None:
        return None
    if any(c.critical and c.result == RESULT_FAIL for c in checks):
        return "F"
    return grade_for(score)


def _scorecard(checks: list[CheckResult]) -> dict[str, dict]:
    """Per-DQ-dimension roll-up (DAMA/ISO 25012): score + letter grade.

    A dimension appears only when at least one of its checks ran. The score is the
    % of *assessed* (non-NA) checks passing for that dimension; the grade follows
    ``_grade_with_floor`` so a critical failure in the dimension floors it to F
    rather than being averaged away by passing checks.
    """
    report: dict[str, dict] = {}
    for dim in ALL_DIMENSIONS:
        dim_checks = [c for c in checks if c.dimension == dim]
        if not dim_checks:
            continue
        assessed = [c for c in dim_checks if c.result != RESULT_NA]
        passed = sum(1 for c in assessed if c.result == RESULT_PASS)
        score = (100.0 * passed / len(assessed)) if assessed else None
        report[dim] = {
            "score": round(score, 1) if score is not None else None,
            "grade": _grade_with_floor(score, dim_checks),
            "checks_assessed": len(assessed),
            "checks_passed": passed,
        }
    return report


# Deterministic "depth" capabilities: each, when enabled, contributes
# verdict-driving checks beyond the always-on structural/format/completeness
# core. Their absence is exactly what a thin assessment hides, so the passport
# reports it rather than letting the headline grade imply full coverage.
_DEPTH_CAPABILITIES: tuple[str, ...] = (
    "structural_validation",
    "terminology_validation",
    "reference_accuracy",
    "cross_field_concordance",
    "clinical_rule_pack",
)


def _coverage(
    det_checks: list[CheckResult],
    *,
    selection: set[str],
    validator: ValidatorClient | None,
    terminology_client: TerminologyClient | None,
    reference: dict | None,
    concordance_rules: list[dict] | None,
    plausibility_rules: list[PlausibilityRule] | None,
    external_validation: bool,
) -> dict:
    """Assessment-coverage transparency: how much was actually evaluated.

    A grade computed over 7 of 22 checks must not read like one over 22 of 22.
    Reports assessed/total counts plus the deterministic *depth* capabilities
    that were NOT exercised because their dependency/input was absent. Only
    selected phases count — deselecting a phase is a deliberate scoping choice,
    not a hidden gap. Fully deterministic (derived from check results + which
    inputs were supplied), so it never affects reproducibility.
    """
    assessed = sum(1 for c in det_checks if c.result != RESULT_NA)
    total = len(det_checks)

    not_exercised: list[str] = []
    if STRUCTURAL in selection and (validator is None or not external_validation):
        not_exercised.append("structural_validation")
    if TERMINOLOGY in selection and terminology_client is None:
        not_exercised.append("terminology_validation")
    has_reference = isinstance(reference, dict) and bool(reference.get("records"))
    if SOURCE_ACCURACY in selection and not has_reference:
        not_exercised.append("reference_accuracy")
    if VALUE_PLAUSIBILITY in selection and not concordance_rules:
        not_exercised.append("cross_field_concordance")
    if TEMPORAL_PLAUSIBILITY in selection and not plausibility_rules:
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
    # verdict is verification-only — surface that rather than letting the grade
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
    # Structural conformance validates every resource (RC4) — disclose how many
    # were actually validated (the structural check's applicable count) so a PASS
    # is read as whole-dataset, not sampled.
    if STRUCTURAL in selection and validator is not None and external_validation:
        struct_chk = next(
            (c for c in det_checks if c.check_id == "conformance.structural"), None
        )
        if struct_chk is not None and struct_chk.result != RESULT_NA:
            cov["structural_validated_count"] = struct_chk.applicable
    cov["caveat"] = _coverage_caveat(cov)
    return cov


def _coverage_caveat(coverage: dict | None) -> str:
    """Fitness caveat naming what limited the assessment: depth capabilities that
    did not run, phases that were descoped, and — most importantly — whether ANY
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


def _fitness_statement(
    decision: str,
    overall: float,
    overall_grade: str | None,
    intended_use: str | None,
    coverage: dict | None = None,
) -> dict:
    """Purpose-bound fitness verdict — quality is fitness for a *declared* use
    (Juran; Wang & Strong 1996), so the statement always names the use. A
    coverage caveat is appended so the verdict cannot over-claim relative to how
    much of the suite actually ran."""
    use = (intended_use or "secondary use (general)").strip()
    pct = round(overall, 1)
    if overall_grade is None:
        grade_str = "not assessed"
        pct_str = "no applicable checks"
    else:
        grade_str = f"grade {overall_grade}"
        pct_str = f"{pct}% checks passing"
    if decision == BLOCK:
        fit = False
        statement = (
            f"Not fit for {use}: blocking quality failures must be resolved "
            f"({grade_str}, {pct_str})."
        )
    elif decision == CONDITIONAL_PASS:
        fit = True
        statement = (
            f"Conditionally fit for {use}: {grade_str} ({pct_str}) — "
            f"remediate the flagged dimensions before high-stakes use."
        )
    else:
        fit = True
        statement = f"Fit for {use}: {grade_str} ({pct_str})."
    # Reuse the caveat precomputed in _coverage (single source of truth).
    caveat = (coverage or {}).get("caveat") or _coverage_caveat(coverage)
    if caveat:
        statement = f"{statement} {caveat}"
    return {
        "intended_use": use,
        "overall_grade": overall_grade,
        "fit": fit,
        "statement": statement,
        "coverage_caveat": caveat,
    }


def _run_checks(
    resources: list[dict],
    selection: set[str],
    *,
    validator: ValidatorClient | None,
    terminology_client: TerminologyClient | None,
    threshold_overrides: dict[str, float] | None,
    plausibility_rules: list[PlausibilityRule] | None,
    baseline_store,
    definitional_bounds: dict | None,
    concordance_rules: list[dict] | None,
    full_urls: list[str] | None,
    extraction_time: str | None = None,
    reference_time: str | None = None,
    reference: dict | None = None,
    ig_profiles: dict[str, list[str]] | None = None,
    external_validation: bool = True,
) -> list[CheckResult]:
    """Produce the checks for *resources*, phase-tag them, keep only selected phases.

    Shared by the whole-batch pass and each per-sector target pass.

    ``reference_time`` is the single stable "now" anchor for time-relative
    plausibility rules (``not_in_future``) — held constant across the whole
    assessment so the verdict has no per-resource clock drift (see
    ``rules.evaluate``).
    """
    checks: list[CheckResult] = []
    checks.extend(
        conformance.evaluate(
            resources,
            validator,
            terminology_client,
            threshold_overrides,
            # external_validation=False skips ONLY the slow external FHIR-validator
            # calls (structural/profile/IG); the in-process structural checks
            # (value_format, coding_structure, id/type present, references) still run.
            run_validator=external_validation and STRUCTURAL in selection,
            run_terminology=TERMINOLOGY in selection,
            full_urls=full_urls,
            ig_profiles=ig_profiles,
        )
    )
    checks.extend(completeness.evaluate(resources, threshold_overrides))
    checks.extend(
        plausibility.evaluate(
            resources,
            plausibility_rules or [],
            threshold_overrides,
            baseline_store=baseline_store,
            definitional_bounds=definitional_bounds,
            concordance_rules=concordance_rules,
            reference_time=reference_time,
        )
    )
    checks.extend(
        timeliness.evaluate(
            resources, threshold_overrides, extraction_time=extraction_time
        )
    )
    checks.append(
        accuracy.evaluate_against_reference(resources, reference, threshold_overrides)
    )
    checks.append(identity.evaluate_patient_identity(resources, threshold_overrides))
    provenance_sam = governance.evaluate_provenance_sam(resources, threshold_overrides)
    if provenance_sam is not None:
        checks.append(provenance_sam)

    rule_phase = {r.rule_id: phase_for_kind(r.kind) for r in (plausibility_rules or [])}
    rule_dim = {r.rule_id: dimension_for_kind(r.kind) for r in (plausibility_rules or [])}
    rule_ids = frozenset(rule_phase)
    for c in checks:
        c.phase = phase_for_builtin(c.check_id) or rule_phase.get(c.check_id, "")
        c.dimension = dimension_for_builtin(c.check_id) or rule_dim.get(c.check_id, "")
        c.advisory = not is_deterministic(c.check_id, rule_ids)
    return [c for c in checks if c.phase in selection]


def _coding_systems(resource: dict) -> set[str]:
    """All distinct coding.system URIs anywhere in a resource."""
    systems: set[str] = set()
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for coding in node.get("coding", []) or []:
                if isinstance(coding, dict) and isinstance(coding.get("system"), str):
                    systems.add(coding["system"])
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    return systems


def _fhirpath_match(resource: dict, expr: str) -> bool:
    """Evaluate a FHIRPath cohort predicate against a resource (fail-safe).

    A boolean expression (e.g. ``gender = 'female'``) matches on its truth value;
    a path expression (e.g. ``telecom``) matches when it yields any value. A
    FHIRPath error never raises — it is treated as "does not match" so a bad
    predicate cannot crash the gate (mirrors the rule engine's fail-safe eval).
    """
    try:
        from fhirpathpy import evaluate as _eval

        result = _eval(resource, expr) or []
    except Exception:  # noqa: BLE001 — FHIRPath errors must not crash the gate
        return False
    if not result:
        return False
    if all(isinstance(x, bool) for x in result):
        return any(result)
    return True


def _matches_target(resource: dict, target: dict) -> bool:
    """A resource belongs to a sector target when it matches every constraint set
    on the target (resource_types, code_systems, and/or a FHIRPath cohort
    predicate). An empty target matches everything."""
    if not isinstance(resource, dict):
        return False
    rtypes = target.get("resource_types")
    if rtypes and resource.get("resourceType") not in set(rtypes):
        return False
    csystems = target.get("code_systems")
    if csystems and not (_coding_systems(resource) & set(csystems)):
        return False
    expr = target.get("fhirpath")
    if expr and not _fhirpath_match(resource, expr):
        return False
    return True


def _target_label(target: dict) -> str:
    parts = []
    if target.get("resource_types"):
        parts.append("+".join(target["resource_types"]))
    if target.get("code_systems"):
        parts.append("|".join(target["code_systems"]))
    if target.get("fhirpath"):
        parts.append(f"[{target['fhirpath']}]")
    return " ".join(parts) or "all"


def _targets_report(
    resources: list[dict],
    targets: list[dict],
    selection: set[str],
    *,
    provenance_capped: bool = False,
    **run_kwargs,
) -> dict[str, dict]:
    """Per-sector verdicts: scope the resource set to each target, re-run the
    selected phases, and roll up an independent decision *under the same policy
    as the headline* (see ``_subset_decision``)."""
    report: dict[str, dict] = {}
    for tgt in targets:
        if not isinstance(tgt, dict):
            continue
        tid = tgt.get("id") or _target_label(tgt)
        subset = [r for r in resources if _matches_target(r, tgt)]
        if not subset:
            report[tid] = {
                "decision": RESULT_NA,
                "score": None,
                "resource_count": 0,
                "checks_assessed": 0,
                "checks_passed": 0,
                "phases": {},
                "blockers": [],
            }
            continue
        tchecks = _run_checks(subset, selection, baseline_store=None, **run_kwargs)
        det_tchecks = [c for c in tchecks if not c.advisory]
        assessed = [c for c in det_tchecks if c.result != RESULT_NA]
        passed = sum(1 for c in assessed if c.result == RESULT_PASS)
        # A sector is a slice of the same dataset, so a missing-provenance cap on
        # the headline applies to it too — pass it through so a sector can never
        # badge PASS while the headline is CONDITIONAL.
        decision, score, has_assessed = _subset_decision(
            det_tchecks, provenance_capped=provenance_capped
        )
        block_ids = [
            c.check_id for c in det_tchecks if c.critical and c.result == RESULT_FAIL
        ]
        report[tid] = {
            "decision": decision,
            "score": round(score, 1) if has_assessed else None,
            "resource_count": len(subset),
            "checks_assessed": len(assessed),
            "checks_passed": passed,
            "phases": _phase_report(det_tchecks, selection),
            "blockers": block_ids,
        }
    return report


def _phase_report(checks: list[CheckResult], selection: set[str]) -> dict[str, dict]:
    """Per-phase verdict roll-up (selectable-suite reporting).

    Each phase's decision is computed by the SAME shared policy as the headline
    (``_subset_decision`` → ``_decide``), so a phase honours the ``CATEGORY_MIN_RATE``
    floor and can never read more leniently than the overall verdict. The
    dataset-level provenance cap is intentionally NOT applied per-phase (the
    provenance concern surfaces in its own phase, not by downgrading e.g. the
    structural phase). NA (skipped) checks are excluded from the rate.
    """
    report: dict[str, dict] = {}
    for ph in sorted(selection):
        ph_checks = [c for c in checks if c.phase == ph]
        if not ph_checks:
            continue
        assessed = [c for c in ph_checks if c.result != RESULT_NA]
        passed = sum(1 for c in assessed if c.result == RESULT_PASS)
        ph_decision, score, has_assessed = _subset_decision(ph_checks)
        ph_blockers = [
            c.check_id for c in ph_checks if c.critical and c.result == RESULT_FAIL
        ]
        report[ph] = {
            "decision": ph_decision,
            "score": round(score, 1) if has_assessed else None,
            "checks_assessed": len(assessed),
            "checks_passed": passed,
            "blockers": ph_blockers,
        }
    return report


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
        mapped = {_OBSERVATION_SUBTYPE_KEYS[c] for c in codes if c in _OBSERVATION_SUBTYPE_KEYS}
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


def _check_resource_type_thresholds(
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
            c
            for c in checks
            if c.result != RESULT_NA and c.resource_type in ("", rt)
        ]
        if not relevant:
            continue
        passed = sum(1 for c in relevant if c.result == RESULT_PASS)
        rate = 100.0 * passed / len(relevant)
        threshold = _resolve_rt_threshold(rt, resources, resource_thresholds)
        if rate < threshold:
            below.append(f"{rt} ({round(rate, 1)}% < {threshold}% over {len(relevant)} checks)")
    return below


# Use-case → quality requirements (Kahn 2012: fitness is task-dependent — a
# dataset fit for cohort discovery may be unfit for outcomes research). Each use
# names the Kahn category floors it needs (at CATEGORY_MIN_RATE — not a separate
# magic number) plus whether it requires dataset provenance and external
# validation. The caller's free-text ``intended_use`` is keyword-matched to a
# profile. Ordered loosest → strictest; ``general`` is the fallback.
_USE_PROFILES: tuple[tuple[str, dict], ...] = (
    ("cohort discovery / feasibility", {
        "categories": ("conformance",),
        "require_provenance": False,
        "require_external_validation": False,
        "keywords": ("cohort", "feasibil", "discovery", "recruit", "count"),
    }),
    ("descriptive analytics", {
        "categories": ("conformance", "completeness"),
        "require_provenance": False,
        "require_external_validation": False,
        "keywords": ("descriptive", "analytic", "statistic", "dashboard", "reporting"),
    }),
    ("AI/ML model training", {
        "categories": ("conformance", "completeness", "plausibility"),
        "require_provenance": True,
        "require_external_validation": False,
        "keywords": ("ai", "ml", "model", "training", "machine learning"),
    }),
    ("outcomes / comparative-effectiveness research", {
        "categories": ("conformance", "completeness", "plausibility"),
        "require_provenance": True,
        "require_external_validation": True,
        "keywords": ("outcome", "effectiveness", "comparative", "inference", "causal"),
    }),
    ("regulated submission / external sharing", {
        "categories": ("conformance", "completeness", "plausibility"),
        "require_provenance": True,
        "require_external_validation": True,
        "keywords": ("regulat", "submission", "fda", "ema", "ehds", "sharing"),
    }),
)
_GENERAL_USE = ("secondary use (general)", {
    "categories": ("conformance", "completeness"),
    "require_provenance": False,
    "require_external_validation": False,
    "keywords": (),
})


def _resolve_use_profile(intended_use: str | None) -> tuple[str, dict]:
    """Map free-text intended_use to a canonical use profile (first keyword match)."""
    text = (intended_use or "").lower()
    for label, prof in _USE_PROFILES:
        if any(k in text for k in prof["keywords"]):
            return label, prof
    return _GENERAL_USE


def _unmet_requirements(
    prof: dict,
    category_scores: dict[str, float | None] | None,
    provenance_present: bool,
    external_validation_performed: bool,
) -> list[str]:
    """Requirements of *prof* that this dataset does not meet (empty = fit for it)."""
    reasons: list[str] = []
    scores = category_scores or {}
    for cat in prof["categories"]:
        s = scores.get(cat)
        if s is not None and s < CATEGORY_MIN_RATE:
            reasons.append(f"{cat} below {CATEGORY_MIN_RATE}%")
    if prof["require_provenance"] and not provenance_present:
        reasons.append("missing dataset provenance")
    if prof["require_external_validation"] and not external_validation_performed:
        reasons.append("no external validation performed (verification-only)")
    return reasons


def _fitness(
    decision: str,
    auditability: dict,
    category_scores: dict[str, float | None] | None = None,
    blockers: list[str] | None = None,
    intended_use: str | None = None,
    external_validation_performed: bool = False,
) -> tuple[list[str], list[str]]:
    """Task-dependent fitness-for-use lists (Juran; Wang & Strong 1996; Kahn 2012).

    The declared ``intended_use`` selects a requirement profile; a use is approved
    only when ITS floors are met, so "fit for AI training" and "fit for cohort
    discovery" are no longer the same verdict. Lower-risk uses stay approved when
    their lighter floors hold (so a CONDITIONAL_PASS still says what the data IS
    good for); higher-stakes uses are explicitly not-cleared with the reason.
    """
    if decision == BLOCK:
        not_ok = ["any secondary use until blockers are resolved"]
        if blockers:
            block_checks = ", ".join(b.split(":")[0] for b in blockers[:3])
            not_ok.append(f"privacy processing (blocked by: {block_checks})")
        return ([], not_ok)

    provenance_present = bool(auditability.get("provenance_present", False))
    declared_label, declared_prof = _resolve_use_profile(intended_use)

    def _fit(prof: dict) -> bool:
        return not _unmet_requirements(
            prof, category_scores, provenance_present, external_validation_performed
        )

    approved: list[str] = []
    not_ok: list[str] = []

    # The declared use: approved iff its own requirements hold.
    declared_unmet = _unmet_requirements(
        declared_prof, category_scores, provenance_present, external_validation_performed
    )
    if declared_unmet:
        not_ok.append(f"{declared_label} — requires: {'; '.join(declared_unmet)}")
    else:
        approved.append(declared_label)

    # Enumerate the canonical uses so the provider sees the full fit picture.
    for label, prof in (*_USE_PROFILES, _GENERAL_USE):
        if label == declared_label:
            continue
        if _fit(prof):
            approved.append(label)
        else:
            reasons = _unmet_requirements(
                prof, category_scores, provenance_present, external_validation_performed
            )
            not_ok.append(f"{label} — requires: {'; '.join(reasons)}")

    # De-duplicate while preserving order.
    approved = list(dict.fromkeys(approved))
    not_ok = list(dict.fromkeys(not_ok))
    return (approved, not_ok)
