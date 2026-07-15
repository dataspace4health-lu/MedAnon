"""Re-identification risk metrics for de-identified FHIR Patient datasets.

Computes k-anonymity, l-diversity, and derived risk scores (prosecutor,
journalist, marketer) on the *output* of a de-identification pipeline.

Why this is needed alongside gPAS pseudonymization
---------------------------------------------------
gPAS replaces direct identifiers (Patient.id, MRN) with reversible pseudonyms
this protects against lookup attacks on known IDs.  However, the de-identified
output still contains quasi-identifiers (birth year, gender, zip prefix) and
sensitive attributes (Condition codes) that can be combined to re-identify
individuals through linkage with external datasets ("disease fingerprinting").

k-anonymity measures that residual risk: if a patient's quasi-identifier
combination is shared by fewer than k people in the dataset, they are at risk.
l-diversity adds a check that each k-anonymous group also contains at least l
distinct sensitive-attribute values, preventing attribute inference.

Usage
-----
    from analytics.risk import assess_risk
    report = assess_risk(ndjson_text)          # mixed Patient+Condition NDJSON
    print(report["summary"]["risk_level"])     # "low" | "medium" | "high" | "critical"
"""

from __future__ import annotations

import datetime
import json
import re
from datetime import timezone
from collections import Counter, defaultdict
from typing import Any

# ---------------------------------------------------------------------------
# De-identification sentinel detection
# ---------------------------------------------------------------------------

_QI_SUPPRESSED_VALUES: frozenset[str] = frozenset(
    {
        "",
        "[redacted]",
        "redacted",
        "unknown",
        "other",
        "unk",
        "[removed]",
        "removed",
        "[masked]",
        "masked",
    }
)

_DECADE_DATE_RE = re.compile(r"^\d{3}x$")
# Decade-aligned 4-digit year emitted by the ``date_decade`` generalize strategy
# (e.g. "1940", "1990"). These are coarse buckets, not real birth years, so they
# should be treated as suppressed for k-anonymity purposes  otherwise small
# datasets with patients spread across multiple decades produce false-positive
# k=1 singletons.
_DECADE_YEAR_RE = re.compile(r"^\d{3}0$")


def _is_qi_suppressed(value: str, field: str = "") -> bool:
    """Return True if *value* looks like a de-identification sentinel.

    Checks the lowercase value against known sentinel strings, plus
    field-specific patterns:
      - birth_year: decade-generalized dates like ``"194x"`` or decade-aligned
        4-digit years like ``"1940"`` (output of ``generalize`` strategy
        ``date_decade``)
      - zip_prefix: truncated sentinel markers starting with ``[``
    """
    if not value:
        return True
    lower = value.strip().lower()
    if lower in _QI_SUPPRESSED_VALUES:
        return True
    if field == "birth_year" and (
        _DECADE_DATE_RE.match(value) or _DECADE_YEAR_RE.match(value)
    ):
        return True
    if field == "zip_prefix" and value.startswith("["):
        return True
    return False


# ---------------------------------------------------------------------------
# Quasi-identifier extraction
# ---------------------------------------------------------------------------


def _extract_patient_qi(resource: dict) -> tuple[str, str, str]:
    """Return (gender, birth_year, zip_prefix_3) for one Patient resource.

    Missing / redacted fields produce empty-string sentinels so they still
    participate in grouping (a group of patients all missing gender is a
    valid equivalence class).

    Values that are clearly de-identification outputs (e.g. ``"unknown"``
    gender, decade dates like ``"194x"``, ``"[REDACTED]"`` postal codes)
    are normalised to empty strings so all properly suppressed patients
    collapse into a single equivalence class.
    """
    gender = (resource.get("gender") or "").strip().lower()

    birth_date = (resource.get("birthDate") or "").strip()
    birth_year = birth_date[:4] if len(birth_date) >= 4 else birth_date

    addresses = resource.get("address") or []
    postal = ""
    if addresses and isinstance(addresses, list):
        postal = str(addresses[0].get("postalCode") or "")

    postal_is_suppressed = _is_qi_suppressed(postal)
    zip_prefix = postal[:3]

    if _is_qi_suppressed(gender, "gender"):
        gender = ""
    if _is_qi_suppressed(birth_year, "birth_year"):
        birth_year = ""
    if postal_is_suppressed or _is_qi_suppressed(zip_prefix, "zip_prefix"):
        zip_prefix = ""

    return (gender, birth_year, zip_prefix)


