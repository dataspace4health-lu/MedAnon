"""Scoring: turn deterministic check results into category/overall pass-rates, the
per-dimension scorecard, letter grades, threshold suggestions, and the statistical
advisory block. Pure functions of the check list — no I/O, no policy decisions.
"""

from __future__ import annotations

import math

from dimensions import ALL_DIMENSIONS, grade_for
from passport import CATEGORIES, CheckResult, RESULT_FAIL, RESULT_NA, RESULT_PASS


def category_and_overall(
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


def grade_with_floor(score: float | None, checks: list[CheckResult]) -> str | None:
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


def scorecard(checks: list[CheckResult]) -> dict[str, dict]:
    """Per-DQ-dimension roll-up (DAMA/ISO 25012): score + letter grade.

    A dimension appears only when at least one of its checks ran. The score is the
    % of *assessed* (non-NA) checks passing for that dimension; the grade follows
    ``grade_with_floor`` so a critical failure in the dimension floors it to F
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
            "grade": grade_with_floor(score, dim_checks),
            "checks_assessed": len(assessed),
            "checks_passed": passed,
        }
    return report


def suggest_thresholds(det_checks: list[CheckResult]) -> dict[str, float]:
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


def advisory_report(adv_checks: list[CheckResult]) -> dict:
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
