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
    # ── Demographic / administrative resources ──────────────────────────────
    "Patient": [
        "name",  # (1) names
        "telecom",  # (4)(5)(6) phone, fax, email
        "address",  # (2) geographic subdivisions
        "identifier",  # (7)(8)(18) SSN, MRN, other unique IDs
        "birthDate",  # (3) dates directly related to individual
        "deceasedDateTime",  # (3) dates
        "deceasedBoolean",  # indirect — reveals death status
        "photo",  # (17) full-face photographic images
        "contact.name",  # (1) emergency contact names
        "contact.telecom",  # (4)(5)(6)
        "contact.address",  # (2)
        "generalPractitioner",  # (18) indirect identifier — unique provider link
        "managingOrganization",  # (18) indirect narrowing identifier
        "link",  # (18) links to other patient records
    ],
    "Practitioner": [
        "name",  # (1)
        "telecom",  # (4)(5)(6)
        "address",  # (2)
        "identifier",  # (8)(11)(18) NPI, DEA, state license
        "birthDate",  # (3)
        "photo",  # (17)
        "qualification.identifier",  # (11) certificate / license numbers
    ],
    "RelatedPerson": [
        "name",  # (1)
        "telecom",  # (4)(5)(6)
        "address",  # (2)
        "identifier",  # (18)
        "birthDate",  # (3)
        "photo",  # (17)
        "period",  # (3) relationship active dates
    ],
    "PractitionerRole": [
        "identifier",  # (18)
        "period",  # (3)
        "telecom",  # (4)(5)(6)
    ],
    "Organization": [
        "name",  # (1)
        "telecom",  # (4)(5)(6)
        "address",  # (2)
        "identifier",  # (18)
    ],
    "Location": [
        "name",  # (1) facility names narrow populations
        "address",  # (2)
        "telecom",  # (4)(5)(6)
        "position",  # (2) GPS lat/lon
        "identifier",  # (18)
    ],
    "Device": [
        "identifier",  # (13)(18) device IDs
        "udiCarrier",  # (13) UDI — unique device identifier
        "serialNumber",  # (13)
        "lotNumber",  # (13)
        "distinctIdentifier",  # (13)
        "manufactureDate",  # (3)
        "expirationDate",  # (3)
        "patient",  # (18) links device to patient
    ],
    "Coverage": [
        "identifier",  # (9)(18) health-plan beneficiary numbers
        "subscriber",  # (9)(18)
        "subscriberId",  # (9)
        "beneficiary",  # (18)
        "period",  # (3)
        "payor",  # (18)
    ],
    "Account": [
        "identifier",  # (10) account numbers
        "name",  # (1)
        "subject",  # (18)
        "servicePeriod",  # (3)
    ],
    "Endpoint": [
        "address",  # (14) URLs / web addresses
        "identifier",  # (18)
        "name",  # (1)
    ],
    # ── Clinical encounter / activity resources ──────────────────────────────
    # All carry PHI under HIPAA category (3) dates and (18) unique identifiers.
    "Encounter": [
        "identifier",  # (18)
        "period",  # (3) admission + discharge dates — critical
        "participant.period",  # (3) individual participation windows
        "location.period",  # (3)
        "hospitalization",  # (3) admit/discharge source + disposition
        "subject",  # (18) patient reference
    ],
    "Condition": [
        "identifier",  # (18)
        "recordedDate",  # (3)
        "onsetDateTime",  # (3)
        "onsetPeriod",  # (3)
        "abatementDateTime",  # (3)
        "abatementPeriod",  # (3)
        "recorder",  # (18)
        "asserter",  # (18)
        "subject",  # (18)
    ],
    "Procedure": [
        "identifier",  # (18)
        "performedDateTime",  # (3)
        "performedPeriod",  # (3)
        "performer",  # (18)
        "recorder",  # (18)
        "asserter",  # (18)
        "subject",  # (18)
    ],
    "Observation": [
        "identifier",  # (18)
        "effectiveDateTime",  # (3)
        "effectivePeriod",  # (3)
        "issued",  # (3)
        "performer",  # (18)
        "valueString",  # free text — may contain residual PHI
        "subject",  # (18)
    ],
    "DiagnosticReport": [
        "identifier",  # (18)
        "effectiveDateTime",  # (3)
        "effectivePeriod",  # (3)
        "issued",  # (3)
        "performer",  # (18)
        "resultsInterpreter",  # (18)
        "subject",  # (18)
    ],
    "MedicationRequest": [
        "identifier",  # (18)
        "authoredOn",  # (3)
        "requester",  # (18)
        "subject",  # (18)
    ],
    "MedicationAdministration": [
        "identifier",  # (18)
        "effectiveDateTime",  # (3)
        "effectivePeriod",  # (3)
        "performer",  # (18)
        "subject",  # (18)
    ],
    "MedicationDispense": [
        "identifier",  # (18)
        "whenPrepared",  # (3)
        "whenHandedOver",  # (3)
        "performer",  # (18)
        "subject",  # (18)
    ],
    "AllergyIntolerance": [
        "identifier",  # (18)
        "recordedDate",  # (3)
        "recorder",  # (18)
        "asserter",  # (18)
        "patient",  # (18)
    ],
    "Immunization": [
        "identifier",  # (18)
        "occurrenceDateTime",  # (3)
        "occurrenceString",  # (3) date expressed as string
        "performer",  # (18)
        "lotNumber",  # (13) device/lot identifier
        "patient",  # (18)
    ],
    "DocumentReference": [
        "identifier",  # (18)
        "date",  # (3)
        "author",  # (18)
        "authenticator",  # (18)
        "custodian",  # (18)
        "subject",  # (18)
    ],
    "ServiceRequest": [
        "identifier",  # (18)
        "authoredOn",  # (3)
        "requester",  # (18)
        "performer",  # (18)
        "subject",  # (18)
    ],
    "ImagingStudy": [
        "identifier",  # (18)
        "started",  # (3)
        "subject",  # (18)
        "series",  # contains uid (18) and started (3) per series
    ],
    "CarePlan": [
        "identifier",  # (18)
        "period",  # (3)
        "author",  # (18)
        "contributor",  # (18)
        "subject",  # (18)
    ],
    "CareTeam": [
        "identifier",  # (18)
        "period",  # (3)
        "participant.member",  # (18)
        "participant.period",  # (3)
        "subject",  # (18)
    ],
    "Appointment": [
        "identifier",  # (18)
        "start",  # (3)
        "end",  # (3)
        "participant",  # (18) actor references
    ],
    "EpisodeOfCare": [
        "identifier",  # (18)
        "period",  # (3)
        "patient",  # (18)
        "careManager",  # (18)
    ],
    "FamilyMemberHistory": [
        "identifier",  # (18)
        "date",  # (3)
        "name",  # (1) family member's own name
        "bornDate",  # (3)
        "bornPeriod",  # (3)
        "deceasedDate",  # (3)
        "patient",  # (18)
    ],
    "Composition": [
        "identifier",  # (18)
        "date",  # (3)
        "author",  # (18)
        "attester",  # (18)
        "subject",  # (18)
    ],
    "RiskAssessment": [
        "identifier",  # (18)
        "occurrenceDateTime",  # (3)
        "occurrencePeriod",  # (3)
        "performer",  # (18)
        "subject",  # (18)
    ],
    "AuditEvent": [
        "recorded",  # (3)
        "agent.name",  # (1)
        "agent.who",  # (18)
        "agent.network",  # (15) IP address of agent
    ],
    "Claim": [
        "identifier",  # (9)(10)(18)
        "created",  # (3)
        "enterer",  # (18)
        "provider",  # (18)
        "patient",  # (18)
        "billablePeriod",  # (3)
        "careTeam",  # (18)
    ],
    "ExplanationOfBenefit": [
        "identifier",  # (9)(10)(18)
        "created",  # (3)
        "enterer",  # (18)
        "provider",  # (18)
        "patient",  # (18)
        "billablePeriod",  # (3)
        "careTeam",  # (18)
    ],
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