def extract_quasi_identifiers(
    patients: list[dict],
) -> list[tuple[str, str, str]]:
    """Return one QI tuple per Patient resource (same order as *patients*)."""
    return [_extract_patient_qi(p) for p in patients]


# ---------------------------------------------------------------------------
# Condition code map
# ---------------------------------------------------------------------------


def build_conditions_map(resources: list[dict]) -> dict[str, set[str]]:
    """Build a mapping of patient_id → set of Condition codes.

    Correlates Condition resources to their subject Patient via
    ``subject.reference`` (supports "Patient/id" and plain "id" forms).
    """
    mapping: dict[str, set[str]] = defaultdict(set)
    for r in resources:
        if r.get("resourceType") != "Condition":
            continue
        subject_ref = (r.get("subject") or {}).get("reference") or ""
        # Normalise "Patient/abc123" → "abc123"
        patient_id = subject_ref.split("/")[-1] if subject_ref else ""
        if not patient_id:
            continue
        # Extract SNOMED/ICD code  prefer coding[0].code, fall back to text
        code_obj = r.get("code") or {}
        codings = code_obj.get("coding") or []
        code_str = ""
        if codings and isinstance(codings, list):
            code_str = str(codings[0].get("code") or codings[0].get("display") or "")
        if not code_str:
            code_str = code_obj.get("text") or ""
        if code_str:
            mapping[patient_id].add(code_str.strip())
    return dict(mapping)


def _subject_patient_id(resource: dict) -> str:
    """Bare patient id for a linked resource (subject/patient reference)."""
    for ref_field in ("subject", "patient"):
        ref = resource.get(ref_field)
        if isinstance(ref, dict):
            ref_val = ref.get("reference")
            if ref_val:
                return str(ref_val).split("/")[-1]
    return ""


def build_sensitive_map(
    resources: list[dict], sensitive_attributes: list[str] | None
) -> dict[str, set[str]]:
    """Map patient_id → set of sensitive values, driven by configured paths.

    D7.2 §5.5.4 treats the sensitive attribute as *contextual*, not fixed. When
    ``sensitive_attributes`` (a list of FHIRPaths) is supplied, each path is
    grouped by its leading resource type: a ``Patient.*`` path is read from the
    Patient directly; any other (e.g. ``Condition.code.coding.code``,
    ``Observation.valueCodeableConcept.coding.code``) is correlated to its
    subject Patient. When omitted, falls back to Condition codes (the historical
    default) so existing callers are unchanged.
    """
    if not sensitive_attributes:
        return build_conditions_map(resources)

    by_type: dict[str, list[str]] = defaultdict(list)
    for path in sensitive_attributes:
        rtype = path.split(".", 1)[0]
        by_type[rtype].append(path)

    mapping: dict[str, set[str]] = defaultdict(set)
    for r in resources:
        rtype = r.get("resourceType", "")
        paths = by_type.get(rtype)
        if not paths:
            continue
        pid = str(r.get("id") or "") if rtype == "Patient" else _subject_patient_id(r)
        if not pid:
            continue
        for path in paths:
            val = _read_path(r, path)
            if val:
                mapping[pid].add(val)
    return dict(mapping)


# ---------------------------------------------------------------------------
# Configurable risk thresholds + generic quasi-identifier extraction
# ---------------------------------------------------------------------------

# Default k → risk-level bands. TEHDAS2 D7.2 §6 records HDAB-selected k-values
# spanning 3–100: there is no single correct value, so callers may override
# these via ``privacy_model.risk_thresholds``. Read as: min_k ≥ low → "low",
# ≥ medium → "medium", ≥ high → "high", else "critical".
DEFAULT_RISK_THRESHOLDS: dict[str, int] = {"low": 5, "medium": 3, "high": 2}

# The historical hardcoded QI set (kept as the default so existing callers and
# the Patient-centric FHIR path behave exactly as before).
DEFAULT_QI_FIELDS: list[str] = ["gender", "birth_year", "zip_prefix_3"]


def _risk_level_for(min_k: int, thresholds: dict[str, int] | None) -> str:
    """Classify *min_k* into a risk band using *thresholds* (or the default)."""
    t = thresholds or DEFAULT_RISK_THRESHOLDS
    if min_k >= t.get("low", 5):
        return "low"
    if min_k >= t.get("medium", 3):
        return "medium"
    if min_k >= t.get("high", 2):
        return "high"
    return "critical"


