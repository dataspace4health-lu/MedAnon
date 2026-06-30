// ---------------------------------------------------------------------------
// Shared FHIR resource field extraction
//
// Extracted from FhirTableView so both the table view and the PII detection
// logic can reuse the same field-walking code.
// ---------------------------------------------------------------------------

interface FieldRow {
  field: string;
  value: string;
}

// `text` (Narrative.div) and `extension` are intentionally NOT skipped — they
// carry identifying content (the rendered narrative, US-Core race/ethnicity,
// patient-mothersMaidenName, birthPlace, geolocation lat/long), so their leaves
// must be visible in the table. Only provenance/version (`meta`), contained
// resources, modifier extensions, and parser noise are dropped.
const SKIP_KEYS = new Set([
  "meta",
  "contained",
  "modifierExtension",
  "implicitRules",
]);

// Keys to skip when recursing into nested objects. Nested `extension` is kept
// so extension-in-extension PHI (race ombCategory, geolocation) still surfaces.
const SKIP_NESTED = new Set([
  "modifierExtension",
  "contained",
  "fhir_comments",
]);

const MAX_DEPTH = 5;

function walkValue(
  rows: FieldRow[],
  key: string,
  value: unknown,
  depth: number,
): void {
  if (depth > MAX_DEPTH || value === null || value === undefined) return;

  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean"
  ) {
    rows.push({ field: key, value: String(value) });
    return;
  }

  if (Array.isArray(value)) {
    if (value.length === 0) return;
    const first = value[0];
    if (
      typeof first === "string" ||
      typeof first === "number" ||
      typeof first === "boolean"
    ) {
      // Scalar array (e.g. name.given: ["John", "A"]) — join values
      rows.push({ field: key, value: value.map(String).join(", ") });
    } else {
      // Object array — walk EVERY element so heterogeneous siblings all
      // surface their leaves (e.g. identifier[0] has no `type`, but
      // identifier[1].type.coding.code does). Paths stay index-free so they
      // still match the de-identified resource and the transformation
      // manifest; the dedup pass below keeps the first value per dotted path.
      for (const item of value) {
        if (item && typeof item === "object") walkValue(rows, key, item, depth);
      }
    }
    return;
  }

  if (typeof value === "object") {
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (SKIP_NESTED.has(k)) continue;
      walkValue(rows, `${key}.${k}`, v, depth + 1);
    }
  }
}

/**
 * Deep field extraction — produces dotted sub-paths such as
 * "name.family", "address.city", "code.coding.display", "subject.reference".
 * Array fields walk the first element only; duplicate paths are deduplicated.
 */
export function extractFieldsDeep(
  resource: Record<string, unknown>,
): FieldRow[] {
  const rows: FieldRow[] = [];
  for (const [key, value] of Object.entries(resource)) {
    if (SKIP_KEYS.has(key)) continue;
    if (value === null || value === undefined) continue;
    walkValue(rows, key, value, 0);
  }
  // Keep first occurrence of each dotted path
  const seen = new Set<string>();
  return rows.filter((r) => {
    if (seen.has(r.field)) return false;
    seen.add(r.field);
    return true;
  });
}
