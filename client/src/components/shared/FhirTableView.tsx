import { useState } from "react";
import { ChevronRight } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { extractFieldsDeep } from "@/lib/fhirFields";
import type { PiiDetectionMap } from "@/lib/piiDetection";

// ---------------------------------------------------------------------------
// Action colour palette
// ---------------------------------------------------------------------------

const ACTION_STYLES: Record<
  string,
  { row: string; badge: string; label: string }
> = {
  redact:            { row: "bg-rose-50",   badge: "bg-rose-100 text-rose-700",     label: "Redacted"      },
  substitute:        { row: "bg-orange-50", badge: "bg-orange-100 text-orange-700", label: "Substituted"   },
  cryptohash:        { row: "bg-blue-50",   badge: "bg-blue-100 text-blue-700",     label: "Cryptohash"    },
  generalize:        { row: "bg-amber-50",  badge: "bg-amber-100 text-amber-700",   label: "Generalized"   },
  gpas_pseudonymize: { row: "bg-purple-50", badge: "bg-purple-100 text-purple-700", label: "Pseudonymized" },
  encrypt:           { row: "bg-indigo-50", badge: "bg-indigo-100 text-indigo-700", label: "Encrypted"     },
  perturb:           { row: "bg-teal-50",   badge: "bg-teal-100 text-teal-700",     label: "Perturbed"     },
  scrub_text:        { row: "bg-pink-50",   badge: "bg-pink-100 text-pink-700",     label: "Scrubbed"      },
  nlp_detect:        { row: "bg-pink-50",   badge: "bg-pink-100 text-pink-700",     label: "NLP scrub"     },
  modified:          { row: "bg-slate-50",  badge: "bg-slate-100 text-slate-600",   label: "Modified"      },
};

function getStyle(action: string) {
  return ACTION_STYLES[action] ?? ACTION_STYLES.modified;
}

// ---------------------------------------------------------------------------
// Field grouping
// ---------------------------------------------------------------------------

interface FieldEntry {
  field: string;
  subField: string;
  origVal: string;
  deidVal: string;
  changed: boolean;
  action?: string;
}

interface FieldGroup {
  parentKey: string;
  isGroup: boolean;
  entries: FieldEntry[];
  changed: boolean;
  dominantAction?: string;
}

