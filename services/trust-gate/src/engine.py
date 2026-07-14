"""Trust Gate engine — the assessment pipeline.

Thin orchestrator over the ``verdict`` package. ``assess`` runs the pipeline for a
FHIR resource set; ``assess_omop`` for an OMOP CDM dataset. Both follow the same
stages: run checks → split deterministic vs advisory → score → decide → coverage →
fitness → assemble the QualityPassport.

Measurement (Kahn 2016 / OHDSI DQD): each check is scored as a violation fraction
vs a per-check threshold; category/overall scores are the % of applicable checks
passing (no arbitrary domain weights). Decision (product fitness-for-use policy,
NOT Kahn): a failed *critical* check → BLOCK; else overall < ``PASS_MIN_RATE``, any
Kahn category below ``CATEGORY_MIN_RATE``, or missing provenance → CONDITIONAL_PASS;
else PASS. See ``verdict.decision``.
"""

from __future__ import annotations

import logging
import os

from checks import clinical_eval, conformance, dqd, governance
from constants import SAMPLER_SEED, is_deterministic
from passport import QualityPassport, RESULT_NA
from phases import ALL_PHASES, PROVENANCE, STRUCTURAL, TERMINOLOGY, normalize_selection
from profiling import profile as build_profile
from rules import PlausibilityRule
from terminology_client import TerminologyClient
from validator_client import ValidatorClient

from verdict.context import AssessmentContext
from verdict.coverage import compute_coverage
from verdict.decision import (
    blockers,
    regulated_blockers,
    check_resource_type_thresholds,
    decide,
    elevate_critical,
)
from verdict.fitness import compute_fitness, fitness_statement
from verdict.runner import phase_report, run_checks, targets_report
from verdict.scoring import (
    advisory_report,
    category_and_overall,
    grade_with_floor,
    scorecard,
    suggest_thresholds,
)

_log = logging.getLogger("trust_gate.engine")

