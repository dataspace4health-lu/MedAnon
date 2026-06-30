"""Quality Passport data models — grounded in the Kahn et al. (2016) framework.

Reference: Kahn MG, Callahan TJ, Barnard J, et al. "A Harmonized Data Quality
Assessment Terminology and Framework for the Secondary Use of Electronic Health
Record Data." eGEMs 2016;4(1):18. doi:10.13063/2327-9214.1244 — the field
standard used by OHDSI (Data Quality Dashboard) and PCORnet.

Measurement layer (Kahn):
  - Three categories: ``conformance``, ``completeness``, ``plausibility``.
  - Two contexts: ``verification`` (internal constraints, no external reference)
    and ``validation`` (against an external benchmark — e.g. a profile/IG or a
    terminology server).
  - Each check is scored OHDSI-DQD style: a *violation fraction*
    (violations / applicable rows) compared to a per-check *threshold*. A check
    FAILs when the fraction exceeds its threshold, PASSes otherwise, and is NA
    when no rows were applicable. The headline metric is **% of checks passing**
    per category — NOT a weighted average of arbitrary domain weights.

Policy layer (product, NOT Kahn):
  - ``decision`` (PASS / CONDITIONAL_PASS / BLOCK) and the auditability evidence
    are an explicit fitness-for-use policy on top of the measurement. This split
    is deliberate so the scoring stays defensible and the policy stays tunable.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
from dataclasses import dataclass, field

# Decision constants (policy layer).
PASS = "PASS"
CONDITIONAL_PASS = "CONDITIONAL_PASS"
BLOCK = "BLOCK"

# Per-check outcome constants.
RESULT_PASS = "PASS"
RESULT_FAIL = "FAIL"
RESULT_NA = "NA"  # not applicable — no rows to assess (Kahn "could not assess")

# Kahn categories + contexts.
CATEGORIES = ("conformance", "completeness", "plausibility")
CONTEXTS = ("verification", "validation")

# Honesty caveat surfaced whenever plausibility/clinical checks ran: statistical
# distribution-conformance is not clinical-coherence certification (the generative
# semantic validator that would close that gap is deferred).
PLAUSIBILITY_LIMITATION_NOTE = (
    "Plausibility is assessed by distribution-driven, demographically stratified "
    "checks: a value flagged here is statistically implausible for its cohort. A "
    "PASS means no such statistical violation was found; it does NOT certify "
    "clinical coherence (a value within every stratified distribution can still be "
    "clinically contradictory in context). Deterministic cross-field clinical-logic "
    "rules cover part of this; the generative/semantic epidemiology validator that "
    "completes it is deferred."
)

FRAMEWORK = (
    "Kahn et al. 2016 harmonized DQ framework "
    "(conformance/completeness/plausibility × verification/validation); "
    "OHDSI DQD violation-rate-vs-threshold scoring"
)

# PIQI HDQT v2.0 (ASTP/ONC 2024) taxonomy — 4 categories × 3 dimensions.
# Each CheckResult carries these as metadata for downstream analytics.
HDQT_CATEGORIES = ("availability", "accuracy", "conformity", "plausibility")
HDQT_DIMENSIONS = (
    "missing", "unpopulated", "incomplete",           # availability
    "invalid_format", "invalid_value", "invalid_grouping",  # accuracy
    "invalid_member", "incompatible", "obsolete",      # conformity
    "clinically_implausible", "temporally_implausible", "situationally_implausible",  # plausibility
)

# Semantic version tracking for downstream consumers of the passport.
FRAMEWORK_VERSIONS = {"kahn": "2016", "hdqt": "2.0", "evaluation_profile": "fhir_r4_v1"}

# The Trust Gate assesses data BEFORE de-identification, and the passport is
# persisted (medanon.processing_runs.trust_passport) and served over the API.
# Any PHI-bearing value (a Patient.identifier value such as an MRN/SSN) that a
# check wants to surface in the audit must be reduced to a stable, non-reversible
# token first — never the raw value. The optional salt frustrates dictionary
# attacks on small-domain identifiers; tokens stay stable within a deployment so
# auditors can still correlate repeats.
_AUDIT_SALT = os.environ.get("TRUST_GATE_AUDIT_SALT", "")


def redact_token(value: str) -> str:
    """Stable, non-reversible short token for a PHI-bearing value in audit output."""
    if not value:
        return ""
    digest = hashlib.sha256((_AUDIT_SALT + value).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


@dataclass
class CheckResult:
    """One data-quality check, scored as a violation fraction vs a threshold.

    HDQT fields (``hdqt_category`` / ``hdqt_dimension``) map this check to the
    PIQI Healthcare Data Quality Taxonomy v2.0 (ASTP/ONC 2024) — additive
    metadata that does not affect Kahn scoring or the PASS/FAIL decision.

    ``skipped`` is True when the check could not run (prerequisite unavailable,
    external service timeout, etc.). Skipped checks are NA (applicable=0) and
    are collected in the passport's ``skipped_checks`` summary.

    ``violation_details`` carries per-resource violation context for checks that
    populate it (e.g. critical block checks). Empty for aggregate-only checks.
    """

    check_id: str
    category: str  # conformance | completeness | plausibility (Kahn)
    subcategory: str  # value | relational | uniqueness | atemporal | temporal | completeness
    context: str  # verification | validation
    applicable: int = 0  # rows/items the check applied to
    violations: int = 0  # rows/items violating the check
    threshold: float = 0.0  # max acceptable violation fraction (DQD failClass)
    critical: bool = False  # a FAIL here forces a BLOCK
    description: str = ""
    recommendation: str = ""
    # PIQI HDQT v2.0 taxonomy (additive; does not affect Kahn scoring).
    hdqt_category: str = ""   # availability | accuracy | conformity | plausibility
    hdqt_dimension: str = ""  # e.g. missing | invalid_format | temporally_implausible
    # Resource-type attribution for Phase 2A per-type calibration. "" means the
    # check is batch-wide / multi-type (it applies to every type in the batch);
    # a concrete type (e.g. "Patient", "Observation") means it is type-scoped and
    # its pass/fail only attributes to that type's per-resource-type pass-rate.
    resource_type: str = ""
    # Honesty mechanism (Verily DQ patterns): why this check was not run.
    skipped: bool = False
    skip_reason: str = ""
    # Audit phase (selectable suite) this check belongs to, and free-form tags
    # for tag-based selection. Assigned by the engine from phases.py.
    phase: str = ""
    tags: list[str] = field(default_factory=list)
    # DQ dimension (DAMA/ISO 25012) for the scorecard. Assigned from dimensions.py.
    dimension: str = ""
    # Statistical / batch-relative check (outlier, drift): non-deterministic, so
    # it is reported but never drives the verdict (Phase 1.5 determinism). The
    # engine authoritatively (re)sets this from the determinism registry; the
    # default is True so a check that is never classified fails *safe* (advisory,
    # cannot move the gate) rather than silently becoming a verdict driver.
    advisory: bool = True
    # Per-resource violation context (populated only by checks that track it).
    violation_details: list[dict] = field(default_factory=list)

    # Cap per-check violation detail so a large dataset cannot balloon the
    # passport. The full count still lives in ``violations``; details are a
    # bounded, representative sample for the audit report.
    MAX_DETAILS = 50

    def add_detail(self, **fields) -> None:
        """Record a PHI-free, capped per-resource finding for the audit report.

        Conventionally carries ``resource_type`` / ``resource_id`` / ``path`` /
        ``detail``; never raw free-text or coded values. No-ops once the cap is
        hit (``violations`` keeps the true total).
        """
        if len(self.violation_details) < CheckResult.MAX_DETAILS:
            self.violation_details.append({"check_id": self.check_id, **fields})

    @property
    def violation_fraction(self) -> float:
        if self.applicable <= 0:
            return 0.0
        return self.violations / self.applicable

    @property
    def result(self) -> str:
        if self.applicable <= 0:
            return RESULT_NA
        return RESULT_FAIL if self.violation_fraction > self.threshold else RESULT_PASS

    def to_dict(self) -> dict:
        d = {
            "check_id": self.check_id,
            "category": self.category,
            "subcategory": self.subcategory,
            "context": self.context,
            "result": self.result,
            "applicable": self.applicable,
            "violations": self.violations,
            "violation_fraction": round(self.violation_fraction, 4),
            "threshold": self.threshold,
            "critical": self.critical,
            "description": self.description,
            "recommendation": self.recommendation,
        }
        if self.phase:
            d["phase"] = self.phase
        if self.tags:
            d["tags"] = self.tags
        if self.dimension:
            d["dimension"] = self.dimension
        if self.advisory:
            d["advisory"] = True
        if self.hdqt_category:
            d["hdqt_category"] = self.hdqt_category
            d["hdqt_dimension"] = self.hdqt_dimension
        if self.skipped:
            d["skipped"] = True
            d["skip_reason"] = self.skip_reason
        if self.violation_details:
            d["violation_details"] = self.violation_details
        return d


@dataclass(slots=True)
class CategoryResult:
    """Per-Kahn-category roll-up: % of applicable checks that passed."""

    name: str
    checks_assessed: int
    checks_passed: int

    @property
    def pass_rate(self) -> float | None:
        if self.checks_assessed <= 0:
            return None
        return 100.0 * self.checks_passed / self.checks_assessed

    def to_dict(self) -> dict:
        pr = self.pass_rate
        return {
            "name": self.name,
            "checks_assessed": self.checks_assessed,
            "checks_passed": self.checks_passed,
            "pass_rate": round(pr, 1) if pr is not None else None,
        }


@dataclass(slots=True)
class QualityPassport:
    """The Trust Gate verdict for a dataset/batch."""

    dataset_id: str
    source_types: list[str]
    decision: str  # PASS | CONDITIONAL_PASS | BLOCK (policy layer)
    overall_score: float  # % of all applicable checks passing (100.0 when none assessed)
    category_scores: dict[str, float | None]  # Kahn category → pass-rate
    checks: list[CheckResult]
    auditability: dict  # provenance/governance evidence (policy layer, not Kahn)
    blockers: list[str]
    approved_for: list[str]
    not_approved_for: list[str]
    provenance: dict
    generated_at: str
    privacy_processing_allowed: bool
    # True when at least one check was assessed (applicable > 0). False means no
    # check ran at all — overall_score of 100.0 in that case is vacuously true and
    # should be displayed as "not assessed" rather than a real 100%.
    has_assessed: bool = True
    resource_count: int = 0
    config_profile: str = "auto"
    framework: str = FRAMEWORK
    # Descriptive profiling (analysis support, NOT scored) — see profiling.py.
    profile: dict = None  # type: ignore[assignment]
    # PIQI HDQT v2.0 / Verily honesty mechanism: checks that were not run.
    # Keys are reason categories; each value is {skipped: int, reason: str}.
    skipped_checks: dict = None  # type: ignore[assignment]
    # Semantic framework version tracking for downstream consumers.
    framework_versions: dict = None  # type: ignore[assignment]
    # Per-phase verdicts (selectable-suite roll-up): phase_id → {decision,
    # score, checks_assessed, checks_passed, blockers}. Empty when not phased.
    phases: dict = None  # type: ignore[assignment]
    # Per-sector verdicts (target roll-up): target_id → {decision, score,
    # resource_count, phases, blockers}. Empty when no targets were requested.
    targets: dict = None  # type: ignore[assignment]
    # Per-dimension scorecard (DAMA/ISO 25012): dimension → {score, grade,
    # checks_assessed, checks_passed}. Overall letter grade + purpose-bound
    # fitness statement.
    scorecard: dict = None  # type: ignore[assignment]
    overall_grade: str | None = None
    fitness: dict = None  # type: ignore[assignment]
    # Statistical advisory (non-deterministic outlier/drift): reported but not
    # part of the verdict (Phase 1.5 determinism).
    advisory: dict = None  # type: ignore[assignment]
    # Evaluation-environment provenance for reproducibility: sampler seed, which
    # external services were used, and the decision basis.
    evaluation: dict = None  # type: ignore[assignment]
    # Assessment-coverage transparency: assessed/total checks, the deterministic
    # depth capabilities not exercised, and the validation depth. Stops the
    # headline grade/fitness from over-claiming relative to what actually ran.
    coverage: dict = None  # type: ignore[assignment]

    def _limitations(self) -> list[str]:
        """Honesty caveats about what a PASS does and does not certify.

        The plausibility layer is distribution-driven and demographically
        stratified, which catches values that are statistically extreme for their
        cohort but cannot, on its own, certify *clinical coherence*: a value that
        sits inside every stratified distribution can still be clinically
        contradictory in context (e.g. an advanced-CKD patient with a normal-range
        creatinine). Deterministic cross-field clinical-logic rules cover part of
        this; the generative/semantic epidemiology validator that completes it is
        deferred (see methodology doc). Disclose this so a PASS on plausibility is
        not read as a clinical-coherence certification.
        """
        notes: list[str] = []
        if any(
            c.category in ("plausibility", "clinical") and c.result != RESULT_NA
            for c in self.checks
        ):
            notes.append(PLAUSIBILITY_LIMITATION_NOTE)
        return notes

    def to_dict(self) -> dict:
        # Aggregate violation_details from all checks into a flat violations list.
        violations = [
            detail
            for c in self.checks
            for detail in c.violation_details
        ]
        # Aggregate skipped checks by reason category.
        skipped: dict[str, dict] = {}
        for c in self.checks:
            if c.skipped and c.skip_reason:
                entry = skipped.setdefault(c.check_id, {"skipped": 0, "reason": c.skip_reason})
                entry["skipped"] += 1
        if self.skipped_checks:
            for k, v in self.skipped_checks.items():
                skipped.setdefault(k, v)
        return {
            "dataset_id": self.dataset_id,
            "source_types": self.source_types,
            "decision": self.decision,
            "overall_score": round(self.overall_score, 1) if self.has_assessed else None,
            "has_assessed": self.has_assessed,
            "category_scores": {
                k: (round(v, 1) if v is not None else None)
                for k, v in self.category_scores.items()
            },
            "phases": self.phases or {},
            "targets": self.targets or {},
            "scorecard": self.scorecard or {},
            "overall_grade": self.overall_grade,
            "fitness": self.fitness or {},
            "advisory": self.advisory or {},
            "evaluation": self.evaluation or {},
            "coverage": self.coverage or {},
            "checks": [c.to_dict() for c in self.checks],
            "violations": violations,
            "skipped_checks": skipped,
            "framework_versions": self.framework_versions or FRAMEWORK_VERSIONS,
            "auditability": self.auditability,
            "blockers": self.blockers,
            "approved_for": self.approved_for,
            "not_approved_for": self.not_approved_for,
            "provenance": self.provenance,
            "generated_at": self.generated_at,
            "privacy_processing_allowed": self.privacy_processing_allowed,
            "resource_count": self.resource_count,
            "config_profile": self.config_profile,
            "framework": self.framework,
            "profile": self.profile or {},
            "limitations": self._limitations(),
            "report": self.to_markdown(),
        }

    def to_markdown(self) -> str:
        lines: list[str] = [
            f"# Quality Passport — {self.dataset_id}",
            "",
            f"- **Decision:** {self.decision}",
            "- **Overall score:** "
            + (f"{round(self.overall_score, 1)}% checks passing" if self.has_assessed else "not assessed")
            + (f" (grade {self.overall_grade})" if self.overall_grade else ""),
            *(
                [f"- **Fitness:** {self.fitness.get('statement', '')}"]
                if self.fitness and self.fitness.get("statement")
                else []
            ),
            *(
                [
                    "- **Assessment coverage:** "
                    f"{self.coverage.get('checks_assessed')}/"
                    f"{self.coverage.get('checks_total')} checks assessed"
                    f" ({self.coverage.get('validation_depth')})"
                    + (
                        "; not exercised: "
                        + ", ".join(self.coverage.get("not_exercised", []))
                        if self.coverage.get("not_exercised")
                        else ""
                    )
                ]
                if self.coverage and self.coverage.get("checks_total")
                else []
            ),
            f"- **Privacy processing allowed:** "
            f"{'yes' if self.privacy_processing_allowed else 'no'}",
            f"- **Resources assessed:** {self.resource_count}",
            f"- **Source types:** {', '.join(self.source_types) or 'unknown'}",
            f"- **Framework:** {self.framework}",
            f"- **Generated at:** {self.generated_at}",
            *(
                [
                    "- **Reporting context:** "
                    f"{self.evaluation.get('lifecycle_stage', 'operation')} stage, "
                    f"{self.evaluation.get('org_role', 'data-receiving')} org"
                ]
                if self.evaluation
                else []
            ),
            "",
            "## Data-quality categories (Kahn 2016)",
            "",
            "| Category | Checks passed | Pass rate |",
            "|---|---|---|",
        ]
        for name in CATEGORIES:
            score = self.category_scores.get(name)
            shown = f"{round(score, 1)}%" if score is not None else "not assessed"
            assessed = sum(
                1 for c in self.checks if c.category == name and c.result != RESULT_NA
            )
            passed = sum(
                1 for c in self.checks if c.category == name and c.result == RESULT_PASS
            )
            lines.append(f"| {name} | {passed}/{assessed} | {shown} |")
        lines.append("")

        if self.scorecard:
            lines.append("## Dimension scorecard (DAMA / ISO 25012)")
            lines.append("")
            lines.append("| Dimension | Grade | Checks passed | Score |")
            lines.append("|---|---|---|---|")
            for dim, rep in self.scorecard.items():
                score = rep.get("score")
                shown = f"{score}%" if score is not None else "not assessed"
                lines.append(
                    f"| {dim} | {rep.get('grade') or '-'} | "
                    f"{rep.get('checks_passed', 0)}/{rep.get('checks_assessed', 0)} | {shown} |"
                )
            lines.append("")

        if self.phases:
            lines.append("## Audit phases (selected)")
            lines.append("")
            lines.append("| Phase | Decision | Checks passed | Score |")
            lines.append("|---|---|---|---|")
            for ph, rep in self.phases.items():
                score = rep.get("score")
                shown = f"{score}%" if score is not None else "not assessed"
                lines.append(
                    f"| {ph} | {rep.get('decision', '')} | "
                    f"{rep.get('checks_passed', 0)}/{rep.get('checks_assessed', 0)} | {shown} |"
                )
            lines.append("")

        if self.targets:
            lines.append("## Sectors (per-target verdicts)")
            lines.append("")
            lines.append("| Sector | Decision | Resources | Checks passed | Score |")
            lines.append("|---|---|---|---|---|")
            for tid, rep in self.targets.items():
                score = rep.get("score")
                shown = f"{score}%" if score is not None else "not assessed"
                lines.append(
                    f"| {tid} | {rep.get('decision', '')} | "
                    f"{rep.get('resource_count', 0)} | "
                    f"{rep.get('checks_passed', 0)}/{rep.get('checks_assessed', 0)} | {shown} |"
                )
            lines.append("")

        if self.blockers:
            lines.append("## Blockers")
            lines.append("")
            lines.extend(f"- {b}" for b in self.blockers)
            lines.append("")

        failed = [c for c in self.checks if c.result == RESULT_FAIL]
        if failed:
            lines.append("## Failed checks")
            lines.append("")
            lines.append(
                "| Check | Category | Subcat | Context | Violations | Threshold | Recommendation |"
            )
            lines.append("|---|---|---|---|---|---|---|")
            for c in failed:
                lines.append(
                    f"| {c.check_id} | {c.category} | {c.subcategory} | {c.context} | "
                    f"{c.violations}/{c.applicable} ({round(c.violation_fraction * 100, 1)}%) | "
                    f"{c.threshold} | {c.recommendation} |"
                )
            lines.append("")

        aud = self.auditability
        if aud:
            lines.append("## Auditability (fitness-for-use policy)")
            lines.append("")
            for k, v in aud.items():
                lines.append(f"- {k}: {v}")
            lines.append("")

        if self.approved_for or self.not_approved_for:
            lines.append("## Fitness for use")
            lines.append("")
            lines.extend(f"- approved: {u}" for u in self.approved_for)
            lines.extend(f"- not approved: {u}" for u in self.not_approved_for)
            lines.append("")

        prof = self.profile or {}
        if prof:
            lines.append("## Data profile (descriptive — not scored)")
            lines.append("")
            counts = prof.get("resource_counts") or {}
            if counts:
                lines.append(
                    "- Resource types: "
                    + ", ".join(f"{k}×{v}" for k, v in counts.items())
                )
            systems = prof.get("code_system_distribution") or {}
            if systems:
                lines.append(
                    "- Code systems: "
                    + ", ".join(f"{k} ({v})" for k, v in systems.items())
                )
            if prof.get("patient_gender_distribution"):
                lines.append(f"- Gender: {prof['patient_gender_distribution']}")
            lines.append(f"- Reference density: {prof.get('reference_density', 0)}")
            for s in prof.get("observation_value_stats") or []:
                lines.append(
                    f"- Obs {s['code']} ({s['unit']}): n={s['count']}, "
                    f"min={s['min']}, max={s['max']}, mean={s['mean']}, sd={s['stddev']}"
                )
            lines.append("")

        limitations = self._limitations()
        if limitations:
            lines.append("## Limitations")
            lines.append("")
            lines.extend(f"- {note}" for note in limitations)
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def now_iso() -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
