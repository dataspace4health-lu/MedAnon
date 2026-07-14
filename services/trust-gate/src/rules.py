"""Clinical-plausibility rule engine (Kahn `plausibility` category).

Declarative YAML rules (in ``config/checks.yaml`` under ``plausibility_rules``),
mirroring the load-time validation philosophy of the anonymizer rule schema
(``pipeline/config/rule_schema.py``): a typed Pydantic model, ``extra="forbid"``,
and a pure ``validate_rules`` that returns error strings rather than raising.

Each rule produces one :class:`CheckResult`. ``kind`` maps to the Kahn
subcategory/context:

  date_order / period_order / date_after_birth → temporal  / verification
  value_range                                  → atemporal / verification

Same-resource field access uses FHIRPath via ``fhirpathpy``; cross-resource rules
(date-after-birth) build a batch Patient index. Reference integrity and
uniqueness are built-in conformance/plausibility checks (see ``checks/``), not
rules.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from passport import CheckResult

_log = logging.getLogger("trust_gate.rules")

_KIND = Literal[
    "date_order",
    "period_order",
    "date_after_birth",
    "date_before_death",
    "not_in_future",
    "value_range",
]

# kind → (subcategory, context) within the Kahn plausibility category.
_KIND_TAXONOMY: dict[str, tuple[str, str]] = {
    "date_order": ("temporal", "verification"),
    "period_order": ("temporal", "verification"),
    "date_after_birth": ("temporal", "verification"),
    "date_before_death": ("temporal", "verification"),
    "not_in_future": ("temporal", "verification"),
    "value_range": ("atemporal", "verification"),
}


class PlausibilityRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    description: str = ""
    resource_type: str = Field(min_length=1)
    kind: _KIND
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Literal["minor", "major", "critical"] = "major"
    criticality: Literal["blocker", "non_blocker"] = "non_blocker"
    threshold: float = 0.0
    recommendation: str = ""


def validate_rules(raw_rules: list[dict]) -> list[str]:
    """Validate raw rule dicts; return a list of error strings (never raises)."""
    errors: list[str] = []
    for idx, rule in enumerate(raw_rules, start=1):
        label = (
            rule.get("rule_id") if isinstance(rule, dict) else None
        ) or f"rules[{idx}]"
        try:
            PlausibilityRule.model_validate(rule)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) or "(rule)"
                errors.append(f"{label}: {loc}: {err['msg']}")
    return errors


def _config_path() -> str:
    env = os.environ.get("TRUST_GATE_CHECKS_PATH", "").strip()
    if env:
        return env
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config",
        "checks.yaml",
    )


def load_config(
    path: str | None = None,
) -> tuple[list[PlausibilityRule], dict[str, float]]:
    """Load + validate the check config. Returns (rules, threshold_overrides)."""
    path = path or _config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        _log.warning("check config not found at %s", path)
        return [], {}
    raw = doc.get("plausibility_rules", []) if isinstance(doc, dict) else []
    for e in validate_rules(raw):
        _log.warning("plausibility rule invalid: %s", e)
    rules: list[PlausibilityRule] = []
    for r in raw:
        try:
            rules.append(PlausibilityRule.model_validate(r))
        except ValidationError:
            continue
    thresholds = doc.get("thresholds", {}) if isinstance(doc, dict) else {}
    return rules, {str(k): float(v) for k, v in thresholds.items()}


def _normalize_rate(value: float) -> float:
    """Normalize a pass-rate threshold to the percentage scale (0–100).

    Config + env supply per-resource-type thresholds as *fractions* (``0.95``),
    but the engine compares them against pass-rates already expressed as
    *percentages* (``PASS_MIN_RATE`` etc., 0–100). Without this, ``50.0 < 0.95``
    is always False and the entire Phase 2A calibration is a silent no-op.

    Any value ``<= 1.0`` is treated as a fraction and scaled ×100; values already
    on the percentage scale (e.g. ``95``) pass through unchanged. ``1.0`` maps to
    ``100`` (require-all), which is the intended meaning of a 1.0 fraction.
    """
    return value * 100.0 if value <= 1.0 else value


def load_policy(path: str | None = None) -> dict:
    """Load the non-rule policy config: concordance rules, definitional unit
    bounds, and per-resource-type pass-rate thresholds.

    Returns:
    ``{
        "concordance_rules": [...],
        "definitional_bounds": {...},
        "resource_thresholds": {...},
    }``
    (all keys present and safe when absent).

    ``resource_thresholds`` values are normalized to the percentage scale (0–100)
    so they compare correctly against engine pass-rates (see ``_normalize_rate``).
    """
    path = path or _config_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {
            "concordance_rules": [],
            "definitional_bounds": {},
            "resource_thresholds": {},
        }
    if not isinstance(doc, dict):
        return {
            "concordance_rules": [],
            "definitional_bounds": {},
            "resource_thresholds": {},
        }
    raw_bounds = doc.get("definitional_unit_bounds", {}) or {}
    bounds: dict[str, tuple[float, float]] = {}
    for unit, pair in raw_bounds.items():
        try:
            bounds[str(unit)] = (float(pair[0]), float(pair[1]))
        except (TypeError, ValueError, IndexError):
            _log.warning("invalid definitional_unit_bounds entry for %r", unit)
    concordance = doc.get("concordance_rules", []) or []
    raw_rt = doc.get("resource_thresholds", {}) or {}
    resource_thresholds: dict[str, float] = {}
    for rtype, val in raw_rt.items():
        try:
            resource_thresholds[str(rtype)] = _normalize_rate(float(val))
        except (TypeError, ValueError):
            _log.warning("invalid resource_thresholds entry for %r", rtype)
    # Env override: TRUST_GATE_RESOURCE_THRESHOLDS_JSON (JSON map).
    import json as _json
    import os as _os

    env_rt = _os.environ.get("TRUST_GATE_RESOURCE_THRESHOLDS_JSON", "").strip()
    if env_rt:
        try:
            env_map = _json.loads(env_rt)
            if isinstance(env_map, dict):
                resource_thresholds.update(
                    {str(k): _normalize_rate(float(v)) for k, v in env_map.items()}
                )
        except Exception:  # noqa: BLE001
            _log.warning(
                "TRUST_GATE_RESOURCE_THRESHOLDS_JSON is not valid JSON  ignored"
            )
    return {
        "concordance_rules": concordance if isinstance(concordance, list) else [],
        "definitional_bounds": bounds,
        "resource_thresholds": resource_thresholds,
    }


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _fhirpath(resource: dict, expr: str) -> list:
    try:
        from fhirpathpy import evaluate as _eval

        return _eval(resource, expr) or []
    except Exception as exc:  # noqa: BLE001  FHIRPath errors must not crash the gate
        _log.debug("fhirpath eval failed for %r: %s", expr, exc)
        return []


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_instant(s: str):
    """Parse a FHIR dateTime carrying a timezone offset → aware UTC datetime.

    Returns None for date-only values (``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``) or
    naive datetimes (no offset)  those cannot be ordered as instants, so callers
    fall back to lexicographic prefix comparison (which is chronologically correct
    for the offset-free FHIR date subset).
    """
    import datetime as _dt

    if "T" not in s:
        return None  # date-only → lexicographic prefix compare
    try:
        norm = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = _dt.datetime.fromisoformat(norm)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # naive datetime (no offset) → lexicographic
    return dt.astimezone(_dt.timezone.utc)


def _compare_dates(a: str, b: str) -> int:
    """Order two FHIR date/dateTime strings → -1 (a<b) / 0 (a==b) / 1 (a>b).

    Uses a true instant comparison when BOTH operands carry a timezone offset
    (so e.g. ``...+14:00`` and ``...Z`` sort by the actual moment, not by their
    raw string); otherwise falls back to a length-matched lexicographic prefix
    compare  correct for the offset-free FHIR date subset and partial dates.
    """
    ia, ib = _to_instant(a), _to_instant(b)
    if ia is not None and ib is not None:
        return -1 if ia < ib else (1 if ia > ib else 0)
    n = min(len(a), len(b))
    pa, pb = a[:n], b[:n]
    return -1 if pa < pb else (1 if pa > pb else 0)


def _first_date(resource: dict, expr: str) -> str | None:
    for v in _fhirpath(resource, expr):
        if isinstance(v, str) and v:
            return v
    return None


def _first_date_any(resource: dict, expr) -> str | None:
    """First non-empty date among one or more FHIRPath expressions.

    ``expr`` may be a single path or a list of fallbacks tried in order  e.g.
    ``["performedDateTime", "performedPeriod.start"]`` so a rule anchored on an
    instant still resolves resources that carry only a period (Procedure, the
    common Synthea/real-world shape).
    """
    exprs = expr if isinstance(expr, list) else [expr]
    for e in exprs:
        if isinstance(e, str):
            v = _first_date(resource, e)
            if v is not None:
                return v
    return None


def _birth_index(resources: list[dict]) -> dict[str, str]:
    birth: dict[str, str] = {}
    for r in resources:
        if isinstance(r, dict) and r.get("resourceType") == "Patient" and r.get("id"):
            bd = r.get("birthDate")
            if isinstance(bd, str):
                birth[f"Patient/{r['id']}"] = bd
    return birth


def _death_index(resources: list[dict]) -> dict[str, str]:
    """Patient/id → deceasedDateTime (only patients with an explicit death date)."""
    death: dict[str, str] = {}
    for r in resources:
        if isinstance(r, dict) and r.get("resourceType") == "Patient" and r.get("id"):
            dd = r.get("deceasedDateTime")
            if isinstance(dd, str) and dd:
                death[f"Patient/{r['id']}"] = dd
    return death


def _subject_ref(resource: dict) -> str | None:
    for fld in ("subject", "patient"):
        node = resource.get(fld)
        if isinstance(node, dict) and isinstance(node.get("reference"), str):
            return node["reference"]
    return None


def _has_code(resource: dict, codes: set[str]) -> bool:
    """True when resource.code.coding contains any of *codes* (system-agnostic)."""
    code = resource.get("code")
    coding = code.get("coding", []) if isinstance(code, dict) else []
    return any(isinstance(c, dict) and c.get("code") in codes for c in coding)


def _value_range_node(res: dict, codes: set[str]) -> dict | None:
    """Pick the node a value_range rule applies to: the Observation itself or the
    matching ``component[]`` entry whose coding intersects *codes*.

    Component-scoped vitals are otherwise invisible to a top-level-only lookup
    e.g. the US-Core blood-pressure panel carries the panel code 85354-9 at the
    top level while systolic (8480-6) / diastolic (8462-4) live under
    ``component[].valueQuantity``. With no code filter the top-level Observation
    is used (component-less semantics, unchanged for plain value[x] vitals).
    """
    if not codes:
        return res
    candidates = [res]
    components = res.get("component")
    if isinstance(components, list):
        candidates.extend(c for c in components if isinstance(c, dict))
    for node in candidates:
        if _has_code(node, codes):
            return node
    return None


def evaluate(
    resources: list[dict],
    rules: list[PlausibilityRule],
    reference_time: str | None = None,
) -> list[CheckResult]:
    """Run every rule across the batch, returning one CheckResult per rule.

    ``reference_time`` is the single "now" anchor for time-relative rules
    (``not_in_future``), held constant for the whole assessment so there is no
    per-resource clock drift and the verdict is reproducible. The engine always
    supplies it as ``provenance.extraction_time`` when present (making the
    verdict fully deterministic, since the anchor is part of the input) or one
    assessment timestamp captured once per call (recorded in the evaluation for
    audit). When ``None`` (a direct/standalone caller) it falls back to the
    current wall-clock time.
    """
    birth = _birth_index(resources)
    death = _death_index(resources)
    results: list[CheckResult] = []

    for rule in rules:
        subcat, context = _KIND_TAXONOMY.get(rule.kind, ("atemporal", "verification"))
        targets = [
            r
            for r in resources
            if isinstance(r, dict) and r.get("resourceType") == rule.resource_type
        ]
        chk = CheckResult(
            check_id=rule.rule_id,
            category="plausibility",
            subcategory=subcat,
            context=context,
            threshold=rule.threshold,
            critical=rule.criticality == "blocker",
            description=rule.description,
            recommendation=rule.recommendation,
            resource_type=rule.resource_type,
        )
        for res in targets:
            ok, assessed = _eval_one(rule, res, birth, death, reference_time)
            if not assessed:
                continue
            chk.applicable += 1
            if not ok:
                chk.violations += 1
                chk.add_detail(
                    resource_type=rule.resource_type,
                    resource_id=res.get("id", ""),
                    path=rule.rule_id,
                    detail=rule.description or rule.rule_id,
                )
        results.append(chk)
    return results


def _patient_date(res: dict, index: dict[str, str]) -> str | None:
    """Resolve the subject Patient's indexed date (birth/death) for *res*.

    A Patient resource has no subject/patient reference  it *is* the subject
    so resolve against its own ``Patient/{id}`` key. Without this, patient-level
    temporal rules (e.g. death-after-birth) can never be assessed and always NA.
    """
    if res.get("resourceType") == "Patient" and res.get("id"):
        return index.get(f"Patient/{res['id']}")
    ref = _subject_ref(res)
    if not ref:
        return None
    return next((v for k, v in index.items() if ref.endswith(k)), None)


def _eval_one(
    rule: PlausibilityRule,
    res: dict,
    birth: dict[str, str],
    death: dict[str, str],
    reference_time: str | None = None,
) -> tuple[bool, bool]:
    """Evaluate one rule against one resource → (passed, assessed)."""
    p = rule.params
    kind = rule.kind

    if kind == "date_order":
        a, b = _first_date(res, p["after"]), _first_date(res, p["before"])
        if a is None or b is None:
            return True, False
        return (_compare_dates(a, b) >= 0), True

    if kind == "period_order":
        node = res.get(p.get("path", "period"))
        if not isinstance(node, dict):
            return True, False
        start, end = node.get("start"), node.get("end")
        if not (isinstance(start, str) and isinstance(end, str)):
            return True, False
        return (_compare_dates(start, end) <= 0), True

    if kind == "date_after_birth":
        date = _first_date_any(res, p["date"])
        if date is None:
            return True, False
        bd = _patient_date(res, birth)
        if bd is None:
            return True, False
        return (_compare_dates(date, bd) >= 0), True

    if kind == "date_before_death":
        # OHDSI plausibleBeforeDeath: a clinical event must not occur after the
        # subject's recorded death (only assessed for deceased patients).
        # Death-certificate / cause-of-death Observations are legitimately
        # recorded post-mortem  exempt them via params.exempt_codes.
        exempt = set(p.get("exempt_codes", []))
        if exempt and _has_code(res, exempt):
            return True, False
        date = _first_date_any(res, p["date"])
        if date is None:
            return True, False
        dd = _patient_date(res, death)
        if dd is None:
            return True, False  # patient not deceased / unknown → not applicable
        return (_compare_dates(date, dd) <= 0), True

    if kind == "not_in_future":
        # DAMA timeliness: a recorded date must not be in the future. "Future" is
        # only meaningful relative to a reference clock  anchor on the single
        # assessment-wide reference_time (provenance extraction_time, else one
        # captured assessment timestamp) so the verdict has no per-resource drift
        # and is fully reproducible when provenance is present. A direct caller
        # that passes no anchor falls back to wall-clock.
        date = _first_date_any(res, p["date"])
        if date is None:
            return True, False
        anchor = reference_time if reference_time is not None else _now_iso()
        return (_compare_dates(date, anchor) <= 0), True

    if kind == "value_range":
        codes = set(p.get("codes", []))
        node = _value_range_node(res, codes)
        if node is None:
            return (
                True,
                False,
            )  # rule's codes match neither the Observation nor a component
        vq = node.get("valueQuantity")
        if not isinstance(vq, dict) or not isinstance(vq.get("value"), (int, float)):
            return True, False
        # Unit check: an unexpected OR missing unit is itself an implausibility
        # a magnitude like 200 is meaningless without its unit (the classic
        # "weight in grams recorded against a kg range" error from the article).
        # A unit-filtered rule therefore FAILs a value whose unit is absent or
        # not in the allowed set, rather than silently range-checking a bare number.
        allowed_units = set(p.get("units", []))
        unit = vq.get("code") or vq.get("unit")
        if allowed_units and (not unit or unit not in allowed_units):
            return False, True
        val = float(vq["value"])
        lo, hi = p.get("min"), p.get("max")
        if lo is not None and val < float(lo):
            return False, True
        if hi is not None and val > float(hi):
            return False, True
        return True, True

    return True, False
