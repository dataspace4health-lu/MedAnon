import type { Decision } from "@/api/trustGate";

// ---------------------------------------------------------------------------
// Decision presentation metadata
// ---------------------------------------------------------------------------

export interface DecisionStyle {
  label: string;
  /** Tailwind classes for the decision banner container. */
  banner: string;
  /** Tailwind text-color class for the decision title + dot. */
  accent: string;
  dot: string;
  blurb: string;
}

export const DECISION_STYLES: Record<Decision, DecisionStyle> = {
  PASS: {
    label: "PASS",
    banner: "border-emerald-500/30 bg-emerald-500/10",
    accent: "text-emerald-600 dark:text-emerald-400",
    dot: "bg-emerald-500",
    blurb: "Data is fit for secondary use. Privacy processing may proceed.",
  },
  CONDITIONAL_PASS: {
    label: "CONDITIONAL PASS",
    banner: "border-amber-500/30 bg-amber-500/10",
    accent: "text-amber-600 dark:text-amber-400",
    dot: "bg-amber-500",
    blurb: "Usable for limited purposes; some quality gaps need remediation.",
  },
  BLOCK: {
    label: "BLOCK",
    banner: "border-destructive/30 bg-destructive/10",
    accent: "text-destructive",
    dot: "bg-destructive",
    blurb: "A critical check failed. Data is not trustworthy enough to transform.",
  },
};

/** Pass-rate → semantic color (matches the ≥90 / ≥80 policy cut-offs). */
export function rateColorClass(pct: number): string {
  if (pct >= 90) return "bg-emerald-500";
  if (pct >= 80) return "bg-amber-500";
  return "bg-destructive";
}

export function rateTextClass(pct: number): string {
  if (pct >= 90) return "text-emerald-600 dark:text-emerald-400";
  if (pct >= 80) return "text-amber-600 dark:text-amber-400";
  return "text-destructive";
}

/** "conformance.reference_integrity" → "Reference integrity" (humanized leaf). */
export function humanizeCheckId(checkId: string): string {
  const leaf = checkId.split(".").pop() ?? checkId;
  const words = leaf.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export const KAHN_CATEGORIES = ["conformance", "completeness", "plausibility"] as const;

// ---------------------------------------------------------------------------
// Server-pull defaults
// ---------------------------------------------------------------------------

export const DEFAULT_PULL_TYPES = ["Patient", "Observation", "Condition", "Encounter"];

// ---------------------------------------------------------------------------
// Paste samples
// ---------------------------------------------------------------------------

export const SAMPLE_CLEAN = JSON.stringify(
  [
    {
      resourceType: "Patient",
      id: "pat-001",
      active: true,
      gender: "female",
      birthDate: "1985-04-12",
      name: [{ family: "Mustermann", given: ["Erika"] }],
      identifier: [{ system: "urn:mrn", value: "MRN-7781" }],
      meta: { lastUpdated: "2024-09-01T00:00:00Z" },
    },
    {
      resourceType: "Observation",
      id: "obs-hr-001",
      status: "final",
      category: [
        {
          coding: [
            {
              system: "http://terminology.hl7.org/CodeSystem/observation-category",
              code: "vital-signs",
            },
          ],
        },
      ],
      code: { coding: [{ system: "http://loinc.org", code: "8867-4", display: "Heart rate" }] },
      subject: { reference: "Patient/pat-001" },
      effectiveDateTime: "2024-09-01T08:30:00Z",
      valueQuantity: { value: 72, unit: "beats/minute", system: "http://unitsofmeasure.org", code: "/min" },
    },
  ],
  null,
  2,
);

export const SAMPLE_BLOCKED = JSON.stringify(
  [
    { resourceType: "Patient", gender: "other" },
    {
      resourceType: "Observation",
      id: "obs-x",
      status: "entered-in-error",
      code: { coding: [{ system: "http://loinc.org", code: "8867-4" }] },
      valueQuantity: { value: 70, code: "/min" },
    },
  ],
  null,
  2,
);

/** Current UTC timestamp as a FHIR-style instant (for the provenance field). */
export function nowInstant(): string {
  return new Date().toISOString();
}

// ---------------------------------------------------------------------------
// OMOP / tabular samples (POST /v1/trust/assess/omop)
// ---------------------------------------------------------------------------

export const SAMPLE_OMOP_CLEAN = JSON.stringify(
  {
    tables: {
      person: [{ person_id: 1, gender_concept_id: 8532, year_of_birth: 1985 }],
      measurement: [
        {
          measurement_id: 1,
          person_id: 1,
          measurement_concept_id: 3004249,
          measurement_date: "2024-01-01",
          value_as_number: 118,
        },
      ],
    },
  },
  null,
  2,
);

export const SAMPLE_OMOP_TABULAR = JSON.stringify(
  {
    tables: {
      person: [{ pid: 1, sex: 8532, yob: 1985 }],
      measurement: [{ mid: 1, pid: 1, concept: 3004249, mdate: "2024-01-01", val: 118 }],
    },
    mapping: {
      person: { pid: "person_id", sex: "gender_concept_id", yob: "year_of_birth" },
      measurement: { mid: "measurement_id", pid: "person_id", concept: "measurement_concept_id", mdate: "measurement_date", val: "value_as_number" },
    },
  },
  null,
  2,
);
