"""Descriptive data profiling  analysis support, NOT a scored quality check.

The Kahn/OHDSI checks answer *"is the data good enough?"*. Profiling answers
*"what is in the data?"*  the descriptive statistics an analyst needs before
modelling: resource-type counts, terminology-system distribution, numeric value
summaries, and reference density. It carries no PASS/FAIL and never affects the
decision; it is attached to the passport as a separate ``profile`` block.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from checks.clinical_eval import _age_band
from constants import PROFILE_MAX_DISTINCT, TEMPORAL_FIELD_NAMES

# Date-bearing keys to span for the EHDS "time period covered" coverage element.
_PERIOD_KEYS = TEMPORAL_FIELD_NAMES | {"start", "end", "date"}


def _iter_dates(resource: dict):
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and (k in _PERIOD_KEYS or k.endswith("DateTime")):
                    yield v
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(node, list):
            stack.extend(node)


def _time_period(resources: list[dict]) -> dict | None:
    """Min/max clinical date across the batch (EHDS Art.56 'time period covered').

    ISO dates compare lexically, so YYYY / YYYY-MM / YYYY-MM-DD all order correctly
    for a span. None when the batch carries no parseable dates.
    """
    lo = hi = None
    for r in resources:
        for v in _iter_dates(r):
            d = v[:10]
            if len(d) >= 4 and d[:4].isdigit():
                if lo is None or d < lo:
                    lo = d
                if hi is None or d > hi:
                    hi = d
    return {"start": lo, "end": hi} if lo is not None else None


def _iter_codings(resource: dict):
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            coding = node.get("coding")
            if isinstance(coding, list):
                for c in coding:
                    if isinstance(c, dict):
                        yield c
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)


def _top(counter: Counter, n: int = PROFILE_MAX_DISTINCT) -> dict[str, int]:
    return {k: v for k, v in counter.most_common(n)}


def _obs_code(res: dict) -> str | None:
    code = res.get("code")
    if not isinstance(code, dict):
        return None
    for c in code.get("coding", []):
        if isinstance(c, dict) and c.get("code"):
            return str(c["code"])
    return None


def _obs_display(res: dict) -> str | None:
    """Human label for an Observation's code, taken from the source FHIR itself:
    the first coding ``display`` (any system), else ``code.text``. Carried into
    the profile so the UI labels every code present in the data without a static
    LOINC table (the source already named it). PHI-free: a code display, never a
    value."""
    code = res.get("code")
    if not isinstance(code, dict):
        return None
    for c in code.get("coding", []):
        if isinstance(c, dict) and c.get("display"):
            return str(c["display"])
    text = code.get("text")
    return str(text) if isinstance(text, str) and text else None


# Demographic stratification for FHIR Observations  same age-band x sex strata
# the OMOP clinical-eval path uses, so the flat and stratified clinical-value cards
# read identically across source models. PHI-free: bands + sex letter + LOINC only.
_GENDER = {"male": "M", "female": "F"}


def _patient_index(valid: list[dict]) -> dict[str, tuple[int | None, str]]:
    """Patient.id -> (birth_year, sex letter). Missing/unparseable -> (None, 'U')."""
    idx: dict[str, tuple[int | None, str]] = {}
    for r in valid:
        if r.get("resourceType") != "Patient" or not r.get("id"):
            continue
        bd = r.get("birthDate")
        byear = (
            int(bd[:4])
            if isinstance(bd, str) and len(bd) >= 4 and bd[:4].isdigit()
            else None
        )
        idx[str(r["id"])] = (byear, _GENDER.get(r.get("gender"), "U"))
    return idx


def _subject_id(res: dict) -> str | None:
    """The Patient id an Observation references via subject.

    Resolves both server-relative refs (``Patient/<id>``  what a HAPI scan
    returns) and intra-bundle ``urn:uuid:<uuid>`` refs (what a freshly imported
    Synthea-style transaction Bundle carries, where each Patient's id is its uuid).
    """
    subj = res.get("subject")
    if not isinstance(subj, dict):
        return None
    ref = subj.get("reference")
    if not isinstance(ref, str):
        return None
    if "Patient/" in ref:
        return ref.split("Patient/", 1)[1].split("?")[0].split("/")[0]
    if ref.startswith("urn:uuid:"):
        return ref[len("urn:uuid:") :]
    return None


def _obs_year(res: dict) -> int | None:
    """Year the Observation was taken (for age-at-measurement banding)."""
    for k in ("effectiveDateTime", "issued"):
        v = res.get(k)
        if isinstance(v, str) and len(v) >= 4 and v[:4].isdigit():
            return int(v[:4])
    eff = res.get("effectivePeriod")
    if isinstance(eff, dict):
        s = eff.get("start")
        if isinstance(s, str) and len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
    return None


def _stratum(res: dict, patients: dict[str, tuple[int | None, str]]) -> str:
    """Demographic stratum "sex|age-band" for an Observation, "U|unknown" if unknown."""
    byear, sex = patients.get(_subject_id(res) or "", (None, "U"))
    oyear = _obs_year(res)
    band = (
        _age_band(oyear - byear)
        if (byear is not None and oyear is not None)
        else "unknown"
    )
    return f"{sex}|{band}"


def _summ(values: list[float], bins: int = 10) -> dict:
    n = len(values)
    mean = sum(values) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    lo, hi = min(values), max(values)
    sorted_vals = sorted(values)
    median = (
        sorted_vals[n // 2]
        if n % 2
        else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
    )
    if hi > lo:
        width = (hi - lo) / bins
        counts = [0] * bins
        for v in values:
            counts[min(int((v - lo) / width), bins - 1)] += 1
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
        "stddev": round(std, 4),
        "median": round(median, 4),
        "histogram": hist,
    }


def profile(resources: list[dict]) -> dict:
    """Compute the descriptive profile for a batch of FHIR resources."""
    valid = [r for r in resources if isinstance(r, dict) and r.get("resourceType")]

    resource_counts = Counter(r["resourceType"] for r in valid)

    code_systems: Counter = Counter()
    for r in valid:
        for coding in _iter_codings(r):
            sys = coding.get("system")
            if isinstance(sys, str) and sys:
                code_systems[sys] += 1

    # Numeric value summary per Observation code (analyst-facing distributions).
    patients = _patient_index(valid)
    by_code: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_stratum: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    display_by_code: dict[tuple[str, str], str] = {}
    for r in valid:
        if r.get("resourceType") != "Observation":
            continue
        vq = r.get("valueQuantity")
        code = _obs_code(r)
        if isinstance(vq, dict) and isinstance(vq.get("value"), (int, float)) and code:
            unit = vq.get("unit") or vq.get("code") or ""
            value = float(vq["value"])
            key = (code, str(unit))
            by_code[key].append(value)
            by_stratum[(code, str(unit), _stratum(r, patients))].append(value)
            if key not in display_by_code:
                disp = _obs_display(r)
                if disp:
                    display_by_code[key] = disp
    # Every distinct numeric (concept, unit) gets a distribution card  no cap, so
    # the analyst sees the patient's whole clinical panel. Ordered by frequency
    # (most-measured first) purely for display; the long tail is fully retained.
    observation_value_stats = [
        {
            "code": code,
            "unit": unit,
            "display": display_by_code.get((code, unit)),
            **_summ(vals),
        }
        for (code, unit), vals in sorted(
            by_code.items(), key=lambda kv: len(kv[1]), reverse=True
        )
    ]
    # Same distributions conditioned on the demographic (sex x age-band) stratum, so
    # a pediatric value is not pooled against adults. Carries the source FHIR label.
    observation_value_stats_stratified = [
        {
            "code": code,
            "unit": unit,
            "display": display_by_code.get((code, unit)),
            "stratum": stratum,
            **_summ(vals),
        }
        for (code, unit, stratum), vals in sorted(
            by_stratum.items(), key=lambda kv: len(kv[1]), reverse=True
        )
    ]

    # Patient gender distribution (common cohort-shaping descriptor).
    gender = Counter(
        r.get("gender")
        for r in valid
        if r.get("resourceType") == "Patient" and r.get("gender")
    )

    # Reference density: literal references per resource (linkage richness).
    ref_total = sum(_count_refs(r) for r in valid)

    return {
        "total_resources": len(valid),
        "resource_counts": _top(resource_counts),
        "distinct_resource_types": len(resource_counts),
        "code_system_distribution": _top(code_systems),
        "observation_value_stats": observation_value_stats,
        "observation_value_stats_stratified": observation_value_stats_stratified,
        "patient_gender_distribution": dict(gender),
        "reference_density": round(ref_total / len(valid), 3) if valid else 0.0,
        "time_period": _time_period(valid),
    }


def _count_refs(resource: dict) -> int:
    n = 0
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("reference"), str):
                n += 1
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    return n
