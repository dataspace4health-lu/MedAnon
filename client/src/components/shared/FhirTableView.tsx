import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { extractFields } from "@/lib/fhirFields";

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
