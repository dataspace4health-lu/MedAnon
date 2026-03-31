// ---------------------------------------------------------------------------
// PII detection: combines backend manifest + client-side field diff
// ---------------------------------------------------------------------------

import { extractFields } from "./fhirFields";

const MANIFEST_SYSTEM = "https://medanon.local/transformation-manifest";

export interface PiiFieldEntry {
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
 * Simplify a FHIRPath manifest path to a top-level field key.
 * e.g. "Patient.name[0].family" → "name"
 *      "Observation.effectiveDateTime" → "effectiveDateTime"
 */
function simplifyPath(fullPath: string): string {
  // Strip resourceType prefix
  const dotIdx = fullPath.indexOf(".");
  const rest = dotIdx >= 0 ? fullPath.slice(dotIdx + 1) : fullPath;
  // Take the first segment (before any . or [)
  const match = rest.match(/^([^.[]+)/);
  return match?.[1] ?? rest;
}

/**
 * Build a PII detection map from paired original + de-identified resources.
 *
 * Strategy:
 * 1. Parse manifest entries from the de-identified resource (authoritative).
 * 2. Diff extracted fields to find changed/removed fields (fallback).
 * 3. For each changed field, prefer the manifest action label; if no manifest
 *    entry covers that field, label it "modified".
 * 4. Aggregate by (resourceType, fieldPath, action) and count occurrences.
 */
export function buildPiiDetectionMap(
  originals: Record<string, unknown>[],
  deidentified: Record<string, unknown>[],
): PiiDetectionMap {
  // Aggregate: resourceType → (fieldPath::action → count)
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
    const manifestByField = new Map<string, string>(); // simplified field → action
    for (const entry of manifestEntries) {
      const simple = simplifyPath(entry.path);
      // Keep first action seen per field (rules are ordered by priority)
      if (!manifestByField.has(simple)) {
        manifestByField.set(simple, entry.action);
      }
    }

    // Diff extracted fields
    const origFields = extractFields(orig);
    const deidFields = extractFields(deid);
    const origMap = new Map(origFields.map((r) => [r.field, r.value]));
    const deidMap = new Map(deidFields.map((r) => [r.field, r.value]));

    // Collect all fields that differ
    const changedFields = new Set<string>();
    for (const [field, val] of origMap) {
      const deidVal = deidMap.get(field);
      if (deidVal === undefined || deidVal !== val) changedFields.add(field);
    }
    for (const field of deidMap.keys()) {
      if (!origMap.has(field)) changedFields.add(field);
    }

    // Also include manifest-only fields not captured by the shallow diff
    for (const field of manifestByField.keys()) {
      changedFields.add(field);
    }

    if (changedFields.size === 0) continue;

    if (!agg.has(resourceType)) agg.set(resourceType, new Map());
    const typeAgg = agg.get(resourceType)!;

    for (const field of changedFields) {
      const action = manifestByField.get(field) ?? "modified";
      const key = `${field}::${action}`;
      typeAgg.set(key, (typeAgg.get(key) ?? 0) + 1);
    }
  }

  // Convert to sorted PiiDetectionMap
  const result: PiiDetectionMap = {};
  for (const [resourceType, fields] of agg) {
    const entries: PiiFieldEntry[] = [];
    for (const [key, count] of fields) {
      const [fieldPath, action] = key.split("::");
      entries.push({ fieldPath, action, count });
    }
    entries.sort((a, b) => b.count - a.count || a.fieldPath.localeCompare(b.fieldPath));
    result[resourceType] = entries;
  }

  return result;
}

// ---------------------------------------------------------------------------
// Heuristic action classifier — used when no manifest is available and
// there are no original resources to diff against (bulk export results).
// ---------------------------------------------------------------------------

const HEX_64 = /^[0-9a-f]{64}$/;
const TOKEN_PATTERN = /\[\[[A-Z_]+_\d+]]/;

function classifyValue(value: string): string | null {
  if (value === "[REDACTED]" || value === "REDACTED") return "redact";
  if (HEX_64.test(value)) return "cryptohash";
  if (TOKEN_PATTERN.test(value)) return "scrub_text";
  // Year-only date (generalize date_year)
  if (/^\d{4}$/.test(value)) return "generalize";
  return null;
}

/**
 * Build a PII detection map from **only** de-identified resources (no originals).
 *
 * Used by the Bulk De-identify page where original resources are not available.
 * Strategy:
 * 1. Parse the manifest from meta.tag — authoritative when present.
 * 2. When no manifest, scan extracted field values for PII signatures
 *    (hashes, [REDACTED], [[TYPE_N]] tokens, year-only dates).
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

    // Try manifest first
    const manifestEntries = parseManifestEntries(resource);
    if (manifestEntries.length > 0) {
      const seen = new Set<string>();
      for (const entry of manifestEntries) {
        const field = simplifyPath(entry.path);
        const key = `${field}::${entry.action}`;
        if (!seen.has(key)) {
          seen.add(key);
          typeAgg.set(key, (typeAgg.get(key) ?? 0) + 1);
        }
      }
      continue;
    }

    // Fallback: heuristic scan of field values
    const fields = extractFields(resource);
    for (const { field, value } of fields) {
      if (field === "resourceType" || field === "id") continue;
      const action = classifyValue(value);
      if (action) {
        const key = `${field}::${action}`;
        typeAgg.set(key, (typeAgg.get(key) ?? 0) + 1);
      }
    }
  }

  // Convert to sorted PiiDetectionMap
  const result: PiiDetectionMap = {};
  for (const [resourceType, fields] of agg) {
    const entries: PiiFieldEntry[] = [];
    for (const [key, count] of fields) {
      const [fieldPath, action] = key.split("::");
      entries.push({ fieldPath, action, count });
    }
    entries.sort((a, b) => b.count - a.count || a.fieldPath.localeCompare(b.fieldPath));
    result[resourceType] = entries;
  }

  return result;
}
