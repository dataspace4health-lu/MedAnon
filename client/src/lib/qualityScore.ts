// ---------------------------------------------------------------------------
// De-identification Quality Score
// ---------------------------------------------------------------------------
//
// Computes a letter grade (A–F) and percentage for how effectively PII was
// removed from a de-identified FHIR dataset.  Purely client-side, consumes
// the PiiDetectionMap and FieldSummaryMap already produced by piiDetection.ts.
// ---------------------------------------------------------------------------

import type { PiiDetectionMap, FieldSummaryMap } from "./piiDetection";

// ---- Types ----------------------------------------------------------------

export type LetterGrade = "A" | "B" | "C" | "D" | "F";

export interface QualityScore {
  /** 0–100 integer percentage */
  percentage: number;
  grade: LetterGrade;
  /** Fraction of known-sensitive fields that were treated (0.0–1.0) */
  coverage: number;
  /** Average action strength across all treated fields (0.0–1.0) */
  strength: number;
  /** Fraction of resources processed without errors (0.0–1.0) */
  errorRate: number;
}

export interface GradeStyle {
  bg: string;
  text: string;
  border: string;
}

// ---- Reference data -------------------------------------------------------

/**
 * FHIR fields per resource type that carry PII (HIPAA Safe Harbor 18
 * identifiers cross-referenced with FHIR R4 paths).  The "*" key lists
 * fields that apply to every resource type.
 */
const KNOWN_SENSITIVE: Record<string, string[]> = {
  // Demographic / administrative
  Patient: [
    "name", "telecom", "address", "identifier", "birthDate",
    "deceasedDateTime", "deceasedBoolean", "photo",
    "contact.name", "contact.telecom", "contact.address",
    "generalPractitioner", "managingOrganization", "link",
  ],
  Practitioner: [
    "name", "telecom", "address", "identifier", "birthDate", "photo",
    "qualification.identifier",
  ],
  RelatedPerson: [
    "name", "telecom", "address", "identifier", "birthDate", "photo", "period",
  ],
  PractitionerRole: ["identifier", "period", "telecom"],
  Organization: ["name", "telecom", "address", "identifier"],
  Location: ["name", "address", "telecom", "position", "identifier"],
  Device: [
    "identifier", "udiCarrier", "serialNumber", "lotNumber",
    "distinctIdentifier", "manufactureDate", "expirationDate", "patient",
  ],
  Coverage: ["identifier", "subscriber", "subscriberId", "beneficiary", "period", "payor"],
  Account: ["identifier", "name", "subject", "servicePeriod"],
  Endpoint: ["address", "identifier", "name"],
  // Clinical encounter / activity
  Encounter: ["identifier", "period", "participant.period", "location.period", "hospitalization", "subject"],
  Condition: ["identifier", "recordedDate", "onsetDateTime", "onsetPeriod", "abatementDateTime", "abatementPeriod", "recorder", "asserter", "subject"],
  Procedure: ["identifier", "performedDateTime", "performedPeriod", "performer", "recorder", "asserter", "subject"],
  Observation: ["identifier", "effectiveDateTime", "effectivePeriod", "issued", "performer", "valueString", "subject"],
  DiagnosticReport: ["identifier", "effectiveDateTime", "effectivePeriod", "issued", "performer", "resultsInterpreter", "subject"],
  MedicationRequest: ["identifier", "authoredOn", "requester", "subject"],
  MedicationAdministration: ["identifier", "effectiveDateTime", "effectivePeriod", "performer", "subject"],
  MedicationDispense: ["identifier", "whenPrepared", "whenHandedOver", "performer", "subject"],
  AllergyIntolerance: ["identifier", "recordedDate", "recorder", "asserter", "patient"],
  Immunization: ["identifier", "occurrenceDateTime", "occurrenceString", "performer", "lotNumber", "patient"],
  DocumentReference: ["identifier", "date", "author", "authenticator", "custodian", "subject"],
  ServiceRequest: ["identifier", "authoredOn", "requester", "performer", "subject"],
  ImagingStudy: ["identifier", "started", "subject", "series"],
  CarePlan: ["identifier", "period", "author", "contributor", "subject"],
  CareTeam: ["identifier", "period", "participant.member", "participant.period", "subject"],
  Appointment: ["identifier", "start", "end", "participant"],
  EpisodeOfCare: ["identifier", "period", "patient", "careManager"],
  FamilyMemberHistory: ["identifier", "date", "name", "bornDate", "bornPeriod", "deceasedDate", "patient"],
  Composition: ["identifier", "date", "author", "attester", "subject"],
  RiskAssessment: ["identifier", "occurrenceDateTime", "occurrencePeriod", "performer", "subject"],
  AuditEvent: ["recorded", "agent.name", "agent.who", "agent.network"],
  Claim: ["identifier", "created", "enterer", "provider", "patient", "billablePeriod", "careTeam"],
  ExplanationOfBenefit: ["identifier", "created", "enterer", "provider", "patient", "billablePeriod", "careTeam"],
  "*": ["id", "text"],
};

