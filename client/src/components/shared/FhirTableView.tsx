import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

// ---------------------------------------------------------------------------
// FHIR resource field extraction
// ---------------------------------------------------------------------------

interface FieldRow {
  field: string;
  value: string;
}

function extractFromObject(
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

const SKIP_KEYS = new Set([
  "meta",
  "text",
  "extension",
  "contained",
  "modifierExtension",
  "implicitRules",
]);

function extractFields(resource: Record<string, unknown>): FieldRow[] {
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

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface FhirTableViewProps {
  originalResources: Record<string, unknown>[];
  resources: Record<string, unknown>[];
}

export function FhirTableView({
  originalResources,
  resources,
}: FhirTableViewProps) {
  if (originalResources.length === 0 && resources.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-4 text-center">
        No resources to display.
      </p>
    );
  }

  const pairs = resources.map((deided, idx) => ({
    original: originalResources[idx] ?? {},
    deided,
  }));

  return (
    <div className="flex flex-col gap-6">
      {pairs.map(({ original, deided }, idx) => {
        const resourceType = String(
          deided.resourceType ?? original.resourceType ?? "Resource",
        );
        const resourceId = String(deided.id ?? original.id ?? idx);

        const origFields = extractFields(original);
        const deidFields = extractFields(deided);

        // Build a unified field list from original; fall back to deid-only fields
        const origMap = new Map(origFields.map((r) => [r.field, r.value]));
        const deidMap = new Map(deidFields.map((r) => [r.field, r.value]));
        const allFields = [
          ...origFields.map((r) => r.field),
          ...deidFields
            .filter((r) => !origMap.has(r.field))
            .map((r) => r.field),
        ];

        return (
          <div key={`${resourceType}-${resourceId}-${idx}`}>
            <p className="mb-2 text-sm font-semibold text-foreground">
              {resourceType}{" "}
              <span className="font-mono text-muted-foreground">
                ({resourceId})
              </span>
            </p>
            <div className="rounded-lg border overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow className="bg-muted/50">
                    <TableHead className="w-1/4 font-semibold text-xs uppercase tracking-wide">
                      Field
                    </TableHead>
                    <TableHead className="w-[37.5%] font-semibold text-xs uppercase tracking-wide">
                      Value
                    </TableHead>
                    <TableHead className="w-[37.5%] font-semibold text-xs uppercase tracking-wide">
                      De-identified Value
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {allFields.map((field) => {
                    const origVal = origMap.get(field) ?? "";
                    const deidVal = deidMap.get(field) ?? "";
                    const changed = origVal !== deidVal;
                    return (
                      <TableRow key={field}>
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          {field}
                        </TableCell>
                        <TableCell className="text-sm">{origVal}</TableCell>
                        <TableCell
                          className={`text-sm ${
                            changed
                              ? "bg-amber-50 text-amber-800 font-medium"
                              : ""
                          }`}
                        >
                          {deidVal}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          </div>
        );
      })}
    </div>
  );
}
