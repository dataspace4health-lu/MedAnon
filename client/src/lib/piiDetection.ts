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
      const action =
        resolveAction(manifestByField, field) ??
        classifyValue(deidMap.get(field) ?? "") ??
        (origMap.has(field) && !deidMap.has(field) ? "redact" : null) ??
        "modified";
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

// ---------------------------------------------------------------------------
// Heuristic action classifier — used when no manifest is available and
// there are no original resources to diff against (bulk export results).
// Also supplements manifest data for post-processing changes not tracked.
// ---------------------------------------------------------------------------

const HEX_64 = /^[0-9a-f]{64}$/;
const TOKEN_PATTERN = /\[\[[A-Z_]+_\d+]]/;
const YEAR_MONTH = /^\d{4}-\d{2}$/;
const REDACTED_RE = /^\[REDACTED]$/;

function classifyValue(value: string): string | null {
  if (!value) return null;
  // Handle comma-joined array values like "[REDACTED], [REDACTED]"
  const parts = value.split(", ");
  if (parts.every((p) => REDACTED_RE.test(p.trim()) || p.trim() === "REDACTED"))
    return "redact";
  if (HEX_64.test(value)) return "cryptohash";
  if (TOKEN_PATTERN.test(value)) return "scrub_text";
  if (/^\d{4}$/.test(value)) return "generalize";
  if (YEAR_MONTH.test(value)) return "generalize";
  return null;
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

    const manifestEntries = parseManifestEntries(resource);
    const manifestFields = new Set<string>();
    if (manifestEntries.length > 0) {
      const seen = new Set<string>();
      for (const entry of manifestEntries) {
        const field = simplifyPath(entry.path);
        manifestFields.add(field);
        const key = `${field}::${entry.action}`;
        if (!seen.has(key)) {
          seen.add(key);
          typeAgg.set(key, (typeAgg.get(key) ?? 0) + 1);
        }
      }
    }

    // Heuristic scan on deep fields not covered by manifest or their parent
    const fields = extractFieldsDeep(resource);
    for (const { field, value } of fields) {
      if (field === "resourceType") continue;
      if (
        manifestFields.has(field) ||
        manifestFields.has(parentKey(field))
      )
        continue;
      const action = classifyValue(value);
      if (action) {
        const key = `${field}::${action}`;
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
