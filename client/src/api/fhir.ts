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
// Resource type discovery
// ---------------------------------------------------------------------------

/** Infrastructure types excluded from resource-type counts (not useful for export). */
const EXCLUDED_TYPES = new Set([
  "CapabilityStatement", "OperationDefinition", "SearchParameter",
  "StructureDefinition", "CompartmentDefinition", "ImplementationGuide",
  "CodeSystem", "ValueSet", "ConceptMap", "NamingSystem",
  "OperationOutcome", "Bundle",
]);

// Clinically relevant types first — so a capped field-tree (top N) gets the
// PII-heavy resources, and the explorer's type list reads naturally.
const TYPE_PRIORITY = [
  "Patient", "Practitioner", "RelatedPerson", "Observation", "Condition",
  "Encounter", "MedicationRequest", "Procedure", "AllergyIntolerance",
  "DiagnosticReport", "Immunization", "CarePlan", "Organization", "Location",
];

function prioritySort(types: string[]): string[] {
  return [...types].sort((a, b) => {
    const pa = TYPE_PRIORITY.indexOf(a);
    const pb = TYPE_PRIORITY.indexOf(b);
    if (pa !== -1 && pb !== -1) return pa - pb;
    if (pa !== -1) return -1;
    if (pb !== -1) return 1;
    return a.localeCompare(b);
  });
}

/**
 * Read supported resource types from the server's CapabilityStatement,
 * priority-sorted (clinical/PII-heavy types first).
 *
 * Falls back to a minimal list if the metadata request fails.
 */
async function discoverResourceTypes(): Promise<string[]> {
  try {
    const cs = await capabilityStatement();
    const rest = cs.rest as Array<{ resource?: Array<{ type?: string }> }> | undefined;
    const types = rest?.[0]?.resource
      ?.map((r) => r.type)
      .filter((t): t is string => typeof t === "string" && !EXCLUDED_TYPES.has(t));
    if (types && types.length > 0) return prioritySort(types);
  } catch {
    // metadata unavailable — fall through to fallback
  }
  return ["Patient", "Observation", "Condition", "Encounter", "Procedure"];
}

export interface ResourceTypeCount {
  type: string;
  count: number;
}

// Module-level cache for the type list (separate from counts cache).
let _typesCache: string[] | null = null;

/**
 * Return only resource types that actually have data on the server (count > 0),
 * priority-sorted (clinical/PII-heavy types first). Probes each
 * CapabilityStatement type with `?_summary=count` so types the server merely
 * *supports* but holds no data for are excluded. Cached for the browser session.
 */
export async function listResourceTypes(): Promise<string[]> {
  if (_typesCache) return _typesCache;
  // fetchResourceTypeCounts probes every CapabilityStatement type and filters
  // to count > 0 — exactly what we want for the field-tree and type picker.
  const counts = await fetchResourceTypeCounts();
  const types = prioritySort(counts.map((c) => c.type));
  _typesCache = types;
  return types;
}

/**
 * Fetch a small sample of raw resources for a given type (default 5).
 * Returns the raw resource objects — callers are responsible for PHI handling.
 * Used by the AI assistant field-tree builder which extracts paths/types only.
 */
export async function sampleResources(
  type: string,
  count: number = 5,
): Promise<Record<string, unknown>[]> {
  try {
    const bundle = await fetchFhir<FhirBundle>(`/${type}`, {
      _count: String(count),
    });
    return (bundle.entry ?? [])
      .map((e) => e.resource)
      .filter((r): r is Record<string, unknown> => r != null);
  } catch {
    return [];
  }
}

// ---------------------------------------------------------------------------
// Server field-tree (PHI-free) — sampled paths/types for AI grounding.
//
// Cached at module level so reopening the AI Assistant panel (or navigating
// away and back) reuses the tree instead of re-sampling every resource type.
// The cache stores ONLY the extracted summary — never raw resources.
// ---------------------------------------------------------------------------

export interface ServerFieldTree {
  summary: string;          // PHI-free `path : <type>` lines
  resourceTypes: string[];
  pathCount: number;
}

const _FIELD_TREE_TTL_MS = 5 * 60 * 1000; // 5 minutes
let _fieldTreeCache: ServerFieldTree | null = null;
let _fieldTreeCacheAt = 0;
// Whether the cached tree carries sample values (PHI). A request for a different
// mode must miss the cache — a values tree and a paths-only tree differ.
let _fieldTreeCacheValues = false;
let _fieldTreeInflight: Promise<ServerFieldTree> | null = null;

/** Clear the cached field tree and type list (call after the user refreshes). */
export function clearServerFieldTreeCache(): void {
  _fieldTreeCache = null;
  _fieldTreeCacheAt = 0;
  _fieldTreeCacheValues = false;
  _typesCache = null;
  _countsCache = null;
  _countsCacheAt = 0;
}

/**
 * Build a field-tree by sampling `samplePerType` resources of every server
 * type, extracting paths+types via `extractFn` (injected to avoid a cross-module
 * import cycle with the config-builder fieldTree util).
 *
 * By default the tree is PHI-free (paths + types). When `opts.includeValues` is
 * set, `extractFn` is expected to append sample values per leaf — the tree then
 * carries PHI and must only be sent to a local model (the backend enforces this
 * via the AI local-guard when the request is flagged). The cache holds only one
 * mode at a time; switching modes misses the cache.
 *
 * Cached for 5 minutes. Concurrent callers share one in-flight request.
 */