def _read_path(resource: dict, path: str) -> str:
    """Read a scalar value at a dotted FHIRPath, walking first list elements.

    Mirrors the light path reader used by ``pipeline.privacy.apply``  no
    fhirpathpy dependency (the analytics service is dependency-slim). The
    resource-type prefix (``Patient.``) is stripped when present.
    """
    parts = path.split(".")
    if len(parts) > 1 and parts[0][:1].isupper():
        parts = parts[1:]
    obj: Any = resource
    for part in parts:
        if isinstance(obj, list):
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return ""
        obj = obj.get(part)
        if obj is None:
            return ""
    if isinstance(obj, list):
        obj = obj[0] if obj else None
    return "" if obj is None else str(obj).strip()


def extract_quasi_identifiers_from_model(
    patients: list[dict], quasi_identifiers: list[dict]
) -> tuple[list[tuple[str, ...]], list[str]]:
    """Extract QI tuples for arbitrary ``privacy_model.quasi_identifiers``.

    Each QI entry is ``{"path": <FHIRPath>, "kind": <str>}``. Returns
    ``(qi_tuples, field_labels)`` where field labels are derived from the QI
    paths. Values that look like de-identification sentinels are normalised to
    empty strings (same collapse behaviour as the built-in extractor).
    """
    labels = [q.get("path", f"qi{i}") for i, q in enumerate(quasi_identifiers)]
    tuples: list[tuple[str, ...]] = []
    for p in patients:
        vals: list[str] = []
        for q in quasi_identifiers:
            raw = _read_path(p, q.get("path", ""))
            vals.append("" if _is_qi_suppressed(raw) else raw)
        tuples.append(tuple(vals))
    return tuples, labels


# ---------------------------------------------------------------------------
# k-anonymity
# ---------------------------------------------------------------------------


