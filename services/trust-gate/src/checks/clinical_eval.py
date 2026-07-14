"""Clinical data evaluation over OMOP (Phase 3B): logic, distribution, outliers.

Goes beyond structural conformance to evaluate whether the clinical data is
*logical* and *distributionally plausible*:

  - 3B.1 demographic stratification  age band x sex (clinical reference ranges
    are demographic-dependent: a normal pediatric value must not be judged against
    an adult pool).
  - 3B.2 stratified outlier signal  robust, reproducible, ADVISORY (statistical /
    batch-relative, so it never moves the verdict; Phase 1.5).
  - 3B.3 clinical-logic coherence  deterministic cross-field rule (a measurement
    cannot predate the person's birth); this one DOES drive the verdict.

No universal clinical ranges are hardcoded; plausibility is distribution-driven
and demographically conditioned (the OHDSI reversal of bundled ranges stands).
"""

from __future__ import annotations

import functools
import os

from checks.plausibility import _outlier_predicate
from constants import OUTLIER_MIN_SAMPLE
from passport import CheckResult
from phases import TEMPORAL_PLAUSIBILITY, VALUE_PLAUSIBILITY

# Age bands (years). Pediatric vs adult reference ranges differ materially
# (e.g. creatinine, hemoglobin, many vitals), so values are judged within a band.
DEFAULT_AGE_BANDS: tuple[tuple[int, int], ...] = (
    (0, 1),
    (1, 5),
    (5, 12),
    (12, 18),
    (18, 40),
    (40, 65),
    (65, 200),
)
_SEX = {8507: "M", 8532: "F"}


def _age_band(age: int) -> str:
    for lo, hi in DEFAULT_AGE_BANDS:
        if lo <= age < hi:
            return f"{lo}-{hi}"
    return "unknown"


def _year(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        return int(value[:4])
    return None


def _iso_date(value) -> str | None:
    """Normalize to a lexically comparable YYYY-MM-DD prefix, or None."""
    if isinstance(value, str) and len(value) >= 10 and value[:4].isdigit():
        return value[:10]
    return None


def _person_index(omop) -> dict:
    """person_id -> (year_of_birth, sex)."""
    idx: dict = {}
    for r in omop.rows("person"):
        idx[r.get("person_id")] = (
            _year(r.get("year_of_birth")),
            _SEX.get(r.get("gender_concept_id"), "U"),
        )
    return idx


def stratified_measurements(omop) -> list[tuple[str, str, str, float]]:
    """(concept_key, unit, stratum, value) for numeric measurements (3B.1)."""
    persons = _person_index(omop)
    out: list[tuple[str, str, str, float]] = []
    for m in omop.rows("measurement"):
        v = m.get("value_as_number")
        if not isinstance(v, (int, float)):
            continue
        concept = (
            m.get("measurement_concept_id") or m.get("measurement_source_value") or "?"
        )
        unit = m.get("unit_source_value") or m.get("unit_concept_id") or ""
        yob, sex = persons.get(m.get("person_id"), (None, "U"))
        myear = _year(m.get("measurement_date"))
        band = (
            _age_band(myear - yob)
            if (yob is not None and myear is not None)
            else "unknown"
        )
        out.append((str(concept), str(unit), f"{sex}|{band}", float(v)))
    return out


def _summ(vals: list[float], bins: int = 10) -> dict:
    """Robust summary + a histogram (bin counts) for one value series."""
    import statistics

    n = len(vals)
    lo, hi = min(vals), max(vals)
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals) if n > 1 else 0.0
    med = statistics.median(vals)
    # Histogram bins across [lo, hi]; a flat series collapses to one bar.
    hist: list[dict] = []
    if hi > lo:
        width = (hi - lo) / bins
        counts = [0] * bins
        for v in vals:
            idx = min(int((v - lo) / width), bins - 1)
            counts[idx] += 1
        hist = [
            {
                "x0": round(lo + i * width, 4),
                "x1": round(lo + (i + 1) * width, 4),
                "n": c,
            }
            for i, c in enumerate(counts)
        ]
    else:
        hist = [{"x0": lo, "x1": hi, "n": n}]
    return {
        "count": n,
        "min": round(lo, 4),
        "max": round(hi, 4),
        "mean": round(mean, 4),
        "stddev": round(sd, 4),
        "median": round(med, 4),
        "histogram": hist,
    }


def value_distributions(
    omop, max_distinct: int | None = None
) -> tuple[list[dict], list[dict]]:
    """Per-(concept, unit) clinical-value distributions for the passport profile.

    Returns ``(observation_value_stats, observation_value_stats_stratified)``:
    a flat distribution per measurement concept (for the analyst-facing
    box-plot/histogram) and the same conditioned on the age-band x sex stratum
    (the demographically-honest view  a normal pediatric value is not pooled
    against adults). Built from the same numeric measurements the outlier check
    uses, so the graphs match the verdict.
    """
    flat: dict[tuple[str, str], list[float]] = {}
    strat: dict[tuple[str, str, str], list[float]] = {}
    for concept, unit, stratum, v in stratified_measurements(omop):
        flat.setdefault((concept, unit), []).append(v)
        strat.setdefault((concept, unit, stratum), []).append(v)

    # max_distinct=None (default) keeps every concept  the analyst sees the whole
    # clinical panel. A positive limit is honoured for callers that want the top-N.
    flat_lim = max_distinct
    strat_lim = max_distinct * 4 if max_distinct is not None else None
    obs = [
        {"code": c, "unit": u, **_summ(v)}
        for (c, u), v in sorted(flat.items(), key=lambda kv: len(kv[1]), reverse=True)[
            :flat_lim
        ]
    ]
    obs_strat = [
        {"code": c, "unit": u, "stratum": s, **_summ(v)}
        for (c, u, s), v in sorted(
            strat.items(), key=lambda kv: len(kv[1]), reverse=True
        )[:strat_lim]
    ]
    return obs, obs_strat


