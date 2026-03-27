/**
 * FHIR REST wrappers for the HAPI FHIR server (/fhir prefix).
 *
 * These functions query the HAPI FHIR R4 server directly through the
 * nginx reverse proxy (or Vite dev proxy). They handle FHIR Bundle
 * pagination and resource joining (e.g. Condition + included Patient).
 */

import { fetchFhir } from "./client";
import type { ConditionRow, PatientSummary } from "./types";

// ---------------------------------------------------------------------------
// FHIR Bundle types (minimal, just what we need)
// ---------------------------------------------------------------------------

interface FhirBundle {
  resourceType: "Bundle";
  total?: number;
  entry?: Array<{
    resource?: Record<string, unknown>;
    search?: { mode?: string };
  }>;
  link?: Array<{ relation: string; url: string }>;
}

interface FhirHumanName {
  use?: string;
  family?: string;
  given?: string[];
  text?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract a display name from a FHIR HumanName array.
 *
 * Joins `given[]` and `family` from the first name entry. Falls back to
 * `name[0].text` if structured fields are absent.
 */
function formatPatientName(
  names: FhirHumanName[] | undefined,
): string {
  if (!names || names.length === 0) return "";

  const name = names[0];

  // Prefer structured fields
  const given = name.given?.join(" ") ?? "";
  const family = name.family ?? "";
  const formatted = [given, family].filter(Boolean).join(" ");

  if (formatted) return formatted;

  // Fall back to text representation
  return name.text ?? "";
}

// ---------------------------------------------------------------------------
// Capability statement
// ---------------------------------------------------------------------------

/**
 * GET /fhir/metadata -- returns the FHIR CapabilityStatement.
 */
export function capabilityStatement(): Promise<Record<string, unknown>> {
  return fetchFhir<Record<string, unknown>>("/metadata");
}

// ---------------------------------------------------------------------------
// Patient queries
// ---------------------------------------------------------------------------

/**
 * GET /fhir/Patient?_summary=count -- returns total patient count.
 */
export async function patientCount(): Promise<number> {
  const bundle = await fetchFhir<FhirBundle>("/Patient", {
    _summary: "count",
  });
  return bundle.total ?? 0;
}

export interface PagedResult<T> {
  items: T[];
  total: number | null;
  hasMore: boolean;
}

/**
 * GET /fhir/Patient -- search by name with limited elements, paginated.
 *
 * Returns a PagedResult with `items`, `total` (from bundle.total, may be null),
 * and `hasMore` (true when a "next" link is present in the bundle).
 */
export async function searchPatients(
  name?: string,
  count: number = 20,
  offset: number = 0,
): Promise<PagedResult<PatientSummary>> {
  const params: Record<string, string> = {
    _count: String(count),
    _elements: "id,name,birthDate,gender",
  };
  if (offset > 0) {
    params._getpagesoffset = String(offset);
  }
  if (name) {
    params.name = name;
  }

  const bundle = await fetchFhir<FhirBundle>("/Patient", params);
  const hasMore = bundle.link?.some((l) => l.relation === "next") ?? false;

  if (!bundle.entry) return { items: [], total: bundle.total ?? null, hasMore };

  const items = bundle.entry
    .filter((e) => e.resource?.resourceType === "Patient")
    .map((e) => {
      const r = e.resource!;
      return {
        id: String(r.id ?? ""),
        name: formatPatientName(r.name as FhirHumanName[] | undefined),
        birthDate: String(r.birthDate ?? ""),
        gender: String(r.gender ?? ""),
      };
    });

  return { items, total: bundle.total ?? null, hasMore };
}

// ---------------------------------------------------------------------------
// Condition queries
// ---------------------------------------------------------------------------

/**
 * GET /fhir/Condition -- search conditions by code text and include
 * the referenced Patient resources via _include.
 *
 * Replicates the original FHIR client logic: separates Patient and Condition
 * resources from the Bundle, builds a patient lookup map, then joins
 * each Condition to its referenced Patient for display.
 */
export async function searchConditions(
  query?: string,
  clinicalStatus?: string,
  count: number = 20,
  offset: number = 0,
): Promise<PagedResult<ConditionRow>> {
  const params: Record<string, string> = {
    _count: String(count),
    _include: "Condition:subject",
  };
  if (offset > 0) {
    params._getpagesoffset = String(offset);
  }
  if (query) {
    params["code:text"] = query;
  }
  if (clinicalStatus) {
    params["clinical-status"] = clinicalStatus;
  }

  const bundle = await fetchFhir<FhirBundle>("/Condition", params);
  const hasMore = bundle.link?.some((l) => l.relation === "next") ?? false;

  if (!bundle.entry) return { items: [], total: bundle.total ?? null, hasMore };

  // Separate included Patient resources from Condition resources.
  // FHIR _include returns Patients with search.mode = "include" and
  // Conditions with search.mode = "match" (or no search mode).
  const patients = new Map<string, Record<string, unknown>>();
  const conditions: Array<Record<string, unknown>> = [];

  for (const entry of bundle.entry) {
    const resource = entry.resource;
    if (!resource) continue;

    if (resource.resourceType === "Patient") {
      const id = String(resource.id ?? "");
      if (id) {
        patients.set(id, resource);
      }
    } else if (resource.resourceType === "Condition") {
      conditions.push(resource);
    }
  }

  // Join Conditions to their referenced Patients.
  const items = conditions.map((c) => {
    // Extract the patient ID from subject.reference ("Patient/123" -> "123")
    const subjectRef =
      (c.subject as Record<string, unknown> | undefined)?.reference;
    const refStr = typeof subjectRef === "string" ? subjectRef : "";
    const patientId = refStr.includes("/")
      ? refStr.split("/").pop() ?? ""
      : refStr;

    // Look up the included Patient resource
    const patient = patients.get(patientId);

    // Extract condition code/display from coding[0]
    const codeObj = c.code as Record<string, unknown> | undefined;
    const codings = (codeObj?.coding ?? []) as Array<
      Record<string, unknown>
    >;
    const firstCoding = codings[0] ?? {};

    // Extract clinical status
    const clinicalStatusObj = c.clinicalStatus as
      | Record<string, unknown>
      | undefined;
    const statusCodings = (clinicalStatusObj?.coding ?? []) as Array<
      Record<string, unknown>
    >;
    const statusText =
      statusCodings[0]?.code ??
      clinicalStatusObj?.text ??
      "";

    return {
      condition_id: String(c.id ?? ""),
      code: String(firstCoding.code ?? codeObj?.text ?? ""),
      display: String(
        firstCoding.display ?? codeObj?.text ?? "",
      ),
      clinical_status: String(statusText),
      patient_id: patientId,
      patient_name: patient
        ? formatPatientName(
            patient.name as FhirHumanName[] | undefined,
          )
        : "",
      patient_birth_date: patient
        ? String(patient.birthDate ?? "")
        : "",
      patient_gender: patient
        ? String(patient.gender ?? "")
        : "",
    };
  });

  return { items, total: bundle.total ?? null, hasMore };
}
