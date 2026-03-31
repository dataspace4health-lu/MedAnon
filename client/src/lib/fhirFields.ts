// ---------------------------------------------------------------------------
// Shared FHIR resource field extraction
//
// Extracted from FhirTableView so both the table view and the PII detection
// logic can reuse the same field-walking code.
// ---------------------------------------------------------------------------

export interface FieldRow {
  field: string;
  value: string;
}

export const SKIP_KEYS = new Set([
  "meta",
  "text",
  "extension",
  "contained",
  "modifierExtension",
  "implicitRules",
]);

export function extractFromObject(
  rows: FieldRow[],
  key: string,
  obj: Record<string, unknown>,
): void {
  // CodeableConcept: { coding: [{ code, display, system }], text }
  if (Array.isArray(obj.coding) && obj.coding.length > 0) {
    const c = obj.coding[0] as Record<string, unknown>;
    if (c.code !== undefined) rows.push({ field: key, value: String(c.code) });
    if (c.display !== undefined)
      rows.push({ field: "display", value: String(c.display) });
    return;
  }
  if (obj.text !== undefined) {
    rows.push({ field: key, value: String(obj.text) });
    return;
  }
  // Reference: { reference: "Patient/123" }
  if (obj.reference !== undefined) {
    rows.push({ field: key, value: String(obj.reference) });
    return;
  }
  // Quantity / Range: { value, unit } or { value, code }
  if (obj.value !== undefined) {
    const fieldName = key.startsWith("value") ? "value" : key;
    rows.push({ field: fieldName, value: String(obj.value) });
    if (obj.unit !== undefined)
      rows.push({ field: "unit", value: String(obj.unit) });
    else if (obj.code !== undefined)
      rows.push({ field: "unit", value: String(obj.code) });
    return;
  }
  // Period: { start, end }
  if (obj.start !== undefined || obj.end !== undefined) {
    if (obj.start !== undefined)
      rows.push({ field: `${key}.start`, value: String(obj.start) });
    if (obj.end !== undefined)
      rows.push({ field: `${key}.end`, value: String(obj.end) });
    return;
  }
  // HumanName: { family, given }
  if (obj.family !== undefined || obj.given !== undefined) {
    const parts = [
      ...((obj.given as string[] | undefined) ?? []),
      (obj.family as string | undefined) ?? "",
    ].filter(Boolean);
    rows.push({ field: key, value: parts.join(" ") });
    return;
  }
  // Address: { line, city, state, postalCode }
  if (obj.city !== undefined || obj.postalCode !== undefined) {
    const parts = [
      ((obj.line as string[] | undefined) ?? []).join(", "),
      obj.city,
      obj.state,
      obj.postalCode,
      obj.country,
    ]
      .filter(Boolean)
      .map(String);
    rows.push({ field: key, value: parts.join(", ") });
    return;
  }
  // Identifier: { system, value }
  if (obj.system !== undefined && "value" in obj) {
    rows.push({ field: key, value: String(obj["value"]) });
    return;
  }
  // Generic fallback: stringify scalar sub-fields
  const str = Object.entries(obj)
    .filter(
      ([, v]) =>
        typeof v === "string" || typeof v === "number" || typeof v === "boolean",
    )
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
  if (str) rows.push({ field: key, value: str });
}

export function extractFields(resource: Record<string, unknown>): FieldRow[] {
  const rows: FieldRow[] = [];

  for (const [key, value] of Object.entries(resource)) {
    if (SKIP_KEYS.has(key)) continue;
    if (value === null || value === undefined) continue;

    if (
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean"
    ) {
      rows.push({ field: key, value: String(value) });
    } else if (Array.isArray(value)) {
      if (value.length === 0) continue;
      const first = value[0];
      if (
        typeof first === "string" ||
        typeof first === "number" ||
        typeof first === "boolean"
      ) {
        rows.push({ field: key, value: value.map(String).join(", ") });
      } else if (first && typeof first === "object") {
        extractFromObject(rows, key, first as Record<string, unknown>);
      }
    } else if (typeof value === "object") {
      extractFromObject(rows, key, value as Record<string, unknown>);
    }
  }

  return rows;
}
