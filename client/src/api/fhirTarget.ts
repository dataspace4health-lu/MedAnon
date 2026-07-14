/**
 * FHIR REST wrappers for the de-identified HAPI FHIR target server (/fhir-target prefix).
 *
 * These functions query the target HAPI FHIR R4 server (HAPI 2) where
 * de-identified resources are stored, through the nginx reverse proxy
 * (or Vite dev proxy). Used by TargetBrowserPage to verify that
 * de-identified resources have been stored correctly.
 */

// Route through the active Target selection (Settings) instead of always hitting
// the built-in nginx /fhir-target. Same signature, so call sites are unchanged.
import { routedFetchFhirTarget as fetchFhirTarget } from "./fhirRoute";

// ---------------------------------------------------------------------------
// FHIR Bundle types (minimal)
// ---------------------------------------------------------------------------

interface FhirBundle {
  resourceType: "Bundle";
  total?: number;
  entry?: Array<{
    resource?: Record<string, unknown>;
    fullUrl?: string;
  }>;
  link?: Array<{ relation: string; url: string }>;
}

// ---------------------------------------------------------------------------
// Capability statement
// ---------------------------------------------------------------------------

/** GET /fhir-target/metadata, returns the FHIR CapabilityStatement. */
export function capabilityStatementTarget(): Promise<Record<string, unknown>> {
  return fetchFhirTarget<Record<string, unknown>>("/metadata");
}

// ---------------------------------------------------------------------------
// Resource type discovery + counting
// ---------------------------------------------------------------------------

/** Infrastructure types excluded from resource-type counts. */
const EXCLUDED_TYPES = new Set([
  "CapabilityStatement", "OperationDefinition", "SearchParameter",
  "StructureDefinition", "CompartmentDefinition", "ImplementationGuide",
  "CodeSystem", "ValueSet", "ConceptMap", "NamingSystem",
  "OperationOutcome", "Bundle",
]);

async function discoverResourceTypesTarget(): Promise<string[]> {
  try {
    const cs = await capabilityStatementTarget();
    const rest = cs.rest as Array<{ resource?: Array<{ type?: string }> }> | undefined;
    const types = rest?.[0]?.resource
      ?.map((r) => r.type)
      .filter((t): t is string => typeof t === "string" && !EXCLUDED_TYPES.has(t));
    if (types && types.length > 0) return types;
  } catch {
    // metadata unavailable, fall through to fallback
  }
  return ["Patient", "Observation", "Condition", "Encounter", "Procedure"];
}

export interface ResourceTypeCount {
  type: string;
  count: number;
}

const _TARGET_COUNTS_TTL_MS = 5 * 60 * 1000; // 5 minutes
let _targetCountsCache: ResourceTypeCount[] | null = null;
let _targetCountsCacheAt = 0;

/**
 * Discover resource types from the target server's CapabilityStatement,
 * then probe each with `?_summary=count&_count=0` in parallel.
 * Results are cached for 5 minutes.
 * Returns only types with count > 0, sorted by count descending.
 */
export async function fetchResourceTypeCountsTarget(): Promise<ResourceTypeCount[]> {
  if (_targetCountsCache && Date.now() - _targetCountsCacheAt < _TARGET_COUNTS_TTL_MS) {
    return _targetCountsCache;
  }

  const types = await discoverResourceTypesTarget();

  const results = await Promise.allSettled(
    types.map(async (type) => {
      const bundle = await fetchFhirTarget<FhirBundle>(`/${type}`, {
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

  _targetCountsCache = counts;
  _targetCountsCacheAt = Date.now();
  return counts;
}

// ---------------------------------------------------------------------------
// Resource browsing
// ---------------------------------------------------------------------------

export interface ResourcePage {
  items: Record<string, unknown>[];
  total: number | null;
  hasMore: boolean;
  nextLink: string | null;
}

/**
 * GET /fhir-target/{type}?_count={count}&_getpagesoffset={offset}
 * Returns a page of resources of the given type from the target server.
 */
export async function fetchResourcesTarget(
  type: string,
  count: number = 20,
  offset: number = 0,
): Promise<ResourcePage> {
  const params: Record<string, string> = {
    _count: String(count),
    _sort: "_id",
  };
  if (offset > 0) {
    params._getpagesoffset = String(offset);
  }

  const bundle = await fetchFhirTarget<FhirBundle>(`/${type}`, params);
  const nextLink = bundle.link?.find((l) => l.relation === "next")?.url ?? null;

  const items = (bundle.entry ?? [])
    .map((e) => e.resource)
    .filter((r): r is Record<string, unknown> => !!r);

  return {
    items,
    total: bundle.total ?? null,
    hasMore: nextLink !== null,
    nextLink,
  };
}