def compute_k_anonymity(
    qi_tuples: list[tuple[str, ...]],
    *,
    thresholds: dict[str, int] | None = None,
    qi_field_names: list[str] | None = None,
) -> dict[str, Any]:
    """Compute k-anonymity and derived risk metrics from QI tuples.

    ``thresholds`` overrides the default k → risk-level bands
    (:data:`DEFAULT_RISK_THRESHOLDS`); ``qi_field_names`` overrides the
    reported QI field labels. Both are keyword-only and default to the
    historical hardcoded behaviour, so existing positional callers are
    unaffected.

    Returns a dict with keys: summary, groups.
    """
    qi_fields = qi_field_names or DEFAULT_QI_FIELDS
    if not qi_tuples:
        return {
            "summary": {
                "total_records": 0,
                "total_groups": 0,
                "min_k": 0,
                "max_k": 0,
                "mean_k": 0.0,
                "prosecutor_risk": 0.0,
                "journalist_risk": 0.0,
                "marketer_risk": 0.0,
                "risk_level": "low",
                "qi_fields": qi_fields,
                "singleton_groups": 0,
                "records_with_missing_qi": 0,
            },
            "groups": [],
        }

    group_counts: Counter = Counter(qi_tuples)
    total = len(qi_tuples)
    num_groups = len(group_counts)

    min_k = min(group_counts.values())
    max_k = max(group_counts.values())
    mean_k = round(total / num_groups, 4)

    # Risk formulas
    # Prosecutor: attacker targets a *specific* individual → 1/min_k
    prosecutor_risk = round(1.0 / min_k, 6)
    # Journalist: attacker picks the easiest target → max(1/k_i) = 1/min_k
    journalist_risk = round(max(1.0 / k for k in group_counts.values()), 6)
    # Marketer: randomly drawn record → num_groups / total (simplification of
    # sum_i k_i * (1/k_i) / total = num_groups / total)
    marketer_risk = round(num_groups / total, 6)

    risk_level = _risk_level_for(min_k, thresholds)

    singleton_groups = sum(1 for k in group_counts.values() if k == 1)

    # Records where at least one QI field is missing/sentinel
    records_with_missing_qi = sum(
        k for tup, k in group_counts.items() if any(not v for v in tup)
    )

    groups = [
        {
            "qi": dict(zip(qi_fields, tup)),
            "size": k_i,
            "k": k_i,
            "risk_1_over_k": round(1.0 / k_i, 6),
            "weight": round(k_i / total, 6),
        }
        for tup, k_i in sorted(group_counts.items(), key=lambda x: x[1])
    ]

    return {
        "summary": {
            "total_records": total,
            "total_groups": num_groups,
            "min_k": min_k,
            "max_k": max_k,
            "mean_k": mean_k,
            "prosecutor_risk": prosecutor_risk,
            "journalist_risk": journalist_risk,
            "marketer_risk": marketer_risk,
            "risk_level": risk_level,
            "qi_fields": qi_fields,
            "singleton_groups": singleton_groups,
            "records_with_missing_qi": records_with_missing_qi,
        },
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# l-diversity
# ---------------------------------------------------------------------------


def compute_l_diversity(
    qi_tuples: list[tuple[str, str, str]],
    patient_ids: list[str],
    conditions_by_patient: dict[str, set[str]],
) -> dict[str, Any]:
    """Compute l-diversity using Condition codes as the sensitive attribute.

    For each k-anonymous equivalence class, counts how many *distinct*
    Condition codes are present across all members.  A group with fewer than
    2 distinct codes violates 2-diversity.

    Returns a dict with keys: computed, min_l, max_l, violations, details.
    """
    if not conditions_by_patient:
        return {
            "computed": False,
            "reason": "No Condition resources found in input  include Patient + Condition NDJSON for l-diversity",
        }

    # Build group → set of codes
    group_codes: dict[tuple, set[str]] = defaultdict(set)
    for qi, pid in zip(qi_tuples, patient_ids):
        codes = conditions_by_patient.get(pid, set())
        group_codes[qi].update(codes)

    l_values = [len(codes) for codes in group_codes.values()]
    if not l_values:
        return {
            "computed": False,
            "reason": "Could not correlate Conditions to Patients",
        }

    min_l = min(l_values)
    max_l = max(l_values)
    violations = sum(1 for lv in l_values if lv < 2)

    details = [
        {
            # Keep the named keys for the default 3-QI triple (frontend contract);
            # fall back to generic qi0/qi1/… for any other configured QI count.
            "group": (
                {"gender": qi[0], "birth_year": qi[1], "zip_prefix": qi[2]}
                if len(qi) == 3
                else {f"qi{i}": v for i, v in enumerate(qi)}
            ),
            "l_value": len(codes),
            "distinct_codes": len(codes),
        }
        for qi, codes in sorted(group_codes.items(), key=lambda x: len(x[1]))
        if len(codes) < 2
    ]

    return {
        "computed": True,
        "min_l": min_l,
        "max_l": max_l,
        "violations": violations,
        "details": details[:20],  # cap to avoid huge responses
    }


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def assess_risk_resources(
    resources: list[dict], *, privacy_model: dict | None = None
) -> dict[str, Any]:
    """Compute full re-identification risk report from a pre-parsed resource list.

    Accepts a list of FHIR resource dicts (Patient and/or Condition).  Patients
    supply quasi-identifiers; Conditions supply the sensitive attribute for
    l-diversity.  Other resource types are silently ignored.

    Args:
        resources: List of FHIR resource dicts (any mix of types).

    Returns:
        A risk report dict with keys: summary, l_diversity, groups, warnings, meta.
    """
    warnings: list[str] = []

    patients = [r for r in resources if r.get("resourceType") == "Patient"]
    patient_count = len(patients)
    input_lines = len(resources)

    # Configurable QIs + thresholds (D7.2 §5.5.7): when a privacy_model carries
    # ``quasi_identifiers`` we extract those paths and use its ``risk_thresholds``;
    # otherwise fall back to the built-in Patient QI triple + default bands.
    pm_qis = (privacy_model or {}).get("quasi_identifiers") or None
    pm_thresholds = (privacy_model or {}).get("risk_thresholds") or None

    if patient_count == 0:
        warnings.append(
            "No Patient resources found in input  risk metrics require Patient resources."
        )
        k_result = compute_k_anonymity([], thresholds=pm_thresholds)
        return {
            **k_result,
            "l_diversity": {"computed": False, "reason": "No Patient resources"},
            "warnings": warnings,
            "meta": {
                "computed_at": datetime.datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "input_lines": input_lines,
                "patient_lines": 0,
                "condition_lines": 0,
            },
        }

    patient_ids = [p.get("id") or "" for p in patients]
    if pm_qis:
        qi_tuples, qi_labels = extract_quasi_identifiers_from_model(patients, pm_qis)
    else:
        qi_tuples = extract_quasi_identifiers(patients)
        qi_labels = DEFAULT_QI_FIELDS

    # Sensitive attribute for l-diversity  configurable per D7.2 §5.5.4
    # (``privacy_model.sensitive_attributes``); defaults to Condition codes.
    pm_sensitive = (privacy_model or {}).get("sensitive_attributes") or None
    conditions_by_patient = build_sensitive_map(resources, pm_sensitive)
    condition_count = sum(1 for r in resources if r.get("resourceType") == "Condition")

    missing_count = sum(1 for tup in qi_tuples if any(not v for v in tup))
    if missing_count:
        warnings.append(
            f"{missing_count} Patient record(s) have one or more missing "
            f"quasi-identifier fields ({', '.join(qi_labels)}). "
            "They are grouped under sentinel values  risk may be underestimated."
        )

    k_result = compute_k_anonymity(
        qi_tuples, thresholds=pm_thresholds, qi_field_names=qi_labels
    )
    l_result = compute_l_diversity(qi_tuples, patient_ids, conditions_by_patient)

    return {
        "summary": k_result["summary"],
        "l_diversity": l_result,
        "groups": k_result["groups"],
        "warnings": warnings,
        "meta": {
            "computed_at": datetime.datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "input_lines": input_lines,
            "patient_lines": patient_count,
            "condition_lines": condition_count,
        },
    }


def assess_risk_rows(
    rows: list[dict],
    *,
    quasi_identifiers: list[str],
    sensitive_attribute: str | None = None,
    thresholds: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Re-identification risk over flat **tabular** rows (one subject per row).

    D7.2 §2.1.1 scopes all structured data, standard or not  SQL/CSV, CDA and
    HL7 v2 outputs are row-based, not FHIR Patient resources, so the Patient-only
    :func:`assess_risk_resources` cannot assess them. Here *quasi_identifiers*
    are column names and *sensitive_attribute* (optional) is the column whose
    distinct values per equivalence class give l-diversity. Reuses the same
    k-anonymity engine, so risk levels/thresholds are consistent across FHIR and
    tabular inputs.
    """
    qi_tuples: list[tuple[str, ...]] = []
    for r in rows:
        vals = []
        for col in quasi_identifiers:
            raw = "" if r.get(col) is None else str(r.get(col)).strip()
            vals.append("" if _is_qi_suppressed(raw) else raw)
        qi_tuples.append(tuple(vals))

    k_result = compute_k_anonymity(
        qi_tuples, thresholds=thresholds, qi_field_names=list(quasi_identifiers)
    )

    l_result: dict[str, Any] = {
        "computed": False,
        "reason": "no sensitive_attribute configured",
    }
    if sensitive_attribute:
        group_codes: dict[tuple, set[str]] = defaultdict(set)
        for r, qi in zip(rows, qi_tuples):
            v = (
                ""
                if r.get(sensitive_attribute) is None
                else str(r.get(sensitive_attribute)).strip()
            )
            if v:
                group_codes[qi].add(v)
        if group_codes:
            l_values = [len(c) for c in group_codes.values()]
            l_result = {
                "computed": True,
                "min_l": min(l_values),
                "max_l": max(l_values),
                "violations": sum(1 for lv in l_values if lv < 2),
                "details": [],
            }

    return {
        "summary": k_result["summary"],
        "l_diversity": l_result,
        "groups": k_result["groups"],
        "warnings": [],
        "meta": {
            "computed_at": datetime.datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "input_lines": len(rows),
            "input_kind": "tabular",
        },
    }


def assess_risk(ndjson_text: str) -> dict[str, Any]:
    """Parse mixed FHIR NDJSON and compute full re-identification risk report.

    Thin wrapper around :func:`assess_risk_resources` for NDJSON input.
    For JSON Bundle or XML input use :func:`assess_risk_resources` directly
    after parsing with ``pipeline.io_formats.parse_payload_bytes``.

    Args:
        ndjson_text: Raw NDJSON text, one FHIR JSON object per line.

    Returns:
        A risk report dict with keys: summary, l_diversity, groups, warnings, meta.

    Raises:
        ValueError: If NDJSON cannot be parsed.
    """
    all_resources: list[dict] = []
    for lineno, raw in enumerate(ndjson_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at line {lineno}: {exc}") from exc
        if isinstance(obj, dict):
            all_resources.append(obj)
    return assess_risk_resources(all_resources)
