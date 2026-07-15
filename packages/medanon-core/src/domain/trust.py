"""Trust Gate vocabulary and built-in profiles (domain contract).

The audit-phase / use-case vocabulary and the built-in trust profiles are pure
domain data, depended on downward by both the pipeline and the postgres trust
profile adapter (rather than the adapter reaching up into pipeline). The SQLite
store and store-selection live in ``pipeline.trust_profile``.
"""

from __future__ import annotations

# Canonical audit-phase ids. KEEP IN SYNC with services/trust-gate/src/phases.py
# ALL_PHASES (the two services have no shared package yet). The Trust Gate defends
# against drift (normalize_selection drops unknown phase ids), but this list is
# what the API validates new profiles against, so update both together.
PHASE_IDS: tuple[str, ...] = (
    "structural_conformance",
    "terminology_validity",
    "referential_integrity",
    "completeness_core",
    "completeness_richness",
    "value_plausibility",
    "temporal_plausibility",
    "identity_integrity",
    "provenance_auditability",
    "timeliness",
    "source_accuracy",
)

# Canonical use-case ids. KEEP IN SYNC with the keys under ``use_cases:`` in
# services/trust-gate/config/use_case_profiles.yaml. A use case is a declared
# purpose the Trust Gate resolves server-side to a metric/phase subset
# (decision-tree selection); explicit ``phases`` still win. Empty string means
# "no declared use case" (the service runs all phases, unchanged default).
USE_CASE_IDS: tuple[str, ...] = (
    "ai_training",
    "cohort_discovery",
    "regulatory_rwe",
    "lab_analytics",
    "exchange_interoperability",
)

# Built-in starter profiles (is_system=True -> read-only). Illustrative, sensible
# defaults the operator can clone and tune; "full-audit" mirrors today's
# run-everything behaviour.
_SYSTEM_PROFILES: list[dict] = [
    {
        "name": "full-audit",
        "description": "Every quality phase (equivalent to the default unscoped pass).",
        "phases": list(PHASE_IDS),
        "intended_use": "secondary use (general)",
    },
    {
        "name": "structural-only",
        "description": "FHIR structural validity only - the fast pre-flight gate.",
        "phases": ["structural_conformance"],
        "intended_use": "ingestion pre-flight",
    },
    {
        "name": "lab-strict",
        "description": "Lab data: structure + terminology + value plausibility + core completeness.",
        "phases": [
            "structural_conformance",
            "terminology_validity",
            "value_plausibility",
            "completeness_core",
        ],
        "intended_use": "lab analytics / research",
        "use_case": "lab_analytics",
    },
    {
        "name": "demographics",
        "description": "Patient demographics: structure + core completeness + identity integrity.",
        "phases": [
            "structural_conformance",
            "completeness_core",
            "identity_integrity",
        ],
        "intended_use": "cohort identification",
        "use_case": "cohort_discovery",
    },
    {
        "name": "research-export",
        "description": "Research-export readiness: structure + references + temporal plausibility + provenance.",
        "phases": [
            "structural_conformance",
            "referential_integrity",
            "temporal_plausibility",
            "provenance_auditability",
        ],
        "intended_use": "research data export",
        "use_case": "regulatory_rwe",
    },
]


def validate_phases(phases: list[str]) -> list[str]:
    """Return the list of invalid phase ids (empty when all are valid)."""
    return [p for p in phases if p not in PHASE_IDS]


def validate_use_case(use_case: str | None) -> bool:
    """True when ``use_case`` is empty/None or a known id, else False."""
    return not use_case or use_case in USE_CASE_IDS
