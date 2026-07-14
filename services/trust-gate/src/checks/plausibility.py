"""Plausibility category (Kahn 2016): are data values believable?

- uniqueness (verification)  no duplicate ResourceType/id in the batch
- atemporal  (verification)  definitional unit bounds (e.g. % ∈ [0,100]):
                              catches whole-batch unit corruption
- atemporal  (verification)  data-driven value-outlier detection (robust
                              modified z-score / IQR over the per-(code,unit)
                              distribution, optionally accumulated across batches)
- atemporal  (verification)  concordance: cross-field contradictions
- temporal / atemporal (verification)  the declarative clinical rule pack
"""

from __future__ import annotations

from collections import Counter, defaultdict
from math import isinf, log
from statistics import median, quantiles

from constants import (
    DEFINITIONAL_UNIT_BOUNDS,
    DRIFT_MODZ_CUTOFF,
    DRIFT_ZERO_MAD_REL_TOL,
    OUTLIER_IQR_K,
    OUTLIER_MIN_SAMPLE,
    OUTLIER_MODZ_CUTOFF,
    threshold_for,
)
from passport import CheckResult
from rules import PlausibilityRule
from rules import evaluate as run_rules


def evaluate(
    resources: list[dict],
    rules: list[PlausibilityRule],
    thresholds: dict[str, float] | None = None,
    *,
    baseline_store=None,
    definitional_bounds: dict | None = None,
    concordance_rules: list[dict] | None = None,
    reference_time: str | None = None,
) -> list[CheckResult]:
    out: list[CheckResult] = [
        _uniqueness(resources, thresholds),
        _definitional_bounds(resources, thresholds, definitional_bounds),
        # Drift reads the accumulated baseline; it MUST run before _value_outliers,
        # which updates the baseline with this batch's values.
        _distribution_drift(resources, thresholds, baseline_store),
        _value_outliers(resources, thresholds, baseline_store),
        _concordance(resources, thresholds, concordance_rules or []),
    ]
    out.extend(run_rules(resources, rules, reference_time))
    return out


def _obs_code(res: dict) -> str | None:
    code = res.get("code")
    if not isinstance(code, dict):
        return None
    for c in code.get("coding", []):
        if isinstance(c, dict) and c.get("code"):
            return str(c["code"])
    return None


def _numeric_groups(resources) -> dict[tuple[str, str], list[tuple[float, str]]]:
    """(code, unit) → [(value, Observation.id), …] for the batch.

    Carries the source id alongside each value so the value-plausibility checks
    can attach a specific finding (which Observation, which code) to the audit.
    """
    groups: dict[tuple[str, str], list[tuple[float, str]]] = defaultdict(list)
    for res in resources:
        if not isinstance(res, dict) or res.get("resourceType") != "Observation":
            continue
        vq = res.get("valueQuantity")
        code = _obs_code(res)
        if isinstance(vq, dict) and isinstance(vq.get("value"), (int, float)) and code:
            unit = str(vq.get("code") or vq.get("unit") or "")
            groups[(code, unit)].append((float(vq["value"]), str(res.get("id", ""))))
    return groups


def _definitional_bounds(resources, thresholds, bounds: dict | None):
    """Values outside their unit's mathematical definition (e.g. a percentage
    >100 or <0). Unlike clinical ranges these are universally true, so they catch
    whole-batch unit corruption that the distribution-relative check cannot."""
    table = {**DEFINITIONAL_UNIT_BOUNDS, **(bounds or {})}
    chk = CheckResult(
        check_id="plausibility.definitional_bounds",
        category="plausibility",
        subcategory="atemporal",
        context="verification",
        threshold=threshold_for("plausibility.definitional_bounds", thresholds),
        description="Numeric values are within their unit's definitional bounds (e.g. % ∈ [0,100]).",
        recommendation="A value outside its unit's definition indicates a unit or data error.",
        hdqt_category="plausibility",
        hdqt_dimension="clinically_implausible",
    )
    for (code, unit), pairs in _numeric_groups(resources).items():
        bound = table.get(unit)
        if not bound:
            continue
        lo, hi = float(bound[0]), float(bound[1])
        # A one-sided (0, inf) bound is a non-negativity rule; phrase it as such
        # so the remediation hint is clear (vs a two-sided bound like % in [0,100]).
        reason = (
            f"value below the non-negative definitional bound (>= {lo}) for unit '{unit}'"
            if isinf(hi)
            else f"value outside definitional bounds [{lo}, {hi}] for unit '{unit}'"
        )
        chk.applicable += len(pairs)
        for value, rid in pairs:
            if value < lo or value > hi:
                chk.violations += 1
                chk.add_detail(
                    resource_type="Observation",
                    resource_id=rid,
                    path=f"valueQuantity ({code} {unit})",
                    value=f"{value} {unit}".strip(),
                    detail=reason,
                )
    return chk


