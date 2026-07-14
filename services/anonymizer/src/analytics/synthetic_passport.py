"""Synthetic Data Passport (TEHDAS2 D7.2 §5.4 / §5.5).

Synthetic data must be judged on **two** axes at once: fidelity (does it preserve
the statistical structure needed for the intended use?) and privacy (does it leak
real records?). This module produces a single passport combining:

- **fidelity**: per-column marginal similarity (1 - total-variation distance)
  between the real and synthetic distributions of the projected variables. This
  is in-house and dependency-free so it always runs; SDMetrics can be layered on
  later for richer (pairwise / detection) metrics.
- **privacy**: the DCR / NNDR / exact-duplicate / attribute-inference report from
  :func:`analytics.privacy_risk.assess_privacy_risk` (WS1).
- **differential_privacy**: a hook for DP generation parameters (epsilon/delta),
  populated when a DP synthesiser is used (phased later, torch-free via
  diffprivlib).

The passport carries a graded verdict so a data user can decide release vs
review vs reject. It is anonymous (distributions and scores only, no records).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# Dual-import shim: ``analytics.*`` in the anonymizer monolith; flat in the
# analytics microservice (build-copied).
try:  # pragma: no cover - import shim
    from analytics.privacy_risk import _project_rows, assess_privacy_risk
except ImportError:  # pragma: no cover - analytics microservice layout
    from privacy_risk import _project_rows, assess_privacy_risk

PASSPORT_VERSION = "1.0"

# Fidelity grade bands on mean per-column similarity in [0, 1].
_FIDELITY_BANDS = [(0.9, "A"), (0.8, "B"), (0.65, "C"), (0.5, "D"), (0.0, "F")]


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _column_fidelity(
    real_rows: list[dict], syn_rows: list[dict], keys: list[str]
) -> dict:
    """Per-column ``1 - TVD`` between real and synthetic marginal distributions."""
    per_column: dict[str, float] = {}
    for k in keys:
        real_counts = Counter(r.get(k, "") for r in real_rows)
        syn_counts = Counter(s.get(k, "") for s in syn_rows)
        rn = sum(real_counts.values())
        sn = sum(syn_counts.values())
        if rn == 0 or sn == 0:
            per_column[k] = 0.0
            continue
        categories = set(real_counts) | set(syn_counts)
        tvd = 0.5 * sum(
            abs(real_counts.get(c, 0) / rn - syn_counts.get(c, 0) / sn)
            for c in categories
        )
        per_column[k] = round(1.0 - tvd, 6)
    mean = round(sum(per_column.values()) / len(per_column), 6) if per_column else 0.0
    return {"per_column": per_column, "mean_fidelity": mean}


def _fidelity_grade(mean: float) -> str:
    for threshold, grade in _FIDELITY_BANDS:
        if mean >= threshold:
            return grade
    return "F"


def build_synthetic_passport(
    real: list[dict],
    synthetic: list[dict],
    *,
    privacy_model: dict | None = None,
    dp_params: dict | None = None,
) -> dict[str, Any]:
    """Assemble a Synthetic Data Passport for *synthetic* against *real*."""
    pm = privacy_model or {}
    qi_fields = pm.get("quasi_identifiers") or [
        {"path": "Patient.gender", "kind": "category"},
        {"path": "Patient.birthDate", "kind": "date"},
        {"path": "Patient.address.postalCode", "kind": "zip"},
    ]
    keys = [q["path"] for q in qi_fields]

    real_rows = _project_rows(real, qi_fields)
    syn_rows = _project_rows(synthetic, qi_fields)
    fidelity = _column_fidelity(real_rows, syn_rows, keys)
    fidelity["method"] = "1 - total_variation_distance (per-column marginal)"
    fidelity["grade"] = _fidelity_grade(fidelity["mean_fidelity"])

    privacy = assess_privacy_risk(real, synthetic=synthetic, privacy_model=pm)

    # Verdict: privacy is a hard gate (any exact duplicate = leak); fidelity is
    # graded. Overall = reject if privacy fails, else review below grade C.
    dist = privacy.get("distance") or {}
    privacy_pass = not (
        dist.get("computed") and int(dist.get("exact_duplicates", 0)) > 0
    )
    if not privacy_pass:
        overall = "reject"
    elif fidelity["mean_fidelity"] < 0.65:
        overall = "review"
    else:
        overall = "release"

    return {
        "passport_version": PASSPORT_VERSION,
        "generated_at": _now_iso(),
        "record_counts": {"real": len(real_rows), "synthetic": len(syn_rows)},
        "fidelity": fidelity,
        "privacy": privacy,
        "differential_privacy": dp_params,
        "verdict": {
            "privacy_pass": privacy_pass,
            "fidelity_grade": fidelity["grade"],
            "overall": overall,
        },
    }
