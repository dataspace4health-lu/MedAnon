/**
 * Connection-aware FHIR scanning for the Trust Gate page.
 *
 * Supports three connection kinds:
 *   - source : the built-in source HAPI server   (nginx /fhir)
 *   - target : the de-identified target server   (nginx /fhir-target)
 *   - custom : any user-supplied FHIR base URL   (backend SSRF-guarded proxy)
 *
 * A "full-server scan" paginates through all selected resource types, assesses
 * the data in chunks via the Trust Gate, and folds the per-chunk passports into
 * one server-wide Quality Passport (see trustGate.aggregateChunks).
 */

import {
  fetchFhir,
  fetchFhirByUrl,
  fetchFhirTarget,
  fetchFhirTargetByUrl,
  fetchFhirProxy,
} from "./client";
import { assessBatch, aggregateChunks } from "./trustGate";
import type { QualityPassport } from "./trustGate";

// ---------------------------------------------------------------------------
// Connection model
// ---------------------------------------------------------------------------

export type ConnKind = "source" | "target" | "custom";

export interface FhirConnection {
  id: string;
  label: string;
  kind: ConnKind;
  /** Absolute FHIR base URL — custom connections only. */
  baseUrl?: string;
  /** Optional bearer token — custom connections only. */
  token?: string;
}

export const SOURCE_CONN: FhirConnection = { id: "source", label: "Source HAPI", kind: "source" };
export const TARGET_CONN: FhirConnection = {
  id: "target",
  label: "Target HAPI (de-identified)",
  kind: "target",
};

const CUSTOM_STORAGE_KEY = "medanon_fhir_connections";