# Data-driven OMOP value/completeness checks (loaded once; small static config).
_DQD_VALUE_CHECKS = dqd.load_dqd_value_checks()


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
    extraction_time = (
        provenance.get("extraction_time") if isinstance(provenance, dict) else None
    )
    # Single stable "now" anchor for time-relative plausibility (not_in_future):
    # prefer provenance.extraction_time (part of the input → fully reproducible
    # verdict), else one assessment timestamp captured here and held constant for
    # the whole pass (no per-resource clock drift; recorded for audit below).
    reference_time = extraction_time or QualityPassport.now_iso()
    # Implementation-Guide (US Core etc.) conformance is opt-in: resolved from the
    # explicit ``ig`` arg or TRUST_GATE_IG_PROFILE. None → the IG check is inert.
    ig_profiles = conformance.resolve_ig_profiles(
        ig or os.environ.get("TRUST_GATE_IG_PROFILE")
    )

    # Selectable-suite tuning: run only the phases the caller asked for (None/empty
    # → all). Deselecting the structural/terminology phase also skips the two slow
    # external calls (FHIR validator / terminology server).
    selection = normalize_selection(phases)

    # Bundle every check input into one immutable context (Parameter Object). The
    # runner, coverage, and each per-sector re-run read from it.
    ctx = AssessmentContext(
        resources=resources,
        selection=selection,
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

    checks = run_checks(ctx)

    auditability = governance.evaluate(resources, provenance)

    # Determinism split (Phase 1.5): the verdict is computed ONLY from the
    # deterministic check set. Statistical/batch-relative checks (outlier, drift)
    # are non-deterministic, so they are reported as an advisory and never move
    # the BLOCK/PASS/CONDITIONAL decision, the category scores, or the scorecard.
    det_checks = [c for c in checks if not c.advisory]
    adv_checks = [c for c in checks if c.advisory]

    # RBQM critical-to-quality (Phase 6.2): elevate the use case's CtQ checks so a
    # failure there blocks (ICH E6(R3): check critical data deeply).
    elevate_critical(det_checks, critical_check_ids)

    # Category pass-rates + overall (Kahn roll-up over deterministic checks).
    category_scores, overall, has_assessed = category_and_overall(det_checks)

    # Per-resource-type threshold check (Phase 2A — ONC ASTP calibration).
    rt_below: list[str] = []
    if resource_thresholds:
        rt_below = check_resource_type_thresholds(
            resources, det_checks, resource_thresholds
        )

    # Decision policy (deterministic checks only).
    blocker_lines = blockers(det_checks) + regulated_blockers(det_checks)
    # Provenance only caps the verdict when its phase is actually being audited.
    provenance_capped = PROVENANCE in selection and not auditability.get(
        "provenance_present", False
    )
    decision, allowed = decide(
        category_scores, overall, blocker_lines, rt_below, provenance_capped
    )

    phase_rep = phase_report(det_checks, selection)

    # Per-sector audit: re-run the selected phases scoped to each target so each
    # sector gets its own independent verdict. The baseline reservoir was already
    # updated over the full batch above, so target runs are read-only against it
    # (baseline_store=None) to avoid double-counting.
    targets_rep = targets_report(
        ctx, targets or [], provenance_capped=provenance_capped
    )

    # Assessment-coverage transparency (deterministic): how much of the suite
    # actually ran. Computed BEFORE fitness so the use-bound approvals can require
    # external validation.
    coverage = compute_coverage(det_checks, ctx)

    approved_for, not_approved_for = compute_fitness(
        decision,
        auditability,
        category_scores=category_scores,
        blockers=blocker_lines,
        intended_use=intended_use,
        external_validation_performed=coverage.get(
            "external_validation_performed", False
        ),
    )

    # Per-dimension scorecard + overall grade + purpose-bound fitness statement
    # (deterministic checks only — reproducible).
    scorecard_data = scorecard(det_checks)
    overall_grade = grade_with_floor(overall, det_checks) if has_assessed else None
    fitness = fitness_statement(
        decision, overall, overall_grade, intended_use, coverage
    )

    # Statistical advisory + evaluation-environment provenance (Phase 1.5).
    advisory = advisory_report(adv_checks)
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
        "suggested_thresholds": suggest_thresholds(det_checks),
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
        blockers=blocker_lines,
        approved_for=approved_for,
        not_approved_for=not_approved_for,
        provenance=provenance,
        generated_at=QualityPassport.now_iso(),
        privacy_processing_allowed=allowed,
        resource_count=sum(1 for r in resources if isinstance(r, dict)),
        config_profile=config_profile,
        phases=phase_rep,
        targets=targets_rep,
        scorecard=scorecard_data,
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
    elevate_critical(det_checks, critical_check_ids)

    category_scores, overall, has_assessed = category_and_overall(det_checks)
    blocker_lines = blockers(det_checks) + regulated_blockers(det_checks)
    decision, allowed = decide(category_scores, overall, blocker_lines, [], False)

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
    scorecard_data = scorecard(det_checks)
    overall_grade = grade_with_floor(overall, det_checks) if has_assessed else None
    fitness = fitness_statement(
        decision, overall, overall_grade, intended_use, coverage
    )
    approved_for, not_approved_for = compute_fitness(
        decision,
        {"provenance_present": bool(provenance)},
        category_scores=category_scores,
        blockers=blocker_lines,
        intended_use=intended_use,
        # The OHDSI DQD suite is the model-appropriate validation for OMOP.
        external_validation_performed=True,
    )
    advisory = advisory_report(adv_checks)
    evaluation = {
        "decision_basis": "deterministic",
        "sampler_seed": SAMPLER_SEED,
        "advisory_check_ids": sorted({c.check_id for c in adv_checks}),
        "validator_used": False,
        "terminology_used": False,
        "source_model": "omop",
        "lifecycle_stage": lifecycle_stage,
        "org_role": org_role,
        "suggested_thresholds": suggest_thresholds(det_checks),
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
        blockers=blocker_lines,
        approved_for=approved_for,
        not_approved_for=not_approved_for,
        provenance=provenance,
        generated_at=QualityPassport.now_iso(),
        privacy_processing_allowed=allowed,
        resource_count=len(omop.rows("person")),
        config_profile=config_profile,
        phases=phase_report(det_checks, set(ALL_PHASES)),
        targets={},
        scorecard=scorecard_data,
        overall_grade=overall_grade,
        fitness=fitness,
        advisory=advisory,
        evaluation=evaluation,
        coverage=coverage,
    )
