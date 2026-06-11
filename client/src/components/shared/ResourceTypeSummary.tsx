import { useState } from "react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import type { PiiDetectionMap, FieldSummaryMap } from "@/lib/piiDetection";

interface ResourceTypeSummaryProps {
  counts: Record<string, number>;
  piiData?: PiiDetectionMap;
  fieldSummary?: FieldSummaryMap;
}

// Deterministic color per resource type
const TYPE_COLORS: Record<string, string> = {
  Patient: "bg-blue-500",
  Observation: "bg-emerald-500",
  Condition: "bg-amber-500",
  MedicationRequest: "bg-teal-500",
  Encounter: "bg-orange-500",
  Procedure: "bg-pink-500",
  DiagnosticReport: "bg-teal-500",
  AllergyIntolerance: "bg-red-400",
  Immunization: "bg-lime-500",
};

function getBarColor(type: string, index: number): string {
  if (TYPE_COLORS[type]) return TYPE_COLORS[type];
  const FALLBACKS = [
    "bg-slate-400",
    "bg-cyan-500",
    "bg-sky-600",
    "bg-rose-400",
    "bg-blue-500",
  ];
  return FALLBACKS[index % FALLBACKS.length];
}

const ACTION_COLORS: Record<string, string> = {
  redact: "bg-red-100 text-red-800",
  cryptohash: "bg-blue-100 text-blue-800",
  generalize: "bg-amber-100 text-amber-800",
  gpas_pseudonymize: "bg-teal-100 text-teal-800",
  encrypt: "bg-blue-100 text-blue-800",
  perturb: "bg-teal-100 text-teal-800",
  substitute: "bg-orange-100 text-orange-800",
  scrub_text: "bg-pink-100 text-pink-800",
  nlp_scrub: "bg-pink-100 text-pink-800",
  nlp_detect: "bg-pink-100 text-pink-800",  // backward compat
  modified: "bg-slate-100 text-slate-700",
};

function actionBadgeClass(action: string): string {
  return ACTION_COLORS[action] ?? ACTION_COLORS.modified;
}

// ---------------------------------------------------------------------------
// Field grouping — collapse dotted sub-paths under their parent key
// ---------------------------------------------------------------------------

type AnyEntry = { fieldPath: string; count: number; action?: string };

interface FieldGroup {
  parentKey: string;
  isGroup: boolean;
  entries: AnyEntry[];
  piiCount: number;
  dominantAction: string | undefined;
  maxCount: number;
}

