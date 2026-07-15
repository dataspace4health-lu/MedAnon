"""Privacy Risk Assessment engine (TEHDAS2 D7.2 §5.5.7).

Goes beyond the k-anonymity / l-diversity *re-identification* measurement in
:mod:`analytics.risk` to cover the inference-attack surface D7.2 §5.5.7 asks
for: **membership inference**, **attribute inference / disclosure**, and
record-level **distance metrics** (DCR / NNDR / τ-DCR). The guideline explicitly
lists these record-level distance measures as acceptable "lighter-weight
methods", and prefers inference-attack-oriented metrics over similarity-only
scores.

Two assessment modes
--------------------
- **De-identified dataset** (``synthetic`` omitted): re-identification only
  k-anonymity, l-diversity, uniqueness (delegated to :mod:`analytics.risk`).
- **Synthetic dataset** (``synthetic`` supplied against a ``real`` reference):
  the full surface  DCR/NNDR/τ-DCR (in-house, no heavy deps) plus attribute
  inference (CAP) via **SDMetrics** when installed.

Dependency posture
------------------
The in-house distance metrics use only the standard library (always available).
SDMetrics is used opportunistically and **degrades to NA** when absent
matching the fail-soft convention used across the codebase. Anonymeter is
intentionally *not* a dependency: it pins ``python <3.12`` and so cannot run on
the project runtime (3.12 Docker / 3.13 local); its singling-out / linkability
role is covered by the in-house DCR/NNDR + SDMetrics CAP.
"""

from __future__ import annotations

import math
from typing import Any

# Dual-import: ``analytics.risk`` in the anonymizer monolith; ``risk`` in the
# analytics microservice (build-copied flat, no package prefix  same pattern
# the other shared analytics modules rely on).
try:  # pragma: no cover - import shim
    from analytics.risk import _read_path, assess_risk_resources
except ImportError:  # pragma: no cover - analytics microservice layout
    from risk import _read_path, assess_risk_resources

# τ-DCR default similarity threshold (fraction of the max normalised distance)
# below which a synthetic record counts as "too close" to a real record.
_DEFAULT_TAU = 0.2


# ---------------------------------------------------------------------------
# Tabular projection
# ---------------------------------------------------------------------------


def _project_rows(resources: list[dict], fields: list[dict]) -> list[dict[str, str]]:
    """Project Patient resources onto flat ``{path: value}`` rows for the QIs."""
    rows: list[dict[str, str]] = []
    for r in resources:
        if r.get("resourceType") != "Patient":
            continue
        rows.append({f["path"]: _read_path(r, f["path"]) for f in fields})
    return rows


def _row_distance(a: dict[str, str], b: dict[str, str], keys: list[str]) -> float:
    """Normalised Hamming distance in [0, 1] between two projected rows.

    Deterministic and dependency-free; deliberately simple (D7.2 asks for
    record-level *closeness*, not a calibrated metric).
    """
    if not keys:
        return 1.0
    diff = sum(0.0 if a.get(k, "") == b.get(k, "") else 1.0 for k in keys)
    return diff / len(keys)


def _dcr_nndr(
    real: list[dict[str, str]],
    synthetic: list[dict[str, str]],
    keys: list[str],
    tau: float = _DEFAULT_TAU,
) -> dict[str, Any]:
    """Distance-to-Closest-Record, NNDR and τ-DCR share (in-house).

    For each synthetic row, find the two smallest distances to real rows
    ``d1`` (closest) and ``d2`` (second closest). DCR = mean d1; NNDR =
    mean d1/d2; τ-DCR share = fraction of synthetic rows with d1 ≤ tau
    (elevated membership/linkage risk).
    """
    if not real or not synthetic:
        return {"computed": False, "reason": "need both real and synthetic rows"}

    dcrs: list[float] = []
    nndrs: list[float] = []
    too_close = 0
    for s in synthetic:
        d1 = d2 = math.inf
        for r in real:
            d = _row_distance(s, r, keys)
            if d < d1:
                d2, d1 = d1, d
            elif d < d2:
                d2 = d
        dcrs.append(d1)
        if d2 > 0:
            nndrs.append(d1 / d2)
        if d1 <= tau:
            too_close += 1

    n = len(dcrs)
    return {
        "computed": True,
        "dcr_mean": round(sum(dcrs) / n, 6),
        "dcr_min": round(min(dcrs), 6),
        "nndr_mean": round((sum(nndrs) / len(nndrs)) if nndrs else 0.0, 6),
        "tau": tau,
        "tau_dcr_share": round(too_close / n, 6),
        "records_at_or_below_tau": too_close,
        "synthetic_records": n,
        # Any exact duplicate (d1 == 0) is a hard membership/leak signal.
        "exact_duplicates": sum(1 for d in dcrs if d == 0.0),
    }


# ---------------------------------------------------------------------------
# SDMetrics adapter (optional  degrades to NA when the lib is absent)
# ---------------------------------------------------------------------------


