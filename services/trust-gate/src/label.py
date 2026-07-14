"""EHDS-style dataset label (Phase 6.1): quality + utility + maturity.

EHDS Regulation Art. 56 mandates a quality-and-utility label for datasets offered
for secondary use. This derives a compact, FAIR-aligned label from a passport:

  - quality   — correctness verdict (grade + decision).
  - utility    — fitness-for-research proxy from completeness/richness.
  - maturity   — CMM-style level from measurement coverage + provenance +
                 reproducibility.
  - fair       — interoperable / reusable signals.

Derived purely from an already-computed passport dict, so it adds no new scoring
and stays drift-free.
"""

from __future__ import annotations

LABEL_SCHEME = "EHDS Art.56 quality+utility+maturity"

# CMM maturity ceiling implied by how much was actually measured. A structural-only
# verdict is, by definition, not a comprehensive measurement, so it cannot claim a
# high maturity level no matter how clean the structural checks were — otherwise
# the EHDS label would over-state assurance (the whole point of the coverage signal).
_DEPTH_MATURITY_CAP: dict[str, int] = {"structural_only": 2, "partial": 4}


def _tier(score: float | None) -> str:
    if score is None:
        return "unknown"
    if score >= 85:
        return "high"
    if score >= 70:
        return "moderate"
    return "low"


def _maturity(passport: dict) -> tuple[int, list[str]]:
    """CMM-style maturity 1-5 from what was actually measured."""
    evaluation = passport.get("evaluation") or {}
    auditability = passport.get("auditability") or {}
    phases = passport.get("phases") or {}
    basis: list[str] = []
    level = 1
    if len([p for p in phases.values() if isinstance(p, dict)]) >= 8:
        level += 1
        basis.append("broad phase coverage")
    if auditability.get("provenance_present"):
        level += 1
        basis.append("provenance present")
    if evaluation.get("decision_basis") == "deterministic":
        level += 1
        basis.append("reproducible verdict")
    if passport.get("overall_grade") in ("A", "B"):
        level += 1
        basis.append("high quality grade")
    level = min(level, 5)
    # Cap by assessment coverage so a thin (structural-only / partial) verdict
    # cannot present as a mature, comprehensively-measured dataset.
    coverage = passport.get("coverage") or {}
    depth = coverage.get("validation_depth")
    cap = _DEPTH_MATURITY_CAP.get(depth)
    if cap is not None and level > cap:
        level = cap
        basis.append(f"capped by {depth} coverage")
    return level, basis


# EHDS data-quality characteristics (Reg. (EU) 2025/327 Art. 56 + Annex) mapped
# from the DAMA/ISO scorecard dimensions the gate already computes.
_EHDS_DQ_DIMENSIONS = (
    "completeness",
    "accuracy",
    "consistency",
    "currency",
    "uniqueness",
    "conformity",
    "integrity",
)


def _ehds(passport: dict) -> dict:
    """Formalize the EHDS Art. 56 data-quality-and-utility label elements.

    Derived purely from the passport (no new scoring): data source, data quality
    (per-characteristic grades + overall verdict), data coverage (records, types,
    time period, population), technical quality, provenance, and how much of the
    suite was actually exercised. EHDS requires the label be clear and honest, so
    the assessment-coverage block travels with it.
    """
    prof = passport.get("profile") or {}
    scorecard = passport.get("scorecard") or {}
    provenance = passport.get("provenance") or {}
    auditability = passport.get("auditability") or {}
    evaluation = passport.get("evaluation") or {}
    coverage = passport.get("coverage") or {}
    source_types = passport.get("source_types") or []

    dimensions = {
        dim: {
            "grade": (scorecard.get(dim) or {}).get("grade"),
            "score": (scorecard.get(dim) or {}).get("score"),
        }
        for dim in _EHDS_DQ_DIMENSIONS
        if dim in scorecard
    }
    return {
        "regulation": "EHDS Reg. (EU) 2025/327 Art. 56 data quality & utility label",
        "data_source": {
            "dataset_id": passport.get("dataset_id"),
            "source_system": provenance.get("source_system"),
            "source_types": source_types,
        },
        "data_quality": {
            "overall_grade": passport.get("overall_grade"),
            "overall_score": passport.get("overall_score"),
            "decision": passport.get("decision"),
            "dimensions": dimensions,
        },
        "data_coverage": {
            "record_count": passport.get("resource_count"),
            "resource_types": prof.get("resource_counts") or {},
            "time_period": prof.get("time_period"),
            "population": prof.get("patient_gender_distribution") or {},
        },
        "technical_quality": {
            "format": "HL7 FHIR R4" if "omop" not in source_types else "OMOP CDM",
            "interoperable": bool(evaluation.get("terminology_used"))
            or "omop" in source_types,
            "reference_density": prof.get("reference_density"),
        },
        "provenance": {
            "present": auditability.get("provenance_present", bool(provenance)),
            "extraction_time": provenance.get("extraction_time"),
            "lifecycle_stage": evaluation.get("lifecycle_stage"),
            "org_role": evaluation.get("org_role"),
        },
        "assessment_coverage": {
            "validation_depth": coverage.get("validation_depth"),
            "checks_assessed": coverage.get("checks_assessed"),
            "checks_total": coverage.get("checks_total"),
            "not_exercised": coverage.get("not_exercised", []),
        },
    }


def build_label(passport: dict) -> dict:
    """Derive the EHDS quality+utility+maturity label from a passport dict."""
    scorecard = passport.get("scorecard") or {}
    evaluation = passport.get("evaluation") or {}
    auditability = passport.get("auditability") or {}
    source_types = passport.get("source_types") or []

    completeness = (scorecard.get("completeness") or {}).get("score")
    utility_score = (
        completeness if completeness is not None else passport.get("overall_score")
    )
    level, basis = _maturity(passport)
    coverage = passport.get("coverage") or {}

    return {
        "scheme": LABEL_SCHEME,
        "quality": {
            "grade": passport.get("overall_grade"),
            "decision": passport.get("decision"),
            "score": passport.get("overall_score"),
        },
        # Assessment coverage made first-class on the label so a consumer sees how
        # much was measured, not only the grade (EHDS transparency).
        "coverage": {
            "depth": coverage.get("validation_depth"),
            "checks_assessed": coverage.get("checks_assessed"),
            "checks_total": coverage.get("checks_total"),
            "not_exercised": coverage.get("not_exercised", []),
        },
        "utility": {
            "score": round(utility_score, 1) if utility_score is not None else None,
            "tier": _tier(utility_score),
        },
        "maturity": {"level": level, "basis": basis},
        "fair": {
            "interoperable": bool(evaluation.get("terminology_used"))
            or "omop" in source_types,
            "reusable": bool(auditability.get("provenance_present")),
        },
        # Formalized EHDS Art. 56 quality-and-utility elements (EU compliance).
        "ehds": _ehds(passport),
    }