def stratified_outlier_check(omop) -> CheckResult:
    """ADVISORY stratified outlier signal (3B.2).

    Flags measurement values extreme within their (concept, unit, demographic
    stratum) distribution, using the shared robust estimator. Non-deterministic in
    spirit (batch-relative) so it is advisory-only and never moves the verdict.
    """
    groups: dict[tuple[str, str, str], list[float]] = {}
    for concept, unit, stratum, v in stratified_measurements(omop):
        groups.setdefault((concept, unit, stratum), []).append(v)

    applicable = 0
    violations = 0
    for values in groups.values():
        if len(values) < OUTLIER_MIN_SAMPLE:
            continue
        predicate = _outlier_predicate(values)
        if predicate is None:
            continue
        applicable += len(values)
        violations += sum(1 for v in values if predicate(v))

    c = CheckResult(
        check_id="plausibility.value_outlier_stratified",
        category="plausibility",
        subcategory="atemporal",
        context="validation",
        applicable=applicable,
        violations=violations,
        threshold=0.0,
        description="Measurement values within their (concept, unit, age x sex) distribution",
        recommendation="Review values extreme for their demographic stratum",
    )
    c.phase = VALUE_PLAUSIBILITY
    c.dimension = "accuracy"
    c.advisory = True
    return c


def measurement_after_birth_check(omop) -> CheckResult:
    """Deterministic clinical-logic coherence (3B.3): a measurement cannot predate
    the person's birth year. Drives the verdict (not advisory)."""
    persons = {
        r.get("person_id"): _year(r.get("year_of_birth")) for r in omop.rows("person")
    }
    applicable = 0
    violations = 0
    details: list[dict] = []
    for m in omop.rows("measurement"):
        myear = _year(m.get("measurement_date"))
        yob = persons.get(m.get("person_id"))
        if myear is None or yob is None:
            continue
        applicable += 1
        if myear < yob:
            violations += 1
            details.append(
                {
                    "resource_type": "measurement",
                    "resource_id": str(m.get("measurement_id", ""))[:64],
                    "path": "measurement_date",
                    "value": str(m.get("measurement_date", "")),
                    "detail": f"measured {myear} but person born {yob} (before birth)",
                }
            )
    c = CheckResult(
        check_id="clinical.measurement_after_birth",
        category="plausibility",
        subcategory="temporal",
        context="verification",
        applicable=applicable,
        violations=violations,
        threshold=0.0,
        description="measurement_date is on/after the person's birth year",
        recommendation="Investigate measurements dated before the patient's birth",
    )
    for d in details[:50]:
        c.add_detail(**d)
    c.phase = TEMPORAL_PLAUSIBILITY
    c.dimension = "consistency"
    return c


@functools.lru_cache(maxsize=1)
def load_clinical_rules() -> dict:
    """Load config/clinical_rules.yaml (cached). Empty dict when absent."""
    env = os.environ.get("TRUST_GATE_CLINICAL_RULES_PATH", "").strip()
    path = env or os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..",
            "config",
            "clinical_rules.yaml",
        )
    )
    try:
        import yaml

        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except (FileNotFoundError, ValueError):
        return {}


def event_after_death_check(omop, rules: dict | None = None) -> CheckResult:
    """Deterministic cross-field coherence (C4): a clinical event must not be dated
    after the person's death. Inert (NA) without a populated death table. Drives
    the verdict (date comparisons are lexical over ISO YYYY-MM-DD)."""
    rules = rules if rules is not None else load_clinical_rules()
    spec = rules.get("events_after_death") or {}
    deaths = {
        r.get("person_id"): _iso_date(r.get("death_date"))
        for r in omop.rows("death")
        if _iso_date(r.get("death_date"))
    }
    applicable = 0
    violations = 0
    details: list[dict] = []
    for ev in spec.get("events", []) if deaths else []:
        table, date_col = ev.get("table"), ev.get("date")
        for row in omop.rows(table):
            dod = deaths.get(row.get("person_id"))
            edate = _iso_date(row.get(date_col))
            if dod is None or edate is None:
                continue
            applicable += 1
            if edate > dod:
                violations += 1
                details.append(
                    {
                        "resource_type": table,
                        "resource_id": str(
                            row.get(f"{table}_id", row.get("person_id", ""))
                        )[:64],
                        "path": date_col,
                        "value": edate,
                        "detail": f"{table}.{date_col}={edate} is after death on {dod}",
                    }
                )
    c = CheckResult(
        check_id="clinical.event_after_death",
        category="plausibility",
        subcategory="temporal",
        context="verification",
        applicable=applicable,
        violations=violations,
        threshold=0.0,
        description="Clinical events are dated on/before the person's death_date.",
        recommendation="Investigate events dated after the patient's recorded death.",
    )
    for d in details[:50]:
        c.add_detail(**d)
    c.phase = TEMPORAL_PLAUSIBILITY
    c.dimension = "consistency"
    return c


def evaluate(omop) -> list[CheckResult]:
    """All clinical-evaluation checks for an OMOP dataset."""
    return [
        stratified_outlier_check(omop),
        measurement_after_birth_check(omop),
        event_after_death_check(omop),
    ]