def _sdmetrics_privacy(
    real: list[dict[str, str]],
    synthetic: list[dict[str, str]],
    key_fields: list[str],
    sensitive_fields: list[str],
) -> dict[str, Any]:
    """Attribute-inference (Correct Attribution Probability) via SDMetrics."""
    if not sensitive_fields:
        return {"computed": False, "reason": "no sensitive_fields configured"}
    try:
        import pandas as pd
        from sdmetrics.single_table import DisclosureProtection
    except Exception as exc:  # noqa: BLE001  optional dependency
        return {"computed": False, "reason": f"sdmetrics unavailable: {exc}"}

    if not real or not synthetic:
        return {"computed": False, "reason": "need both real and synthetic rows"}

    real_df = pd.DataFrame(real)
    syn_df = pd.DataFrame(synthetic)
    out: dict[str, Any] = {"computed": True, "attribute_inference": {}}
    for sf in sensitive_fields:
        keys = [k for k in key_fields if k != sf]
        if not keys or sf not in real_df.columns:
            continue
        try:
            # DisclosureProtection (CAP-based, the non-deprecated wrapper):
            # higher score = better protection (lower correct-attribution).
            score = float(
                DisclosureProtection.compute(
                    real_data=real_df,
                    synthetic_data=syn_df,
                    known_column_names=keys,
                    sensitive_column_names=[sf],
                )
            )
            # NaN when the attacker cannot form a prediction (too few
            # overlapping key combinations); surface as NA, not a non-JSON float.
            out["attribute_inference"][sf] = (
                {"na": "insufficient overlapping key combinations"}
                if math.isnan(score)
                else round(score, 6)
            )
        except Exception as exc:  # noqa: BLE001  metric may reject sparse data
            out["attribute_inference"][sf] = {"error": str(exc)}
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def assess_privacy_risk(
    resources: list[dict] | None = None,
    *,
    rows: list[dict] | None = None,
    synthetic: list[dict] | None = None,
    privacy_model: dict | None = None,
) -> dict[str, Any]:
    """Full privacy-risk report for a de-identified or synthetic dataset.

    Args:
        resources: the released/real dataset (FHIR resource dicts).
        rows: alternative flat **tabular** input (one subject per dict) for
              SQL/CSV/CDA/HL7v2 outputs that aren't FHIR Patient resources
              (D7.2 §2.1.1). When supplied, re-identification is computed via
              :func:`analytics.risk.assess_risk_rows` using ``quasi_identifiers``
              (column names) + ``sensitive_attribute`` from *privacy_model*.
        synthetic: optional synthetic dataset to assess against *resources*.
        privacy_model: carries ``quasi_identifiers`` (key fields),
                       ``sensitive_attributes`` (paths for CAP), and ``tau``.

    Returns a report with keys: ``re_identification``, ``distance``,
    ``attribute_inference``, ``meta``.
    """
    pm = privacy_model or {}

    # Tabular path (D7.2 §2.1.1): flat rows, QI = column names.
    if rows is not None:
        try:
            from analytics.risk import assess_risk_rows
        except ImportError:  # pragma: no cover - microservice layout
            from risk import assess_risk_rows

        qi_cols = [
            q["path"] if isinstance(q, dict) else str(q)
            for q in (pm.get("quasi_identifiers") or [])
        ]
        reid = assess_risk_rows(
            rows,
            quasi_identifiers=qi_cols,
            sensitive_attribute=pm.get("sensitive_attribute"),
            thresholds=pm.get("risk_thresholds"),
        )
        return {
            "re_identification": reid,
            "distance": {"computed": False, "reason": "tabular re-identification only"},
            "attribute_inference": {
                "computed": False,
                "reason": "tabular re-identification only",
            },
            "meta": {
                "mode": "tabular",
                "quasi_identifiers": qi_cols,
                "sensitive_attribute": pm.get("sensitive_attribute"),
            },
        }

    resources = resources or []
    qi_fields = pm.get("quasi_identifiers") or [
        {"path": "Patient.gender", "kind": "category"},
        {"path": "Patient.birthDate", "kind": "date"},
        {"path": "Patient.address.postalCode", "kind": "zip"},
    ]
    key_labels = [q["path"] for q in qi_fields]
    sensitive_fields = pm.get("sensitive_attributes") or []
    tau = float(pm.get("tau", _DEFAULT_TAU))

    report: dict[str, Any] = {
        "re_identification": assess_risk_resources(resources, privacy_model=pm),
        "distance": {"computed": False, "reason": "no synthetic dataset supplied"},
        "attribute_inference": {
            "computed": False,
            "reason": "no synthetic dataset supplied",
        },
        "meta": {
            "mode": "synthetic" if synthetic else "deidentified",
            "quasi_identifiers": key_labels,
            "sensitive_attributes": sensitive_fields,
        },
    }

    if synthetic:
        # Project the union of QI *and* sensitive fields so attribute-inference
        # (CAP) can see the sensitive column; DCR/NNDR still key only on the QIs.
        proj_fields = qi_fields + [
            {"path": s, "kind": "category"}
            for s in sensitive_fields
            if s not in key_labels
        ]
        real_rows = _project_rows(resources, proj_fields)
        syn_rows = _project_rows(synthetic, proj_fields)
        report["distance"] = _dcr_nndr(real_rows, syn_rows, key_labels, tau=tau)
        report["attribute_inference"] = _sdmetrics_privacy(
            real_rows, syn_rows, key_labels, sensitive_fields
        )

    return report
