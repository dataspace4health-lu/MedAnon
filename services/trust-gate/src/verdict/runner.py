"""Runner: check orchestration and the per-sector / per-phase sub-reports.

``run_checks`` produces the full check list for a resource set, phase/dimension-tags
each result, marks advisory (non-deterministic) checks, and keeps only the selected
phases. ``targets_report`` / ``phase_report`` re-run and roll up scoped subsets,
routing every sub-verdict through the shared ``decision.subset_decision`` so a
sector/phase badge is never more lenient than the headline.
"""

from __future__ import annotations

from constants import is_deterministic
from dimensions import dimension_for_builtin, dimension_for_kind
from passport import CheckResult, RESULT_FAIL, RESULT_NA, RESULT_PASS
from phases import phase_for_builtin, phase_for_kind

from verdict.context import AssessmentContext
from verdict.decision import subset_decision
from verdict.registry import CHECK_REGISTRY


def run_checks(ctx: AssessmentContext) -> list[CheckResult]:
    """Produce the checks for ``ctx.resources``, phase/dimension-tag them, mark the
    advisory (non-deterministic) ones, and keep only the selected phases.

    Iterates the check registry (Strategy) rather than hardcoding each call, so the
    suite is defined in one place (``verdict.registry``). Shared by the whole-batch
    pass and each per-sector target pass (``ctx.scoped_to(subset)``).

    ``ctx.reference_time`` is the single stable "now" anchor for time-relative
    plausibility rules (``not_in_future``) — held constant across the whole
    assessment so the verdict has no per-resource clock drift (see ``rules.evaluate``).
    """
    checks: list[CheckResult] = []
    for check in CHECK_REGISTRY:
        checks.extend(check(ctx))

    rules = ctx.plausibility_rules or []
    rule_phase = {r.rule_id: phase_for_kind(r.kind) for r in rules}
    rule_dim = {r.rule_id: dimension_for_kind(r.kind) for r in rules}
    rule_ids = frozenset(rule_phase)
    for c in checks:
        c.phase = phase_for_builtin(c.check_id) or rule_phase.get(c.check_id, "")
        c.dimension = dimension_for_builtin(c.check_id) or rule_dim.get(c.check_id, "")
        c.advisory = not is_deterministic(c.check_id, rule_ids)
    return [c for c in checks if c.phase in ctx.selection]


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


def targets_report(
    ctx: AssessmentContext,
    targets: list[dict],
    *,
    provenance_capped: bool = False,
) -> dict[str, dict]:
    """Per-sector verdicts: scope the resource set to each target, re-run the
    selected phases, and roll up an independent decision *under the same policy as
    the headline* (see ``subset_decision``)."""
    report: dict[str, dict] = {}
    for tgt in targets:
        if not isinstance(tgt, dict):
            continue
        tid = tgt.get("id") or _target_label(tgt)
        subset = [r for r in ctx.resources if _matches_target(r, tgt)]
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
        tchecks = run_checks(ctx.scoped_to(subset))
        det_tchecks = [c for c in tchecks if not c.advisory]
        assessed = [c for c in det_tchecks if c.result != RESULT_NA]
        passed = sum(1 for c in assessed if c.result == RESULT_PASS)
        # A sector is a slice of the same dataset, so a missing-provenance cap on
        # the headline applies to it too — pass it through so a sector can never
        # badge PASS while the headline is CONDITIONAL.
        decision, score, has_assessed = subset_decision(
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
            "phases": phase_report(det_tchecks, ctx.selection),
            "blockers": block_ids,
        }
    return report


def phase_report(checks: list[CheckResult], selection: set[str]) -> dict[str, dict]:
    """Per-phase verdict roll-up (selectable-suite reporting).

    Each phase's decision is computed by the SAME shared policy as the headline
    (``subset_decision`` → ``decide``), so a phase honours the ``CATEGORY_MIN_RATE``
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
        ph_decision, score, has_assessed = subset_decision(ph_checks)
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