def _value_outliers(resources, thresholds, baseline_store=None):
    """Flag numeric values that are extreme vs the distribution for their
    (code, unit)  no hardcoded clinical ranges.

    Robust method: modified z-score ``0.6745·(x−median)/MAD`` with cut-off 3.5
    (Iglewicz & Hoaglin), computed in LOG space for strictly-positive references
    so right-skewed analytes are not over-flagged on the raw scale; falls back to
    the Tukey ``K×IQR`` fence when MAD is 0 (>50% identical values). This is a
    single-distribution check (advisory only): it does NOT detect bimodal
    mixed-unit corruption, which is left to definitional bounds + unit conformance.

    When a ``baseline_store`` is provided, the reference distribution is the
    *accumulated* reservoir for that (code, unit) merged with the current batch
    so small batches gain power and whole-feed drift is caught. The store is then
    updated with the batch's values. With no store the reference is batch-only.
    """
    chk = CheckResult(
        check_id="plausibility.value_outlier",
        category="plausibility",
        subcategory="atemporal",
        context="verification",
        threshold=threshold_for("plausibility.value_outlier", thresholds),
        description=(
            "Numeric values are not extreme outliers vs the "
            f"{'accumulated' if baseline_store else 'batch'} per-(code,unit) "
            "distribution (modified z-score / IQR)."
        ),
        recommendation=(
            "Investigate flagged values for mis-keying or unit errors; confirm "
            "against an authoritative reference range for the measurement."
        ),
        hdqt_category="plausibility",
        hdqt_dimension="situationally_implausible",
    )
    for (code, unit), pairs in _numeric_groups(resources).items():
        batch_values = [v for v, _ in pairs]
        reference = list(batch_values)
        if baseline_store is not None:
            reference = baseline_store.get(code, unit) + reference
        if len(reference) >= OUTLIER_MIN_SAMPLE:
            is_outlier = _outlier_predicate(reference)
            if is_outlier is not None:
                chk.applicable += len(batch_values)
                for value, rid in pairs:
                    if is_outlier(value):
                        chk.violations += 1
                        chk.add_detail(
                            resource_type="Observation",
                            resource_id=rid,
                            path=f"valueQuantity ({code} {unit})",
                            value=f"{value} {unit}".strip(),
                            detail="value is an extreme outlier vs the per-(code,unit) distribution",
                        )
        if baseline_store is not None:
            baseline_store.update(code, unit, batch_values)
    return chk


def _outlier_predicate(reference: list[float]):
    """Return a fn(value)->bool flagging outliers, or None if undeterminable.

    Right-skewed positive clinical analytes (CRP, ferritin, troponin, D-dimer,
    bilirubin) are approximately log-normal, so a SYMMETRIC modified-z / IQR fence
    on the raw scale over-flags the legitimate right tail and under-flags the left.
    When the reference is strictly positive we fence in LOG space (multiplicative
    skew → additive), the standard transform for analytes; otherwise we fence on
    the raw scale. Iglewicz & Hoaglin's 0.6745/3.5 constants assume an approximately
    normal distribution, which the log transform restores.

    This is a single-distribution check: it cannot catch a bimodal mixed-unit
    population (two unit scales merged into one (code,unit))  a legitimately large
    spread, not an outlier. That corruption is the job of ``definitional_bounds``
    and unit conformance, not a per-(code,unit) outlier fence.
    """
    use_log = all(x > 0 for x in reference)
    tref = [log(x) for x in reference] if use_log else list(reference)

    def _t(v: float):
        # Map a tested value into the fence's space. A non-positive value where the
        # reference distribution is strictly positive is itself implausible → flag.
        if use_log:
            return log(v) if v > 0 else None
        return v

    med = median(tref)
    mad = median([abs(x - med) for x in tref])
    if mad > 0:
        cut = OUTLIER_MODZ_CUTOFF

        def _is_modz(v: float) -> bool:
            tv = _t(v)
            return True if tv is None else abs(0.6745 * (tv - med) / mad) > cut

        return _is_modz

    # MAD == 0 (mostly identical values) → Tukey IQR fence in the same space.
    q1, _m, q3 = quantiles(tref, n=4)
    iqr = q3 - q1
    if iqr <= 0:
        return None  # degenerate distribution → nothing to flag
    lo, hi = q1 - OUTLIER_IQR_K * iqr, q3 + OUTLIER_IQR_K * iqr

    def _is_iqr(v: float) -> bool:
        tv = _t(v)
        return True if tv is None else (tv < lo or tv > hi)

    return _is_iqr


