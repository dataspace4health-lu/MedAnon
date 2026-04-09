"""Scoring engine constants and thresholds.

HIPAA Safe Harbor 18 identifier categories mapped to FHIR resource paths,
clinical code system URIs, information loss weights per action, and
configurable thresholds.
"""
from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# HIPAA Safe Harbor 18-identifier field map (resource type → sensitive paths)
# ---------------------------------------------------------------------------

HIPAA_SENSITIVE_PATHS: dict[str, list[str]] = {
    "Patient": [
        "name", "telecom", "address", "identifier", "birthDate",
        "photo", "contact.name", "contact.telecom", "contact.address",
        "deceasedDateTime", "link", "communication",
    ],
    "Practitioner": [
        "name", "telecom", "address", "identifier", "birthDate", "photo",
    ],
    "RelatedPerson": [
        "name", "telecom", "address", "identifier", "birthDate", "photo",
    ],
    "Organization": ["name", "telecom", "address", "identifier"],
    "Location": ["name", "address", "telecom", "position"],
    "Device": ["identifier", "serialNumber", "udiCarrier"],
    "Coverage": ["identifier", "subscriber", "beneficiary"],
    "Account": ["identifier"],
    "Endpoint": ["address"],
    # Wildcard paths apply to ALL resource types
    "*": ["id", "text"],
}

# Resource types that carry PHI (have entries in HIPAA_SENSITIVE_PATHS
# other than the wildcard).
PHI_RESOURCE_TYPES: frozenset[str] = frozenset(
    k for k in HIPAA_SENSITIVE_PATHS if k != "*"
)

# ---------------------------------------------------------------------------
# Clinical code system URIs (for semantic preservation checks)
# ---------------------------------------------------------------------------

CLINICAL_CODE_SYSTEMS: frozenset[str] = frozenset({
    "http://loinc.org",
    "http://snomed.info/sct",
    "http://hl7.org/fhir/sid/icd-10",
    "http://hl7.org/fhir/sid/icd-10-cm",
    "http://www.nlm.nih.gov/research/umls/rxnorm",
    "http://unitsofmeasure.org",
    "http://hl7.org/fhir/observation-category",
    "http://terminology.hl7.org/CodeSystem/v3-ActCode",
    "http://hl7.org/fhir/ValueSet/observation-category",
})

# ---------------------------------------------------------------------------
# Information loss weights per action (higher = more loss / less utility)
# ---------------------------------------------------------------------------

INFO_LOSS_WEIGHTS: dict[str, float] = {
    "redact": 1.0,
    "scrub_text": 0.6,
    "nlp_detect": 0.6,
    "generalize": 0.5,
    "substitute": 0.4,
    "perturb": 0.3,
    "cryptohash": 0.1,
    "gpas_pseudonymize": 0.1,
    "encrypt": 0.0,
}

# ---------------------------------------------------------------------------
# Attacker risk level → normalized risk score mapping
# ---------------------------------------------------------------------------

RISK_LEVEL_MAP: dict[str, float] = {
    "low": 0.05,
    "medium": 0.20,
    "high": 0.45,
    "critical": 0.80,
}

# ---------------------------------------------------------------------------
# Sentinels for redacted / suppressed values
# ---------------------------------------------------------------------------

REDACTED_SENTINELS: frozenset[str] = frozenset({
    "", "[REDACTED]", "[redacted]", "REDACTED", "redacted",
    "unknown", "UNKNOWN", "other", "OTHER",
})

# ---------------------------------------------------------------------------
# Date field names checked in temporal consistency
# ---------------------------------------------------------------------------

DATE_FIELDS: tuple[str, ...] = (
    "effectiveDateTime", "issued", "authoredOn", "recordedDate",
    "onsetDateTime", "abatementDateTime", "occurrenceDateTime",
    "birthDate", "deceasedDateTime",
)

PERIOD_FIELDS: tuple[str, ...] = ("period", "effectivePeriod", "performedPeriod")

# ---------------------------------------------------------------------------
# Configurable thresholds (env-driven)
# ---------------------------------------------------------------------------

RISK_THRESHOLD: float = float(
    os.environ.get("MEDANON_SCORE_RISK_THRESHOLD", "0.3")
)

NER_ENABLED: bool = os.environ.get(
    "MEDANON_SCORE_NER_ENABLED", "true"
).strip().lower() in ("1", "true", "yes")

NER_THRESHOLD: float = float(
    os.environ.get("MEDANON_SCORE_NER_THRESHOLD", "0.5")
)

SCORING_ENABLED: bool = os.environ.get(
    "MEDANON_SCORING_ENABLED", "false"
).strip().lower() in ("1", "true", "yes")

SCORE_ATTACH: bool = os.environ.get(
    "MEDANON_SCORE_ATTACH", "false"
).strip().lower() in ("1", "true", "yes")