CLINICAL_CODE_SYSTEMS: frozenset[str] = frozenset(
    {
        "http://loinc.org",
        "http://snomed.info/sct",
        "http://hl7.org/fhir/sid/icd-10",
        "http://hl7.org/fhir/sid/icd-10-cm",
        "http://www.nlm.nih.gov/research/umls/rxnorm",
        "http://unitsofmeasure.org",
        "http://hl7.org/fhir/observation-category",
        "http://terminology.hl7.org/CodeSystem/v3-ActCode",
        "http://hl7.org/fhir/ValueSet/observation-category",
    }
)

# ---------------------------------------------------------------------------
# Information loss weights per action (higher = more loss / less utility)
# ---------------------------------------------------------------------------

INFO_LOSS_WEIGHTS: dict[str, float] = {
    "redact": 1.0,
    "scrub_text": 0.6,
    "nlp_scrub": 0.6,
    "nlp_detect": 0.6,  # backward-compat for old manifests
    "nlp_detect_act": 0.3,
    "nlp_detect_act/clean": 0.0,
    "nlp_detect_act/redact": 0.6,
    "nlp_detect_act/generalize": 0.35,
    "nlp_detect_act/tokenize": 0.5,
    "generalize": 0.35,
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

REDACTED_SENTINELS: frozenset[str] = frozenset(
    {
        "",
        "[REDACTED]",
        "[redacted]",
        "REDACTED",
        "redacted",
        "unknown",
        "UNKNOWN",
        "other",
        "OTHER",
    }
)

# ---------------------------------------------------------------------------
# Date field names checked in temporal consistency
# ---------------------------------------------------------------------------

DATE_FIELDS: tuple[str, ...] = (
    "effectiveDateTime",
    "issued",
    "authoredOn",
    "recordedDate",
    "onsetDateTime",
    "abatementDateTime",
    "occurrenceDateTime",
    "birthDate",
    "deceasedDateTime",
)

PERIOD_FIELDS: tuple[str, ...] = ("period", "effectivePeriod", "performedPeriod")

# ---------------------------------------------------------------------------
# Configurable thresholds (env-driven)
# ---------------------------------------------------------------------------

RISK_THRESHOLD: float = float(os.environ.get("MEDANON_SCORE_RISK_THRESHOLD", "0.3"))

NER_ENABLED: bool = os.environ.get(
    "MEDANON_SCORE_NER_ENABLED", "true"
).strip().lower() in ("1", "true", "yes")

NER_THRESHOLD: float = float(os.environ.get("MEDANON_SCORE_NER_THRESHOLD", "0.5"))

SCORING_ENABLED: bool = os.environ.get(
    "MEDANON_SCORING_ENABLED", "false"
).strip().lower() in ("1", "true", "yes")

# When true, config_identifier_risk is included in the privacy risk_score max
# and can cause a FAIL gate. When false (default), it is informational only.
SCORE_CONFIG_GATE: bool = os.environ.get(
    "MEDANON_SCORE_CONFIG_GATE", "false"
).strip().lower() in ("1", "true", "yes")

SCORE_ATTACH: bool = os.environ.get(
    "MEDANON_SCORE_ATTACH", "false"
).strip().lower() in ("1", "true", "yes")