/** Load persisted custom connections from localStorage. */
export function loadCustomConnections(): FhirConnection[] {
  try {
    const raw = localStorage.getItem(CUSTOM_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as FhirConnection[];
    return Array.isArray(parsed) ? parsed.filter((c) => c.kind === "custom" && c.baseUrl) : [];
  } catch {
    return [];
  }
}

export function saveCustomConnections(conns: FhirConnection[]): void {
  try {
    localStorage.setItem(CUSTOM_STORAGE_KEY, JSON.stringify(conns));
  } catch {
    /* localStorage unavailable — non-fatal */
  }
}

// ---------------------------------------------------------------------------
// Bundle plumbing
// ---------------------------------------------------------------------------

interface FhirBundle {
  resourceType: "Bundle";
  total?: number;
  entry?: Array<{ resource?: Record<string, unknown> }>;
  link?: Array<{ relation: string; url: string }>;
}

function trimBase(url: string): string {
  return url.replace(/\/$/, "");
}

/**
 * A connection is fetched through the backend SSRF-guarded proxy when it is a
 * custom server, OR when a built-in Source/Target carries an override base URL
 * (the user re-pointed it at a different server on the Settings page). Otherwise
 * the built-ins use their dedicated nginx proxies.
 */
function viaProxy(conn: FhirConnection): boolean {
  return conn.kind === "custom" || Boolean(conn.baseUrl);
}

async function firstPage(conn: FhirConnection, type: string, count: number): Promise<FhirBundle> {
  if (viaProxy(conn))
    return fetchFhirProxy<FhirBundle>(`${trimBase(conn.baseUrl!)}/${type}?_count=${count}`, conn.token);
  if (conn.kind === "target")
    return fetchFhirTarget<FhirBundle>(`/${type}`, { _count: String(count) });
  return fetchFhir<FhirBundle>(`/${type}`, { _count: String(count) });
}

async function nextPage(conn: FhirConnection, bundle: FhirBundle): Promise<FhirBundle | null> {
  const next = bundle.link?.find((l) => l.relation === "next")?.url;
  if (!next) return null;
  if (viaProxy(conn)) return fetchFhirProxy<FhirBundle>(next, conn.token);
  if (conn.kind === "target") return fetchFhirTargetByUrl<FhirBundle>(next);
  return fetchFhirByUrl<FhirBundle>(next);
}

function entriesOf(bundle: FhirBundle): Record<string, unknown>[] {
  return (bundle.entry ?? []).map((e) => e.resource).filter((r): r is Record<string, unknown> => r != null);
}

// ---------------------------------------------------------------------------
// Global reference-integrity tracking (mirrors the engine, but across the WHOLE
// scan — chunked summing would count every cross-chunk reference as dangling).
// ---------------------------------------------------------------------------

/** Reduce a literal reference to its exact "ResourceType/id" key (mirrors engine). */
function normalizeRef(ref: string): string | null {
  if (!ref.includes("/") || /^(#|urn:|https?:\/\/)/.test(ref)) return null;
  let base = ref.split("?")[0];
  const hist = base.indexOf("/_history/");
  if (hist !== -1) base = base.slice(0, hist);
  const parts = base.replace(/\/$/, "").split("/");
  if (parts.length >= 2 && parts[parts.length - 2] && parts[parts.length - 1]) {
    return `${parts[parts.length - 2]}/${parts[parts.length - 1]}`;
  }
  return null;
}

interface RefRecord {
  fromType: string;
  fromId: string;
  target: string;
}

/** Walk a resource collecting every literal `reference` string with its owner. */
function collectReferences(res: Record<string, unknown>, out: RefRecord[]): void {
  const fromType = String(res.resourceType ?? "");
  const fromId = String(res.id ?? "");
  const stack: unknown[] = [res];
  while (stack.length) {
    const node = stack.pop();
    if (Array.isArray(node)) {
      stack.push(...node);
    } else if (node && typeof node === "object") {
      for (const [k, v] of Object.entries(node)) {
        if (k === "reference" && typeof v === "string") {
          const target = normalizeRef(v);
          if (target) out.push({ fromType, fromId, target });
        } else if (v && typeof v === "object") {
          stack.push(v);
        }
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Resource-type discovery + counts
// ---------------------------------------------------------------------------

const EXCLUDED_TYPES = new Set([
  "CapabilityStatement", "OperationDefinition", "SearchParameter", "StructureDefinition",
  "CompartmentDefinition", "ImplementationGuide", "CodeSystem", "ValueSet", "ConceptMap",
  "NamingSystem", "OperationOutcome", "Bundle", "Subscription",
]);

async function capability(conn: FhirConnection): Promise<Record<string, unknown>> {
  if (viaProxy(conn))
    return fetchFhirProxy<Record<string, unknown>>(`${trimBase(conn.baseUrl!)}/metadata`, conn.token);
  if (conn.kind === "target") return fetchFhirTarget<Record<string, unknown>>("/metadata");
  return fetchFhir<Record<string, unknown>>("/metadata");
}

async function countOf(conn: FhirConnection, type: string): Promise<number> {
  if (viaProxy(conn)) {
    const b = await fetchFhirProxy<FhirBundle>(
      `${trimBase(conn.baseUrl!)}/${type}?_summary=count&_count=0`,
      conn.token,
    );
    return b.total ?? 0;
  }
  if (conn.kind === "target") {
    const b = await fetchFhirTarget<FhirBundle>(`/${type}`, { _summary: "count", _count: "0" });
    return b.total ?? 0;
  }
  const b = await fetchFhir<FhirBundle>(`/${type}`, { _summary: "count", _count: "0" });
  return b.total ?? 0;
}

export interface TypeCount {
  type: string;
  count: number;
}

/** Discover the data-bearing resource types on a connection, with counts (desc). */
export async function listTypeCounts(conn: FhirConnection): Promise<TypeCount[]> {
  const cs = await capability(conn);
  const rest = cs.rest as Array<{ resource?: Array<{ type?: string }> }> | undefined;
  const types = (rest?.[0]?.resource ?? [])
    .map((r) => r.type)
    .filter((t): t is string => typeof t === "string" && !EXCLUDED_TYPES.has(t));
  const unique = [...new Set(types)];
  const results = await Promise.allSettled(unique.map(async (t) => ({ type: t, count: await countOf(conn, t) })));
  return results
    .filter((r): r is PromiseFulfilledResult<TypeCount> => r.status === "fulfilled" && r.value.count > 0)
    .map((r) => r.value)
    .sort((a, b) => b.count - a.count);
}

/** Fetch up to `perType` resources of each type from a connection (one page each). */
export async function fetchSample(
  conn: FhirConnection,
  types: string[],
  perType: number,
): Promise<Record<string, unknown>[]> {
  const out: Record<string, unknown>[] = [];
  for (const t of types) {
    const bundle = await firstPage(conn, t, perType);
    out.push(...entriesOf(bundle));
  }
  return out;
}

async function everythingFirst(conn: FhirConnection, patientId: string): Promise<FhirBundle> {
  const path = `/Patient/${encodeURIComponent(patientId)}/$everything`;
  if (viaProxy(conn)) return fetchFhirProxy<FhirBundle>(`${trimBase(conn.baseUrl!)}${path}?_count=200`, conn.token);
  if (conn.kind === "target") return fetchFhirTarget<FhirBundle>(path, { _count: "200" });
  return fetchFhir<FhirBundle>(path, { _count: "200" });
}

/** Fetch a patient compartment via `$everything` from any connection, paginated. */
export async function fetchEverythingVia(
  conn: FhirConnection,
  patientId: string,
  maxPages = 10,
): Promise<Record<string, unknown>[]> {
  const out: Record<string, unknown>[] = [];
  let bundle: FhirBundle | null = await everythingFirst(conn, patientId);
  let pages = 0;
  while (bundle) {
    out.push(...entriesOf(bundle));
    if (++pages >= maxPages) break;
    bundle = await nextPage(conn, bundle);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Full-server scan
// ---------------------------------------------------------------------------

export interface ScanProgress {
  /** Resource type currently being fetched. */
  type: string;
  /** Resources fetched so far (across all types). */
  fetched: number;
  /** Total resources targeted for this scan. */
  target: number;
  /** Chunks assessed by the Trust Gate so far. */
  chunksAssessed: number;
  /** Patient compartments processed (patient-compartment scan). */
  patientsDone?: number;
  /** Total patients on the connection. */
  patientsTotal?: number;
}

// FHIR patient-compartment resource types — assessed together per patient so
// references resolve and the patient timeline is coherent. Everything else
// (Organization, Practitioner, Location, …) is swept separately and deduped.
const PATIENT_COMPARTMENT_TYPES = new Set([
  "Patient", "Observation", "Condition", "Procedure", "Encounter", "AllergyIntolerance",
  "Immunization", "MedicationRequest", "MedicationStatement", "MedicationAdministration",
  "MedicationDispense", "DiagnosticReport", "DocumentReference", "CarePlan", "CareTeam",
  "Goal", "ServiceRequest", "Specimen", "ImagingStudy", "FamilyMemberHistory", "Communication",
  "RiskAssessment", "ClinicalImpression", "NutritionOrder", "DeviceRequest", "DeviceUseStatement",
  "Flag", "Consent", "Coverage", "Claim", "ExplanationOfBenefit", "QuestionnaireResponse",
  "List", "Composition", "Appointment", "EpisodeOfCare", "Provenance", "Media", "Basic",
]);

/**
 * Patient-compartment scan: assess each patient's COMPLETE record (Patient/$everything)
 * as one batch so referential integrity, the patient timeline, and cross-field
 * clinical-logic checks are scored against real context (not arbitrary type-batches).
 * Per-resource conformance is unaffected; value distributions + reference integrity
 * are pooled globally. No resource cap — the whole server is ingested, streamed
 * patient-by-patient (bounded memory). Non-compartment resources are swept after.
 */
export async function scanByPatient(
  conn: FhirConnection,
  typeCounts: TypeCount[],
  opts: ScanOptions,
): Promise<QualityPassport> {
  const chunkSize = opts.chunkSize ?? 2000;
  const pageSize = opts.pageSize ?? 1000;
  const patientsTotal = typeCounts.find((t) => t.type === "Patient")?.count ?? 0;
  const target = typeCounts.reduce((s, t) => s + t.count, 0);

  const passports: QualityPassport[] = [];
  const presentIds = new Set<string>();
  const allRefs: RefRecord[] = [];
  let buffer: Record<string, unknown>[] = [];
  let fetched = 0;
  let patientsDone = 0;

  const abort = () => {
    if (opts.signal?.aborted) throw new DOMException("scan aborted", "AbortError");
  };
  const flush = async () => {
    if (!buffer.length) return;
    abort();
    const p = await assessBatch(buffer, {
      datasetId: `${opts.datasetId}-batch-${passports.length + 1}`,
      provenance: opts.provenance,
      sourceTypes: opts.sourceTypes,
      phases: opts.phases,
      externalValidation: opts.externalValidation ?? false,
    });
    passports.push(p);
    buffer = [];
    opts.onProgress?.({ type: "", fetched, target, chunksAssessed: passports.length, patientsDone, patientsTotal });
  };
  const add = (r: Record<string, unknown>) => {
    const rt = r.resourceType;
    if (!rt) return;
    const key = r.id ? `${rt}/${r.id}` : null;
    if (key) {
      if (presentIds.has(key)) return; // dedupe shared resources across compartments
      presentIds.add(key);
    }
    buffer.push(r);
    fetched++;
    collectReferences(r, allRefs);
  };

  // 1. Patient compartments — each patient's whole record, kept intact in a batch.
  let pbundle: FhirBundle | null = await firstPage(conn, "Patient", pageSize);
  while (pbundle) {
    abort();
    for (const pat of entriesOf(pbundle)) {
      const pid = pat.id ? String(pat.id) : null;
      if (!pid) continue;
      const compartment = await fetchEverythingVia(conn, pid, 50);
      // Don't split a single compartment across batches.
      if (buffer.length && buffer.length + compartment.length > chunkSize) await flush();
      for (const r of compartment) add(r);
      patientsDone++;
      if (buffer.length >= chunkSize) await flush();
      opts.onProgress?.({ type: "Patient", fetched, target, chunksAssessed: passports.length, patientsDone, patientsTotal });
      abort();
    }
    pbundle = await nextPage(conn, pbundle);
  }
  await flush();

  // 2. Non-compartment resources (Organization, Practitioner, Location, …).
  for (const { type } of typeCounts) {
    if (PATIENT_COMPARTMENT_TYPES.has(type)) continue;
    let b: FhirBundle | null = await firstPage(conn, type, pageSize);
    while (b) {
      abort();
      for (const r of entriesOf(b)) add(r);
      if (buffer.length >= chunkSize) await flush();
      opts.onProgress?.({ type, fetched, target, chunksAssessed: passports.length, patientsDone, patientsTotal });
      b = await nextPage(conn, b);
    }
  }
  await flush();

  if (!passports.length) throw new Error("The server returned no resources to assess.");

  const dangling = allRefs.filter((r) => !presentIds.has(r.target));
  const referenceIntegrity = {
    applicable: allRefs.length,
    violations: dangling.length,
    violation_details: dangling.slice(0, 50).map((r) => ({
      check_id: "conformance.reference_integrity",
      resource_type: r.fromType,
      resource_id: r.fromId,
      path: "reference",
      detail: `reference to ${r.target} not present in the scanned set`,
    })),
  };

  return aggregateChunks(passports, {
    datasetId: opts.datasetId,
    provenance: opts.provenance,
    resourceCount: fetched,
    chunkCount: passports.length,
    sourceTypes: opts.sourceTypes,
    referenceIntegrity,
    intendedUse: opts.intendedUse,
  });
}

export interface ScanOptions {
  chunkSize?: number;
  maxResources?: number;
  pageSize?: number;
  datasetId: string;
  provenance: Record<string, unknown>;
  sourceTypes?: string[];
  /** Audit phases to run per chunk (omit → all). */
  phases?: string[];
  /** Declared downstream use → purpose-bound fitness in the aggregate. */
  intendedUse?: string;
  /** Run the slow external FHIR validator per chunk. Defaults to false for
   * full-server scans: at this volume $validate times out to NA and costs
   * ~45s/chunk for no signal; the in-process structural checks still run. */
  externalValidation?: boolean;
  onProgress?: (p: ScanProgress) => void;
  signal?: AbortSignal;
}

/**
 * Scan all selected types on a connection and return one aggregated passport.
 *
 * Resources stream into a buffer; each time it reaches `chunkSize` the chunk is
 * sent to the Trust Gate and the passport collected. Stops fetching once
 * `maxResources` is reached. Throws if aborted via `opts.signal`.
 */
export async function scanServer(
  conn: FhirConnection,
  typeCounts: TypeCount[],
  opts: ScanOptions,
): Promise<QualityPassport> {
  const chunkSize = opts.chunkSize ?? 2000;
  const pageSize = opts.pageSize ?? 1000;
  const maxResources = opts.maxResources ?? 50000;
  const target = Math.min(
    typeCounts.reduce((s, t) => s + t.count, 0),
    maxResources,
  );

  const passports: QualityPassport[] = [];
  let buffer: Record<string, unknown>[] = [];
  let fetched = 0;

  // Global reference-integrity state across the whole scan.
  const presentIds = new Set<string>();
  const allRefs: RefRecord[] = [];

  const abort = () => {
    if (opts.signal?.aborted) throw new DOMException("scan aborted", "AbortError");
  };

  const flush = async () => {
    if (!buffer.length) return;
    abort();
    const p = await assessBatch(buffer, {
      datasetId: `${opts.datasetId}-chunk-${passports.length + 1}`,
      provenance: opts.provenance,
      sourceTypes: opts.sourceTypes,
      phases: opts.phases,
    });
    passports.push(p);
    buffer = [];
    opts.onProgress?.({ type: "", fetched, target, chunksAssessed: passports.length });
  };

  for (const { type } of typeCounts) {
    if (fetched >= maxResources) break;
    let bundle: FhirBundle | null = await firstPage(conn, type, pageSize);
    while (bundle) {
      abort();
      for (const r of entriesOf(bundle)) {
        buffer.push(r);
        fetched++;
        if (r.resourceType && r.id) presentIds.add(`${r.resourceType}/${r.id}`);
        collectReferences(r, allRefs);
        if (buffer.length >= chunkSize) await flush();
        if (fetched >= maxResources) break;
      }
      opts.onProgress?.({ type, fetched, target, chunksAssessed: passports.length });
      if (fetched >= maxResources) break;
      bundle = await nextPage(conn, bundle);
    }
  }
  await flush();

  if (!passports.length) {
    throw new Error("The server returned no resources for the selected types.");
  }

  // Global reference integrity over the whole scanned set.
  const dangling = allRefs.filter((r) => !presentIds.has(r.target));
  const referenceIntegrity = {
    applicable: allRefs.length,
    violations: dangling.length,
    violation_details: dangling.slice(0, 50).map((r) => ({
      check_id: "conformance.reference_integrity",
      resource_type: r.fromType,
      resource_id: r.fromId,
      path: "reference",
      detail: `reference to ${r.target} not present in the scanned set`,
    })),
  };

  return aggregateChunks(passports, {
    datasetId: opts.datasetId,
    provenance: opts.provenance,
    resourceCount: fetched,
    chunkCount: passports.length,
    sourceTypes: opts.sourceTypes,
    referenceIntegrity,
    intendedUse: opts.intendedUse,
  });
}
