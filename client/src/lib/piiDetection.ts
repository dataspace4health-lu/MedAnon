// ---------------------------------------------------------------------------
// PII detection: combines backend manifest + client-side field diff
// ---------------------------------------------------------------------------

import { extractFieldsDeep } from "./fhirFields";

const MANIFEST_SYSTEM = "https://medanon.local/transformation-manifest";

interface PiiFieldEntry {
  fieldPath: string;
  action: string;
  count: number;
}

/** PII detections grouped by FHIR resource type. */
export type PiiDetectionMap = Record<string, PiiFieldEntry[]>;

interface ManifestEntry {
  rule: string;
  action: string;
  path: string;
}

/** Extract the transformation manifest from a de-identified resource's meta.tag. */
function parseManifestEntries(
  resource: Record<string, unknown>,
): ManifestEntry[] {
  const meta = resource.meta as Record<string, unknown> | undefined;
  if (!meta) return [];
  const tags = meta.tag as Array<Record<string, unknown>> | undefined;
  if (!Array.isArray(tags)) return [];

  for (const tag of tags) {
    if (tag.system === MANIFEST_SYSTEM && typeof tag.display === "string") {
      try {
        return JSON.parse(tag.display) as ManifestEntry[];
      } catch {
        return [];
      }
    }
  }
  return [];
}

/**
 * Strip the resourceType prefix from a manifest FHIRPath, preserving sub-paths.
 * "Patient.name.family"        → "name.family"
 * "Observation.effectiveDateTime" → "effectiveDateTime"
 * "Patient.name[0].family"     → "name.family"  (array indices removed)
 */
function simplifyPath(fullPath: string): string {
  const dotIdx = fullPath.indexOf(".");
  const rest = dotIdx >= 0 ? fullPath.slice(dotIdx + 1) : fullPath;
  // Remove array indices like [0], [1]
  return rest.replace(/\[\d+\]/g, "");
}

/** Return the top-level key of a dotted path: "name.family" → "name". */
function parentKey(path: string): string {
  const dot = path.indexOf(".");
  return dot >= 0 ? path.slice(0, dot) : path;
}

/**
 * Look up an action for a field path in a manifest map.
 * Tries exact match first, then falls back to the parent key so that
 * a manifest entry for "name" covers deep paths like "name.family".
 */
function resolveAction(
  manifestByField: Map<string, string>,
  field: string,
): string | undefined {
  return manifestByField.get(field) ?? manifestByField.get(parentKey(field));
}

/**
 * Build a PII detection map from paired original + de-identified resources.
 *
 * Strategy:
 * 1. Parse manifest entries from the de-identified resource (authoritative).
 * 2. Diff deep-extracted fields to find changed/removed sub-fields.
 * 3. For each changed field, prefer the manifest action label; if no manifest
 *    entry covers that field or its parent, label it "modified".
 * 4. Aggregate by (resourceType, fieldPath, action) and count occurrences.
 */
export function buildPiiDetectionMap(
  originals: Record<string, unknown>[],
  deidentified: Record<string, unknown>[],
): PiiDetectionMap {
  const agg = new Map<string, Map<string, number>>();

  const len = Math.min(originals.length, deidentified.length);
  for (let i = 0; i < len; i++) {
    const orig = originals[i];
    const deid = deidentified[i];
    const resourceType =
      (deid.resourceType as string) ??
      (orig.resourceType as string) ??
      "Unknown";

    // Parse manifest (may be empty when MEDANON_MANIFEST_ENABLED is off)
    const manifestEntries = parseManifestEntries(deid);
    const manifestByField = new Map<string, string>();
    for (const entry of manifestEntries) {
      const simple = simplifyPath(entry.path);
      if (!manifestByField.has(simple)) {
        manifestByField.set(simple, entry.action);
      }
    }

    // Diff deep-extracted fields
    const origFields = extractFieldsDeep(orig);
    const deidFields = extractFieldsDeep(deid);
    const origMap = new Map(origFields.map((r) => [r.field, r.value]));
    const deidMap = new Map(deidFields.map((r) => [r.field, r.value]));

    const changedFields = new Set<string>();
    for (const [field, val] of origMap) {
      const deidVal = deidMap.get(field);
      if (deidVal === undefined || deidVal !== val) changedFields.add(field);
    }
    for (const field of deidMap.keys()) {
      if (!origMap.has(field)) changedFields.add(field);
    }

    // Also include manifest-only paths not captured by the field diff
    for (const field of manifestByField.keys()) {
      changedFields.add(field);
    }

    if (changedFields.size === 0) continue;

    if (!agg.has(resourceType)) agg.set(resourceType, new Map());
    const typeAgg = agg.get(resourceType)!;

    for (const field of changedFields) {
      // The manifest is the ONLY authoritative source of a named action. We do
      // NOT guess actions from the output value's shape — a coding.display that
      // happens to be 64-hex, or a code that looks like a year, must never be
      // mislabelled "cryptohash"/"generalize" when no rule touched it. A field
      // that genuinely changed but has no manifest entry is labelled the neutral
      // "modified" (e.g. a post-processing reference rewrite), never a concrete
      // de-identification action it didn't receive.
      const action = resolveAction(manifestByField, field) ?? "modified";
      const key = `${field}::${action}`;
      typeAgg.set(key, (typeAgg.get(key) ?? 0) + 1);
    }
  }

  const result: PiiDetectionMap = {};
  for (const [resourceType, fields] of agg) {
    const entries: PiiFieldEntry[] = [];
    for (const [key, count] of fields) {
      const sep = key.indexOf("::");
      const fieldPath = key.slice(0, sep);
      const action = key.slice(sep + 2);
      entries.push({ fieldPath, action, count });
    }
    entries.sort(
      (a, b) => b.count - a.count || a.fieldPath.localeCompare(b.fieldPath),
    );
    result[resourceType] = entries;
  }

  return result;
}