def _distribution_drift(resources, thresholds, baseline_store=None):
    """Cross-batch / cross-time consistency: each (code, unit) group's batch
    median is compared to the accumulated baseline. A shift beyond a robust
    modified-z cut-off (vs the baseline median/MAD) flags a distribution drift
    a unit change, recalibration, or feed switch the internal outlier check (which
    only sees the current batch) cannot detect.

    NA without a baseline store, and per-group NA until the baseline holds enough
    samples (``OUTLIER_MIN_SAMPLE``) to be a stable reference.
    """
    chk = CheckResult(
        check_id="plausibility.distribution_drift",
        category="plausibility",
        subcategory="atemporal",
        context="verification",
        threshold=threshold_for("plausibility.distribution_drift", thresholds),
        description=(
            "Per-(code,unit) value distribution is consistent with the accumulated "
            "cross-batch baseline (no median drift)."
        ),
        recommendation=(
            "Investigate a sudden distribution shift  unit change, device "
            "recalibration, or a switched source feed."
        ),
        hdqt_category="plausibility",
        hdqt_dimension="situationally_implausible",
    )
    if baseline_store is None:
        return chk  # NA  drift requires an accumulated cross-batch baseline

    for (code, unit), pairs in _numeric_groups(resources).items():
        baseline = baseline_store.get(code, unit)
        batch_values = [v for v, _ in pairs]
        if len(baseline) < OUTLIER_MIN_SAMPLE or not batch_values:
            continue
        bmed = median(baseline)
        bmad = median([abs(x - bmed) for x in baseline])
        chk.applicable += 1
        batch_med = median(batch_values)
        if bmad > 0:
            drifted = abs(0.6745 * (batch_med - bmed) / bmad) > DRIFT_MODZ_CUTOFF
        else:
            # Degenerate (all-identical) baseline → modified-z undefined. Use a
            # RELATIVE shift tolerance rather than exact float equality, which
            # would hair-trigger on a sub-ULP difference for float analytes.
            drifted = abs(batch_med - bmed) > DRIFT_ZERO_MAD_REL_TOL * max(
                abs(bmed), 1e-9
            )
        if drifted:
            chk.violations += 1
            chk.add_detail(
                resource_type="Observation",
                resource_id="",
                path=f"valueQuantity ({code} {unit})",
                detail=(
                    f"batch median {batch_med} drifted from baseline median {bmed}"
                ),
            )
    return chk


def _concordance(resources, thresholds, rules: list[dict]):
    """Cross-field consistency (HL7 EHR-S 'concordance' / OHDSI gender-concept).

    Each rule forbids a set of codes for a given Patient.gender, e.g. pregnancy
    codes on a male patient. Rules are config-driven and ship as illustrative
    examples only  production deployments should bind these to terminology
    value sets (FHIRPath ``memberOf``) rather than literal code lists.
    """
    chk = CheckResult(
        check_id="plausibility.concordance",
        category="plausibility",
        subcategory="atemporal",
        context="verification",
        threshold=threshold_for("plausibility.concordance", thresholds),
        description="Cross-field values are concordant (e.g. sex-specific concepts match gender).",
        recommendation="Resolve contradictions (likely a mis-linked or mis-coded record).",
        hdqt_category="plausibility",
        hdqt_dimension="situationally_implausible",
    )
    if not rules:
        return chk  # applicable=0 → NA (no concordance rules configured)

    gender_by_patient = {
        f"Patient/{r['id']}": r.get("gender")
        for r in resources
        if isinstance(r, dict) and r.get("resourceType") == "Patient" and r.get("id")
    }

    def _subject(res):
        for fld in ("subject", "patient"):
            node = res.get(fld)
            if isinstance(node, dict) and isinstance(node.get("reference"), str):
                ref = node["reference"]
                # Normalize to exact "ResourceType/id" key so we don't loose-match
                # e.g. "RelatedPerson/Patient/123" against key "Patient/123".
                parts = ref.split("?", 1)[0].rstrip("/").rsplit("/", 1)
                return f"{parts[-2]}/{parts[-1]}" if len(parts) == 2 else ref
        return None

    def _codes(res):
        out = set()
        code = res.get("code")
        if isinstance(code, dict):
            for c in code.get("coding", []):
                if isinstance(c, dict) and c.get("code"):
                    out.add(str(c["code"]))
        return out

    for rule in rules:
        rtype = rule.get("resource_type")
        forbidden = set(rule.get("forbidden_codes", []))
        gender = rule.get("when_gender")
        if not (rtype and forbidden and gender):
            continue
        for res in resources:
            if not isinstance(res, dict) or res.get("resourceType") != rtype:
                continue
            ref = _subject(res)
            pg = gender_by_patient.get(ref) if ref else None
            if pg is None:
                continue  # subject gender unknown → not assessed
            chk.applicable += 1
            if pg == gender and bool(_codes(res) & forbidden):
                chk.violations += 1
                chk.add_detail(
                    resource_type=rtype,
                    resource_id=res.get("id", ""),
                    path="code",
                    detail=f"sex-specific concept on gender='{gender}' patient",
                )
    return chk


def _uniqueness(resources, thresholds):
    chk = CheckResult(
        check_id="plausibility.uniqueness",
        category="plausibility",
        subcategory="uniqueness",
        context="verification",
        threshold=threshold_for("plausibility.uniqueness", thresholds),
        description="No duplicate ResourceType/id within the batch.",
        recommendation="De-duplicate resources sharing the same logical id.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_grouping",
    )
    keys = [
        f"{r.get('resourceType')}/{r.get('id')}"
        for r in resources
        if isinstance(r, dict) and r.get("resourceType") and r.get("id")
    ]
    counts = Counter(keys)
    chk.applicable = len(keys)
    # Violations: every resource beyond the first occurrence of a key.
    chk.violations = sum(c - 1 for c in counts.values() if c > 1)
    return chk