function buildGroups(
  allFields: string[],
  origMap: Map<string, string>,
  deidMap: Map<string, string>,
  resolveAction: (f: string) => string | undefined,
): FieldGroup[] {
  const order: string[] = [];
  const map = new Map<string, FieldEntry[]>();

  for (const field of allFields) {
    const dot = field.indexOf(".");
    const parent = dot >= 0 ? field.slice(0, dot) : field;
    const sub = dot >= 0 ? field.slice(dot + 1) : field;
    const origVal = origMap.get(field) ?? "";
    const deidVal = deidMap.get(field) ?? "";
    const changed = origVal !== deidVal;
    const action = resolveAction(field) ?? (changed ? "modified" : undefined);

    if (!map.has(parent)) { map.set(parent, []); order.push(parent); }
    map.get(parent)!.push({ field, subField: sub, origVal, deidVal, changed, action });
  }

  return order.map((parentKey) => {
    const entries = map.get(parentKey)!;
    const isGroup = entries.some((e) => e.field !== parentKey);
    const changed = entries.some((e) => e.changed);
    // Pick the most specific (non-modified) action as the group label
    const specific = entries
      .map((e) => e.action)
      .find((a) => a && a !== "modified");
    const dominantAction = specific ?? (changed ? "modified" : undefined);
    return { parentKey, isGroup, entries, changed, dominantAction };
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface FhirTableViewProps {
  originalResources: Record<string, unknown>[];
  resources: Record<string, unknown>[];
  piiData?: PiiDetectionMap;
}

export function FhirTableView({
  originalResources,
  resources,
  piiData,
}: FhirTableViewProps) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  if (originalResources.length === 0 && resources.length === 0) {
    return (
      <p className="text-sm text-muted-foreground py-4 text-center">
        No resources to display.
      </p>
    );
  }

  const toggle = (key: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });

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
        const resourceKey = `${resourceType}-${resourceId}-${idx}`;

        const origFields = extractFieldsDeep(original);
        const deidFields = extractFieldsDeep(deided);
        const origMap = new Map(origFields.map((r) => [r.field, r.value]));
        const deidMap = new Map(deidFields.map((r) => [r.field, r.value]));

        // Build action lookup from piiData (exact + parent fallback)
        const actionByField = new Map<string, string>();
        if (piiData?.[resourceType]) {
          for (const e of piiData[resourceType]) {
            if (!actionByField.has(e.fieldPath)) actionByField.set(e.fieldPath, e.action);
          }
        }
        const resolveAction = (field: string) => {
          if (actionByField.has(field)) return actionByField.get(field)!;
          const parent = field.includes(".") ? field.split(".")[0] : field;
          return actionByField.get(parent);
        };

        const allFields = [
          ...origFields.map((r) => r.field),
          ...deidFields.filter((r) => !origMap.has(r.field)).map((r) => r.field),
        ];

        const groups = buildGroups(allFields, origMap, deidMap, resolveAction);
        const hasPii = groups.some((g) => g.changed);

        return (
          <div key={resourceKey}>
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
                    <TableHead className="w-[28%] font-semibold text-xs uppercase tracking-wide">
                      Field
                    </TableHead>
                    <TableHead className="font-semibold text-xs uppercase tracking-wide">
                      Original Value
                    </TableHead>
                    <TableHead className="font-semibold text-xs uppercase tracking-wide">
                      De-identified Value
                    </TableHead>
                    {hasPii && (
                      <TableHead className="w-32 font-semibold text-xs uppercase tracking-wide">
                        Action
                      </TableHead>
                    )}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {groups.map((group) => {
                    const gKey = `${resourceKey}::${group.parentKey}`;
                    const isOpen = expanded.has(gKey);
                    const gStyle = group.dominantAction
                      ? getStyle(group.dominantAction)
                      : null;

                    if (!group.isGroup) {
                      // ── Single flat row ─────────────────────────────
                      const e = group.entries[0];
                      const style = e.action ? getStyle(e.action) : null;
                      return (
                        <TableRow
                          key={group.parentKey}
                          className={style ? style.row : undefined}
                        >
                          <TableCell className="font-mono text-xs text-muted-foreground">
                            {group.parentKey}
                          </TableCell>
                          <TableCell className="text-sm">
                            {String(e.origVal)}
                          </TableCell>
                          <TableCell className="text-sm font-medium">
                            {e.changed ? String(e.deidVal) : ""}
                          </TableCell>
                          {hasPii && (
                            <TableCell>
                              {e.action && (
                                <span
                                  className={cn(
                                    "inline-block rounded px-2 py-0.5 text-xs font-medium",
                                    getStyle(e.action).badge,
                                  )}
                                >
                                  {getStyle(e.action).label}
                                </span>
                              )}
                            </TableCell>
                          )}
                        </TableRow>
                      );
                    }

                    // ── Group header + collapsible sub-rows ──────────
                    const changedCount = group.entries.filter(
                      (e) => e.changed,
                    ).length;

                    return [
                      // Group header row
                      <TableRow
                        key={`${group.parentKey}--header`}
                        className={cn(
                          "cursor-pointer select-none hover:brightness-95 transition-colors",
                          gStyle ? gStyle.row : "bg-muted/20",
                        )}
                        onClick={() => toggle(gKey)}
                      >
                        <TableCell className="font-mono text-xs font-semibold">
                          <span className="flex items-center gap-1">
                            <ChevronRight
                              className={cn(
                                "size-3 shrink-0 text-muted-foreground transition-transform",
                                isOpen && "rotate-90",
                              )}
                            />
                            {group.parentKey}
                            <span className="ml-1 text-[10px] font-normal text-muted-foreground">
                              ({group.entries.length} field
                              {group.entries.length !== 1 ? "s" : ""}
                              {changedCount > 0 &&
                                `, ${changedCount} changed`}
                              )
                            </span>
                          </span>
                        </TableCell>
                        <TableCell />
                        <TableCell />
                        {hasPii && (
                          <TableCell>
                            {group.dominantAction && (
                              <span
                                className={cn(
                                  "inline-block rounded px-2 py-0.5 text-xs font-medium",
                                  getStyle(group.dominantAction).badge,
                                )}
                              >
                                {getStyle(group.dominantAction).label}
                              </span>
                            )}
                          </TableCell>
                        )}
                      </TableRow>,

                      // Sub-rows (shown when expanded)
                      ...(isOpen
                        ? group.entries.map((e) => {
                            const style = e.action ? getStyle(e.action) : null;
                            return (
                              <TableRow
                                key={e.field}
                                className={cn(
                                  "border-l-2 border-l-muted/40",
                                  style ? style.row : undefined,
                                )}
                              >
                                <TableCell className="pl-6 font-mono text-xs text-muted-foreground">
                                  {e.subField}
                                </TableCell>
                                <TableCell className="text-sm">
                                  {String(e.origVal)}
                                </TableCell>
                                <TableCell className="text-sm font-medium">
                                  {e.changed ? String(e.deidVal) : ""}
                                </TableCell>
                                {hasPii && (
                                  <TableCell>
                                    {e.action && (
                                      <span
                                        className={cn(
                                          "inline-block rounded px-2 py-0.5 text-xs font-medium",
                                          getStyle(e.action).badge,
                                        )}
                                      >
                                        {getStyle(e.action).label}
                                      </span>
                                    )}
                                  </TableCell>
                                )}
                              </TableRow>
                            );
                          })
                        : []),
                    ];
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
