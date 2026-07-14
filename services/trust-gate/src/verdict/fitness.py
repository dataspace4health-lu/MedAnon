"""Fitness-for-use: quality is fitness for a *declared* use (Juran; Wang & Strong
1996; Kahn 2012  a dataset fit for cohort discovery may be unfit for outcomes
research). ``compute_fitness`` produces the approved / not-approved lists gated on
the declared use's requirement profile; ``fitness_statement`` produces the graded,
purpose-bound prose verdict with a coverage caveat.
"""

from __future__ import annotations

from constants import CATEGORY_MIN_RATE
from passport import BLOCK, CONDITIONAL_PASS

from verdict.coverage import coverage_caveat

# Use-case → quality requirements. Each use names the Kahn category floors it needs
# (at CATEGORY_MIN_RATE  not a separate magic number) plus whether it requires
# dataset provenance and external validation. The caller's free-text ``intended_use``
# is keyword-matched to a profile. Ordered loosest → strictest; ``general`` is the
# fallback.
_USE_PROFILES: tuple[tuple[str, dict], ...] = (
    (
        "cohort discovery / feasibility",
        {
            "categories": ("conformance",),
            "require_provenance": False,
            "require_external_validation": False,
            "keywords": ("cohort", "feasibil", "discovery", "recruit", "count"),
        },
    ),
    (
        "descriptive analytics",
        {
            "categories": ("conformance", "completeness"),
            "require_provenance": False,
            "require_external_validation": False,
            "keywords": (
                "descriptive",
                "analytic",
                "statistic",
                "dashboard",
                "reporting",
            ),
        },
    ),
    (
        "AI/ML model training",
        {
            "categories": ("conformance", "completeness", "plausibility"),
            "require_provenance": True,
            "require_external_validation": False,
            "keywords": ("ai", "ml", "model", "training", "machine learning"),
        },
    ),
    (
        "outcomes / comparative-effectiveness research",
        {
            "categories": ("conformance", "completeness", "plausibility"),
            "require_provenance": True,
            "require_external_validation": True,
            "keywords": (
                "outcome",
                "effectiveness",
                "comparative",
                "inference",
                "causal",
            ),
        },
    ),
    (
        "regulated submission / external sharing",
        {
            "categories": ("conformance", "completeness", "plausibility"),
            "require_provenance": True,
            "require_external_validation": True,
            "keywords": ("regulat", "submission", "fda", "ema", "ehds", "sharing"),
        },
    ),
)
_GENERAL_USE = (
    "secondary use (general)",
    {
        "categories": ("conformance", "completeness"),
        "require_provenance": False,
        "require_external_validation": False,
        "keywords": (),
    },
)


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


def compute_fitness(
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
        declared_prof,
        category_scores,
        provenance_present,
        external_validation_performed,
    )
    if declared_unmet:
        not_ok.append(f"{declared_label}  requires: {'; '.join(declared_unmet)}")
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
            not_ok.append(f"{label}  requires: {'; '.join(reasons)}")

    # De-duplicate while preserving order.
    approved = list(dict.fromkeys(approved))
    not_ok = list(dict.fromkeys(not_ok))
    return (approved, not_ok)


def fitness_statement(
    decision: str,
    overall: float,
    overall_grade: str | None,
    intended_use: str | None,
    coverage: dict | None = None,
) -> dict:
    """Purpose-bound fitness verdict  quality is fitness for a *declared* use
    (Juran; Wang & Strong 1996), so the statement always names the use. A coverage
    caveat is appended so the verdict cannot over-claim relative to how much of the
    suite actually ran."""
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
            f"Conditionally fit for {use}: {grade_str} ({pct_str})  "
            f"remediate the flagged dimensions before high-stakes use."
        )
    else:
        fit = True
        statement = f"Fit for {use}: {grade_str} ({pct_str})."
    # Reuse the caveat precomputed in coverage (single source of truth).
    caveat = (coverage or {}).get("caveat") or coverage_caveat(coverage)
    if caveat:
        statement = f"{statement} {caveat}"
    return {
        "intended_use": use,
        "overall_grade": overall_grade,
        "fit": fit,
        "statement": statement,
        "coverage_caveat": caveat,
    }
