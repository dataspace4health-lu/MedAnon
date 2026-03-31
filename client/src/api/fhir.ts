/**
 * FHIR REST wrappers for the HAPI FHIR server (/fhir prefix).
 *
 * These functions query the HAPI FHIR R4 server directly through the
 * nginx reverse proxy (or Vite dev proxy). They handle FHIR Bundle
 * pagination and resource joining (e.g. Condition + included Patient).
 */

import { fetchFhir, fetchFhirByUrl } from "./client";
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

export interface PatientInfo {
  name: string;
  birthDate: string;
  gender: string;
}

/** Cached patient data keyed by FHIR Patient id — accumulated across pages. */
export type PatientMap = Record<string, PatientInfo>;

export interface ConditionPageResult extends PagedResult<ConditionRow> {
  /** Patient records included in this page's bundle. Merge into a running
   *  cache so load-more pages can still resolve patient names for conditions
   *  whose Patient resource was only included on an earlier page. */
  patientMap: PatientMap;
  /** The absolute HAPI URL for the next page, or null if this is the last page.
   *  Use fetchConditionsNextPage() to follow it — do NOT reconstruct with offset. */
  nextLink: string | null;
}

/**
 * Returns true when the query looks like a code value (all digits / digits with
 * dots or hyphens), e.g. a SNOMED CT code like "73211009" or ICD-10 "E11.9".
 * In that case we use the `code=` parameter instead of `code:text=` so HAPI
 * matches the actual code system value rather than the display text.
 */
function isCodeQuery(query: string): boolean {
  return /^\d[\d.\-]*$/.test(query.trim());
}

/** Parse a raw FHIR bundle into a ConditionPageResult. Shared by the initial
 *  search and the next-link pagination path. */
function parseBundleToConditionPage(
  bundle: FhirBundle,
  knownPatients: PatientMap,
): ConditionPageResult {
  const nextLink =
    bundle.link?.find((l) => l.relation === "next")?.url ?? null;
  const hasMore = nextLink !== null;

  if (!bundle.entry) {
    return { items: [], total: bundle.total ?? null, hasMore, nextLink, patientMap: {} };
  }

  const pagePatients: PatientMap = {};
  const conditions: Array<Record<string, unknown>> = [];

  for (const entry of bundle.entry) {
    const resource = entry.resource;
    if (!resource) continue;

    if (resource.resourceType === "Patient") {
      const id = String(resource.id ?? "");
      if (id) {
        pagePatients[id] = {
          name: formatPatientName(resource.name as FhirHumanName[] | undefined),
          birthDate: String(resource.birthDate ?? ""),
          gender: String(resource.gender ?? ""),
        };
      }
    } else if (resource.resourceType === "Condition") {
      conditions.push(resource);
    }
  }

  // Merge: page patients take precedence over cache (fresher data).
  const mergedPatients: PatientMap = { ...knownPatients, ...pagePatients };

  const items = conditions.map((c) => {
    const subjectRef =
      (c.subject as Record<string, unknown> | undefined)?.reference;
    const refStr = typeof subjectRef === "string" ? subjectRef : "";
    const patientId = refStr.includes("/")
      ? refStr.split("/").pop() ?? ""
      : refStr;

    const patient = mergedPatients[patientId];

    const codeObj = c.code as Record<string, unknown> | undefined;
    const codings = (codeObj?.coding ?? []) as Array<Record<string, unknown>>;
    const firstCoding = codings[0] ?? {};

    const clinicalStatusObj = c.clinicalStatus as Record<string, unknown> | undefined;
    const statusCodings = (clinicalStatusObj?.coding ?? []) as Array<Record<string, unknown>>;
    const statusText = statusCodings[0]?.code ?? clinicalStatusObj?.text ?? "";

    return {
      condition_id: String(c.id ?? ""),
      code: String(firstCoding.code ?? codeObj?.text ?? ""),
      display: String(firstCoding.display ?? codeObj?.text ?? ""),
      clinical_status: String(statusText),
      patient_id: patientId,
      patient_name: patient?.name ?? "",
      patient_birth_date: patient?.birthDate ?? "",
      patient_gender: patient?.gender ?? "",
    };
  });

  return { items, total: bundle.total ?? null, hasMore, nextLink, patientMap: pagePatients };
}

/**
 * GET /fhir/Condition — search conditions with _include:Condition:subject.
 *
 * Pass `category` to restrict the result set:
 *   "encounter-diagnosis,problem-list-item" (default) — clinical conditions only,
 *     excludes social determinants (employment, criminal record, etc.)
 *   "" — all conditions including social/contextual ones
 *   "social-history" — social determinants only
 *
 * Returns `nextLink` (absolute HAPI URL for the next page). Use
 * fetchConditionsNextPage() to paginate — do NOT reconstruct with _getpagesoffset,
 * as HAPI's cursor-based paging doesn't support arbitrary offset reconstruction.
 */
export async function searchConditions(
  query?: string,
  clinicalStatus?: string,
  count: number = 20,
  category: string = "encounter-diagnosis,problem-list-item",
  knownPatients: PatientMap = {},
): Promise<ConditionPageResult> {
  const params: Record<string, string> = {
    _count: String(count),
    _include: "Condition:subject",
  };
  if (query) {
    if (isCodeQuery(query)) {
      params["code"] = query;
    } else {
      params["code:text"] = query;
    }
  }
  if (clinicalStatus) {
    params["clinical-status"] = clinicalStatus;
  }
  if (category) {
    params["category"] = category;
  }

  const bundle = await fetchFhir<FhirBundle>("/Condition", params);
  return parseBundleToConditionPage(bundle, knownPatients);
}

/**
 * Fetch the next page of a condition search by following the bundle `next` link.
 *
 * HAPI FHIR uses cursor-based paging: the `next` link encodes a server-side
 * page token (_getpages=...) that cannot be reconstructed from an offset.
 * Always call this instead of issuing a new searchConditions() with an offset.
 */
export async function fetchConditionsNextPage(
  nextLink: string,
  knownPatients: PatientMap = {},
): Promise<ConditionPageResult> {
  const bundle = await fetchFhirByUrl<FhirBundle>(nextLink);
  return parseBundleToConditionPage(bundle, knownPatients);
}

/**
 * Fetch ALL pages of a condition search and return the complete deduplicated list.
 *
 * Uses a large page size (200) to minimise round-trips, then follows every
 * `next` link until the server has no more pages. Conditions are deduplicated
 * by `condition_id` across pages to handle HAPI cursor-paging overlaps.
 */
export async function searchAllConditions(
  query?: string,
  clinicalStatus?: string,
  category: string = "encounter-diagnosis,problem-list-item",
): Promise<{ items: ConditionRow[]; total: number | null }> {
  let patientMap: PatientMap = {};
  const seen = new Set<string>();
  const all: ConditionRow[] = [];

  // First page — use a large count to minimise round-trips
  let page = await searchConditions(query, clinicalStatus, 200, category, patientMap);
  let total = page.total;

  for (const item of page.items) {
    if (!seen.has(item.condition_id)) {
      seen.add(item.condition_id);
      all.push(item);
    }
  }
  patientMap = { ...patientMap, ...page.patientMap };

  // Follow every subsequent page
  while (page.nextLink) {
    page = await fetchConditionsNextPage(page.nextLink, patientMap);
    if (page.total !== null) total = page.total;
    for (const item of page.items) {
      if (!seen.has(item.condition_id)) {
        seen.add(item.condition_id);
        all.push(item);
      }
    }
    patientMap = { ...patientMap, ...page.patientMap };
  }

  return { items: all, total };
}