/**
 * Action strength weights, derived from (1 − INFO_LOSS_WEIGHTS) in the backend
 * scoring engine (services/anonymizer/src/pipeline/scoring/constants.py).
 * Higher value = stronger de-identification for the quality score.
 */
const ACTION_STRENGTH: Record<string, number> = {
  redact: 1.0,
  cryptohash: 0.9,
  gpas_pseudonymize: 0.9,
  encrypt: 1.0,
  generalize: 0.65,
  substitute: 0.6,
  perturb: 0.7,
  scrub_text: 0.4,
  nlp_scrub: 0.4,
  nlp_detect: 0.4,           // backward-compat alias for old manifests
  nlp_detect_act: 0.7,       // conditional, only fires when PHI is found
  "nlp_detect_act/redact": 0.4,
  "nlp_detect_act/generalize": 0.65,
  "nlp_detect_act/tokenize": 0.5,
  modified: 0.3,
};

const DEFAULT_STRENGTH = 0.3;

// ---- Grade helpers --------------------------------------------------------

function percentageToGrade(pct: number): LetterGrade {
  if (pct >= 90) return "A";
  if (pct >= 75) return "B";
  if (pct >= 60) return "C";
  if (pct >= 40) return "D";
  return "F";
}

const GRADE_STYLES: Record<LetterGrade, GradeStyle> = {
  A: { bg: "bg-green-100", text: "text-green-800", border: "border-green-200" },
  B: { bg: "bg-lime-100", text: "text-lime-800", border: "border-lime-200" },
  C: { bg: "bg-amber-100", text: "text-amber-800", border: "border-amber-200" },
  D: { bg: "bg-orange-100", text: "text-orange-800", border: "border-orange-200" },
  F: { bg: "bg-red-100", text: "text-red-800", border: "border-red-200" },
};

export function getGradeStyle(grade: LetterGrade): GradeStyle {
  return GRADE_STYLES[grade];
}

// ---- Scoring algorithm ----------------------------------------------------

/**
 * Compute a de-identification quality score from existing PII analysis data.
 *
 * @param piiData       Treated PII fields grouped by resource type
 * @param fieldSummary  All fields present in the output, with actions overlaid
 * @param totalResources Total resource count (including error markers)
 * @param errorCount     Number of resources that failed processing
 */
export function computeQualityScore(
  piiData: PiiDetectionMap,
  fieldSummary: FieldSummaryMap,
  totalResources: number,
  errorCount: number,
): QualityScore {
  // -- Component 1: PII Coverage (60% weight) --
  let sensitivePresent = 0;
  let sensitiveTreated = 0;

  for (const [resType, entries] of Object.entries(fieldSummary)) {
    const typeSpecific = KNOWN_SENSITIVE[resType] ?? [];
    const wildcards = KNOWN_SENSITIVE["*"] ?? [];
    const sensitiveSet = new Set([...typeSpecific, ...wildcards]);

    // Build set of top-level field keys present in the data
    const presentRoots = new Set<string>();
    for (const e of entries) {
      const dot = e.fieldPath.indexOf(".");
      presentRoots.add(dot >= 0 ? e.fieldPath.slice(0, dot) : e.fieldPath);
    }

    // Build lookup of which fields have actions
    const treatedRoots = new Set<string>();
    for (const e of entries) {
      if (e.action) {
        const dot = e.fieldPath.indexOf(".");
        treatedRoots.add(dot >= 0 ? e.fieldPath.slice(0, dot) : e.fieldPath);
        treatedRoots.add(e.fieldPath);
      }
    }

    for (const sensitiveField of sensitiveSet) {
      // Check if this sensitive field is present in the data
      const root = sensitiveField.indexOf(".") >= 0
        ? sensitiveField.slice(0, sensitiveField.indexOf("."))
        : sensitiveField;
      const isPresent = presentRoots.has(root) ||
        entries.some((e) => e.fieldPath === sensitiveField);
      if (!isPresent) continue;

      sensitivePresent++;

      if (treatedRoots.has(sensitiveField) || treatedRoots.has(root)) {
        sensitiveTreated++;
      }
    }
  }

  const coverage = sensitivePresent > 0
    ? sensitiveTreated / sensitivePresent
    : 0; // No sensitive fields found → insufficient data to assess

  // -- Component 2: Action Strength (25% weight) --
  let strengthSum = 0;
  let strengthCount = 0;

  for (const entries of Object.values(piiData)) {
    for (const entry of entries) {
      strengthSum += ACTION_STRENGTH[entry.action] ?? DEFAULT_STRENGTH;
      strengthCount++;
    }
  }

  const strength = strengthCount > 0 ? strengthSum / strengthCount : 0;

  // -- Component 3: Error Rate (15% weight) --
  const errorRate = totalResources > 0
    ? Math.max(0, 1 - errorCount / totalResources)
    : 0;

  // -- Final weighted score --
  const raw = coverage * 0.60 + strength * 0.25 + errorRate * 0.15;
  const percentage = Math.round(Math.min(100, Math.max(0, raw * 100)));
  const grade = percentageToGrade(percentage);

  return { percentage, grade, coverage, strength, errorRate };
}