function buildGroups(entries: AnyEntry[]): FieldGroup[] {
  const order: string[] = [];
  const map = new Map<string, AnyEntry[]>();

  for (const entry of entries) {
    const dot = entry.fieldPath.indexOf(".");
    const parent = dot >= 0 ? entry.fieldPath.slice(0, dot) : entry.fieldPath;
    if (!map.has(parent)) {
      map.set(parent, []);
      order.push(parent);
    }
    map.get(parent)!.push(entry);
  }

  return order.map((parentKey) => {
    const groupEntries = map.get(parentKey)!;
    const isGroup = groupEntries.some((e) => e.fieldPath !== parentKey);
    const piiEntries = groupEntries.filter((e) => !!e.action);
    // Within a group: treated fields first, then by count desc
    groupEntries.sort((a, b) => {
      if (!!a.action !== !!b.action) return a.action ? -1 : 1;
      return b.count - a.count;
    });
    return {
      parentKey,
      isGroup,
      entries: groupEntries,
      piiCount: piiEntries.length,
      dominantAction: piiEntries[0]?.action,
      maxCount: Math.max(...groupEntries.map((e) => e.count)),
    };
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ResourceTypeSummary({
  counts,
  piiData,
  fieldSummary,
}: ResourceTypeSummaryProps) {
  const sorted = Object.entries(counts).sort(([, a], [, b]) => b - a);
  // Single Set for both type-level and sub-group-level expansion keys
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  if (sorted.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No resources to display.</p>
    );
  }

  const total = sorted.reduce((sum, [, count]) => sum + count, 0);

  const toggle = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <div className="rounded-lg border overflow-hidden">
      <div className="bg-muted/30 px-3 py-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Resource Summary · {total} total
      </div>
      <div className="divide-y">
        {sorted.map(([type, count], index) => {
          const pct = total > 0 ? (count / total) * 100 : 0;
          const barColor = getBarColor(type, index);

          // Prefer fieldSummary (full field list), fall back to pii-only
          const fieldRows: AnyEntry[] | undefined = fieldSummary?.[type];
          const piiEntries: AnyEntry[] | undefined = piiData?.[type];
          const rows: AnyEntry[] = fieldRows ?? piiEntries ?? [];
          const hasExpansion = rows.length > 0;
          const isExpanded = expanded.has(type);

          const piiCount = fieldRows
            ? fieldRows.filter((f) => f.action).length
            : (piiEntries?.length ?? 0);

          return (
            <div key={type}>
              {/* ── Type header row ── */}
              <div
                className={cn(
                  "flex items-center gap-3 px-3 py-2.5",
                  hasExpansion &&
                    "cursor-pointer hover:bg-muted/20 transition-colors",
                )}
                onClick={hasExpansion ? () => toggle(type) : undefined}
              >
                {hasExpansion ? (
                  <ChevronRight
                    className={cn(
                      "size-3.5 shrink-0 text-muted-foreground transition-transform",
                      isExpanded && "rotate-90",
                    )}
                  />
                ) : (
                  <span className="size-3.5 shrink-0" />
                )}

                <span className={cn("size-2 shrink-0 rounded-full", barColor)} />

                <span className="min-w-0 flex-1 truncate text-sm font-medium">
                  {type}
                </span>

                {piiCount > 0 && (
                  <span className="hidden sm:inline-block rounded px-1.5 py-0.5 text-[10px] font-medium bg-rose-50 text-rose-700 border border-rose-200">
                    {piiCount} treated
                  </span>
                )}

                <div className="hidden w-24 sm:flex">
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                    <div
                      className={cn("h-full rounded-full transition-all", barColor)}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>

                <span className="w-10 text-right text-xs text-muted-foreground tabular-nums">
                  {pct.toFixed(0)}%
                </span>

                <span className="w-6 text-right text-sm font-semibold tabular-nums">
                  {count}
                </span>
              </div>

              {/* ── Expanded field table ── */}
              {isExpanded && rows.length > 0 && (() => {
                const groups = buildGroups(rows);
                // Treated groups first, then by maxCount desc
                groups.sort((a, b) => {
                  if ((a.piiCount > 0) !== (b.piiCount > 0))
                    return a.piiCount > 0 ? -1 : 1;
                  return b.maxCount - a.maxCount;
                });

                return (
                  <div className="border-t bg-muted/10 px-4 py-2">
                    <table className="w-full text-xs">
                      <thead>
                        <tr className="text-muted-foreground">
                          <th className="pb-1.5 text-left font-medium">Field</th>
                          <th className="pb-1.5 text-left font-medium">Action</th>
                          <th className="pb-1.5 text-right font-medium">
                            Resources
                          </th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-muted/40">
                        {groups.flatMap((group) => {
                          const groupKey = `${type}::${group.parentKey}`;
                          const isGroupOpen = expanded.has(groupKey);

                          if (!group.isGroup) {
                            // ── Flat single-field row ──
                            const entry = group.entries[0];
                            return [
                              <tr
                                key={group.parentKey}
                                className={
                                  entry.action ? "bg-rose-50/40" : undefined
                                }
                              >
                                <td className="py-1 font-mono text-foreground">
                                  {group.parentKey}
                                </td>
                                <td className="py-1">
                                  {entry.action ? (
                                    <span
                                      className={cn(
                                        "inline-block rounded px-1.5 py-0.5 text-[10px] font-medium",
                                        actionBadgeClass(entry.action),
                                      )}
                                    >
                                      {entry.action}
                                    </span>
                                  ) : (
                                    <span className="text-muted-foreground/40">
                                      —
                                    </span>
                                  )}
                                </td>
                                <td className="py-1 text-right tabular-nums text-muted-foreground">
                                  {entry.count}
                                </td>
                              </tr>,
                            ];
                          }

                          // ── Collapsible group header + sub-rows ──
                          const label = `${group.entries.length} field${group.entries.length !== 1 ? "s" : ""}${group.piiCount > 0 ? `, ${group.piiCount} treated` : ""}`;
                          return [
                            <tr
                              key={`${group.parentKey}--header`}
                              className={cn(
                                "cursor-pointer select-none hover:bg-muted/30 transition-colors",
                                group.piiCount > 0 && "bg-rose-50/40",
                              )}
                              onClick={() => toggle(groupKey)}
                            >
                              <td className="py-1 font-mono font-semibold">
                                <span className="flex items-center gap-1">
                                  <ChevronRight
                                    className={cn(
                                      "size-3 shrink-0 text-muted-foreground transition-transform",
                                      isGroupOpen && "rotate-90",
                                    )}
                                  />
                                  {group.parentKey}
                                  <span className="ml-1 text-[10px] font-normal text-muted-foreground">
                                    ({label})
                                  </span>
                                </span>
                              </td>
                              <td className="py-1">
                                {group.dominantAction ? (
                                  <span
                                    className={cn(
                                      "inline-block rounded px-1.5 py-0.5 text-[10px] font-medium",
                                      actionBadgeClass(group.dominantAction),
                                    )}
                                  >
                                    {group.dominantAction}
                                  </span>
                                ) : (
                                  <span className="text-muted-foreground/40">
                                    —
                                  </span>
                                )}
                              </td>
                              <td className="py-1 text-right tabular-nums text-muted-foreground">
                                {group.maxCount}
                              </td>
                            </tr>,

                            ...(isGroupOpen
                              ? group.entries.map((entry) => {
                                  const subField = entry.fieldPath.includes(".")
                                    ? entry.fieldPath.slice(
                                        entry.fieldPath.indexOf(".") + 1,
                                      )
                                    : entry.fieldPath;
                                  return (
                                    <tr
                                      key={entry.fieldPath}
                                      className={cn(
                                        "border-l-2 border-l-muted/40",
                                        entry.action && "bg-rose-50/30",
                                      )}
                                    >
                                      <td className="py-1 pl-6 font-mono text-muted-foreground">
                                        {subField}
                                      </td>
                                      <td className="py-1">
                                        {entry.action ? (
                                          <span
                                            className={cn(
                                              "inline-block rounded px-1.5 py-0.5 text-[10px] font-medium",
                                              actionBadgeClass(entry.action),
                                            )}
                                          >
                                            {entry.action}
                                          </span>
                                        ) : (
                                          <span className="text-muted-foreground/40">
                                            —
                                          </span>
                                        )}
                                      </td>
                                      <td className="py-1 text-right tabular-nums text-muted-foreground">
                                        {entry.count}
                                      </td>
                                    </tr>
                                  );
                                })
                              : []),
                          ];
                        })}
                      </tbody>
                    </table>
                  </div>
                );
              })()}
            </div>
          );
        })}
      </div>
    </div>
  );
}