export async function buildServerFieldTree(
  extractFn: (resources: Record<string, unknown>[]) => ServerFieldTree,
  opts?: {
    samplePerType?: number;
    onProgress?: (loaded: number, total: number) => void;
    force?: boolean;
    /** Only sample these resource types (default: all data-bearing types).
     * Scoping to a few types keeps the prompt small and focused — a smaller
     * field tree means lower CPU on CPU-only LLMs and better answers (small
     * models lose focus in a 40-type wall of paths). */
    onlyTypes?: string[];
    /** Cap the number of types sampled when onlyTypes is not given. When
     * omitted, ALL data-bearing types are sampled. */
    maxTypes?: number;
    /** Marks that `extractFn` produces a values-bearing (PHI) tree. Used only
     * to key the cache so a values tree is never served for a paths-only
     * request (or vice-versa); the actual value extraction lives in extractFn. */
    includeValues?: boolean;
  },
): Promise<ServerFieldTree> {
  const samplePerType = opts?.samplePerType ?? 3;
  const onProgress = opts?.onProgress;
  const includeValues = opts?.includeValues ?? false;

  if (
    !opts?.force &&
    _fieldTreeCache &&
    _fieldTreeCacheValues === includeValues &&
    Date.now() - _fieldTreeCacheAt < _FIELD_TREE_TTL_MS
  ) {
    onProgress?.(1, 1);
    return _fieldTreeCache;
  }
  if (_fieldTreeInflight) return _fieldTreeInflight;

  _fieldTreeInflight = (async () => {
    const allTypes = await listResourceTypes();
    const types = opts?.onlyTypes?.length
      ? allTypes.filter((t) => opts.onlyTypes!.includes(t))
      : opts?.maxTypes != null
        ? allTypes.slice(0, opts.maxTypes)
        : allTypes;
    const all: Record<string, unknown>[] = [];
    let loaded = 0;
    const BATCH = 8;
    for (let i = 0; i < types.length; i += BATCH) {
      const batch = types.slice(i, i + BATCH);
      const results = await Promise.allSettled(
        batch.map((t) => sampleResources(t, samplePerType)),
      );
      for (const r of results) {
        if (r.status === "fulfilled") all.push(...r.value);
      }
      loaded += batch.length;
      onProgress?.(Math.min(loaded, types.length), types.length);
    }
    const tree = extractFn(all);
    _fieldTreeCache = tree;
    _fieldTreeCacheAt = Date.now();
    _fieldTreeCacheValues = includeValues;
    return tree;
  })();

  try {
    return await _fieldTreeInflight;
  } finally {
    _fieldTreeInflight = null;
  }
}

// Module-level cache — survives SPA navigation, lives for the browser session.
// Re-fetches after TTL_MS so counts stay reasonably fresh without hammering HAPI.
const _COUNTS_TTL_MS = 5 * 60 * 1000; // 5 minutes
let _countsCache: ResourceTypeCount[] | null = null;
let _countsCacheAt = 0;

/**
 * Discover resource types from the CapabilityStatement, then probe each
 * with `?_summary=count&_count=0` in parallel.
 *
 * Results are cached for 5 minutes so navigating back to the Patient Browser
 * does not re-fire 100+ parallel FHIR count queries on every visit.
 *
 * Returns only types with count > 0, sorted by count descending.
 */
export async function fetchResourceTypeCounts(): Promise<ResourceTypeCount[]> {
  if (_countsCache && Date.now() - _countsCacheAt < _COUNTS_TTL_MS) {
    return _countsCache;
  }

  const types = await discoverResourceTypes();

  const results = await Promise.allSettled(
    types.map(async (type) => {
      const bundle = await fetchFhir<FhirBundle>(`/${type}`, {
        _summary: "count",
        _count: "0",
      });
      return { type, count: bundle.total ?? 0 };
    }),
  );

  const counts = results
    .filter(
      (r): r is PromiseFulfilledResult<ResourceTypeCount> =>
        r.status === "fulfilled" && r.value.count > 0,
    )
    .map((r) => r.value)
    .sort((a, b) => b.count - a.count);

  _countsCache = counts;
  _countsCacheAt = Date.now();
  return counts;
}

/**
 * Fetch raw resources of the given types from the source FHIR server, up to
 * `perType` of each, and return them as one flat list. Used by the Trust Gate
 * page to assemble a real cross-type batch for quality assessment. Unlike
 * `sampleResources`, fetch errors propagate so the UI can report them.
 */
export async function fetchResourcesByTypes(
  types: string[],
  perType: number,
): Promise<Record<string, unknown>[]> {
  const results = await Promise.all(
    types.map(async (t) => {
      const bundle = await fetchFhir<FhirBundle>(`/${t}`, { _count: String(perType) });
      return (bundle.entry ?? [])
        .map((e) => e.resource)
        .filter((r): r is Record<string, unknown> => r != null);
    }),
  );
  return results.flat();
}

/**
 * Fetch a patient's full compartment via `Patient/{id}/$everything`, following
 * pagination. Returns a self-contained resource list (references resolve within
 * the batch), which is the ideal input for the Trust Gate's reference-integrity
 * check. Capped at `maxPages` to bound large compartments.
 */
export async function fetchPatientEverything(
  patientId: string,
  maxPages = 10,
): Promise<Record<string, unknown>[]> {
  const out: Record<string, unknown>[] = [];
  let bundle = await fetchFhir<FhirBundle>(`/Patient/${encodeURIComponent(patientId)}/$everything`, {
    _count: "200",
  });
  let pages = 0;
  while (true) {
    for (const e of bundle.entry ?? []) {
      if (e.resource) out.push(e.resource);
    }
    const next = bundle.link?.find((l) => l.relation === "next")?.url;
    if (!next || ++pages >= maxPages) break;
    bundle = await fetchFhirByUrl<FhirBundle>(next);
  }
  return out;
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
  return /^\d[\d.-]*$/.test(query.trim());
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