/**
 * Build a PII detection map from **only** de-identified resources (no originals).
 *
 * Used by the Bulk De-identify page where original resources are not available.
 * Strategy:
 * 1. Parse the manifest from meta.tag — authoritative when present.
 * 2. Also scan deep-extracted field values for PII signatures to catch
 *    post-processing changes not tracked in the manifest.
 * 3. Aggregate by (resourceType, fieldPath, action).
 */
export function buildPiiFromDeidentifiedOnly(
  resources: Record<string, unknown>[],
): PiiDetectionMap {
  const agg = new Map<string, Map<string, number>>();

  for (const resource of resources) {
    const resourceType = (resource.resourceType as string) ?? "Unknown";
    if (!agg.has(resourceType)) agg.set(resourceType, new Map());
    const typeAgg = agg.get(resourceType)!;

    // Manifest-only: with no original resources to diff against, the
    // transformation manifest (meta.tag) is the SOLE authoritative record of
    // what was de-identified. We intentionally do NOT scan output values for
    // "PII-looking" patterns — that mislabelled untouched fields (a 64-hex
    // coding.display as "cryptohash", a year-like code as "generalize"). When
    // the manifest is disabled (MEDANON_MANIFEST_ENABLED=false) this yields an
    // empty map, which is correct: we cannot truthfully claim any action.
    const manifestEntries = parseManifestEntries(resource);
    const seen = new Set<string>();
    for (const entry of manifestEntries) {
      const field = simplifyPath(entry.path);
      const key = `${field}::${entry.action}`;
      if (!seen.has(key)) {
        seen.add(key);
        typeAgg.set(key, (typeAgg.get(key) ?? 0) + 1);
      }
    }
  }

  const result: PiiDetectionMap = {};
  for (const [resourceType, fields] of agg) {
    const entries: PiiFieldEntry[] = [];
    for (const [key, count] of fields) {
      const sep = key.indexOf("::");
      const fieldPath = key.slice(0, sep);
      const action = key.slice(sep + 2);
      entries.push({ fieldPath, action, count });
    }
    entries.sort(
      (a, b) => b.count - a.count || a.fieldPath.localeCompare(b.fieldPath),
    );
    result[resourceType] = entries;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Full field summary (all fields across all resources, PII fields highlighted)
// ---------------------------------------------------------------------------

interface FieldSummaryEntry {
  fieldPath: string;
  count: number;
  /** Set to the PII action when this field was transformed. */
  action?: string;
}

export type FieldSummaryMap = Record<string, FieldSummaryEntry[]>;

/**
 * Build a full field summary map with PII actions overlaid.
 * Action lookup tries exact match first, then parent key fallback so that
 * a "name" entry covers deep paths like "name.family", "name.given".
 *
 * When `realCounts` is provided, sampled field counts are scaled
 * proportionally so they reflect the true per-type resource totals.
 */
export function buildFieldSummary(
  allFieldCounts: Record<string, Record<string, number>>,
  piiData: PiiDetectionMap,
  realCounts?: Record<string, number>,
): FieldSummaryMap {
  const result: FieldSummaryMap = {};
  for (const [resType, fieldCounts] of Object.entries(allFieldCounts)) {
    const piiActions: Record<string, string> = {};
    if (piiData[resType]) {
      for (const e of piiData[resType]) {
        if (!piiActions[e.fieldPath]) piiActions[e.fieldPath] = e.action;
      }
    }
    // Determine the scale factor: real total / sampled total.
    // The sampled total is the max field count (the field present in every
    // sampled resource), which equals min(realCount, FIELD_SAMPLE_LIMIT).
    const realTotal = realCounts?.[resType];
    const sampledTotal = Math.max(...Object.values(fieldCounts), 1);
    const scale = realTotal && realTotal > sampledTotal
      ? realTotal / sampledTotal
      : 1;

    result[resType] = Object.entries(fieldCounts)
      .map(([fieldPath, count]) => ({
        fieldPath,
        count: scale > 1 ? Math.round(count * scale) : count,
        action:
          piiActions[fieldPath] ?? piiActions[parentKey(fieldPath)],
      }))
      .sort(
        (a, b) => b.count - a.count || a.fieldPath.localeCompare(b.fieldPath),
      );
  }
  return result;
}

/**
 * Strip the transformation manifest tag from a resource's meta.tag array.
 * Returns a shallow clone with the manifest removed so the output/download
 * views don't expose internal bookkeeping.
 */
export function stripManifestTag(
  resource: Record<string, unknown>,
): Record<string, unknown> {
  const meta = resource.meta as Record<string, unknown> | undefined;
  if (!meta) return resource;
  const tags = meta.tag as Array<Record<string, unknown>> | undefined;
  if (!Array.isArray(tags)) return resource;
  const filtered = tags.filter((t) => t.system !== MANIFEST_SYSTEM);
  if (filtered.length === tags.length) return resource;
  const newMeta = { ...meta, tag: filtered.length > 0 ? filtered : undefined };
  const metaKeys = Object.values(newMeta).filter((v) => v !== undefined);
  if (metaKeys.length === 0) {
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const { meta: _meta, ...rest } = resource;
    return rest;
  }
  return { ...resource, meta: newMeta };
}
