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
  MedicationRequest: "bg-purple-500",
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
    "bg-violet-500",
    "bg-rose-400",
    "bg-indigo-400",
  ];
  return FALLBACKS[index % FALLBACKS.length];
}

const ACTION_COLORS: Record<string, string> = {
  redact: "bg-red-100 text-red-800",
  cryptohash: "bg-blue-100 text-blue-800",
  generalize: "bg-amber-100 text-amber-800",
  gpas_pseudonymize: "bg-purple-100 text-purple-800",
  encrypt: "bg-indigo-100 text-indigo-800",
  perturb: "bg-teal-100 text-teal-800",
  substitute: "bg-orange-100 text-orange-800",
  scrub_text: "bg-pink-100 text-pink-800",
  nlp_detect: "bg-pink-100 text-pink-800",
  modified: "bg-slate-100 text-slate-700",
};

function actionBadgeClass(action: string): string {
  return ACTION_COLORS[action] ?? ACTION_COLORS.modified;
}

export function ResourceTypeSummary({
  counts,
  piiData,
  fieldSummary,
}: ResourceTypeSummaryProps) {
  const sorted = Object.entries(counts).sort(([, a], [, b]) => b - a);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  if (sorted.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No resources to display.</p>
    );
  }

  const total = sorted.reduce((sum, [, count]) => sum + count, 0);

  const toggle = (type: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
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

          // Prefer fieldSummary (full field list), fall back to pii-only for backwards compat
          const fieldRows = fieldSummary?.[type];
          const piiEntries = piiData?.[type];
          const hasExpansion =
            (fieldRows && fieldRows.length > 0) ||
            (piiEntries && piiEntries.length > 0);
          const isExpanded = expanded.has(type);

          const piiCount = fieldRows
            ? fieldRows.filter((f) => f.action).length
            : (piiEntries?.length ?? 0);

          return (
            <div key={type}>
              <div
                className={cn(
                  "flex items-center gap-3 px-3 py-2.5",
                  hasExpansion && "cursor-pointer hover:bg-muted/20 transition-colors",
                )}
                onClick={hasExpansion ? () => toggle(type) : undefined}
              >
                {/* Expand chevron */}
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

                {/* Color dot */}
                <span className={cn("size-2 shrink-0 rounded-full", barColor)} />

                {/* Type name */}
                <span className="min-w-0 flex-1 truncate text-sm font-medium">
                  {type}
                </span>

                {/* PII field count */}
                {piiCount > 0 && (
                  <span className="hidden sm:inline-block rounded px-1.5 py-0.5 text-[10px] font-medium bg-rose-50 text-rose-700 border border-rose-200">
                    {piiCount} PII field{piiCount !== 1 ? "s" : ""}
                  </span>
                )}

                {/* Fill bar */}
                <div className="hidden w-24 sm:flex">
                  <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                    <div
                      className={cn("h-full rounded-full transition-all", barColor)}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>

                {/* Percentage */}
                <span className="w-10 text-right text-xs text-muted-foreground tabular-nums">
                  {pct.toFixed(0)}%
                </span>

                {/* Count */}
                <span className="w-6 text-right text-sm font-semibold tabular-nums">
                  {count}
                </span>
              </div>

              {/* Expanded field table */}
              {isExpanded && (
                <div className="border-t bg-muted/10 px-4 py-2">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-muted-foreground">
                        <th className="pb-1.5 text-left font-medium">Field</th>
                        <th className="pb-1.5 text-left font-medium">Action Applied</th>
                        <th className="pb-1.5 text-right font-medium">Occurrences</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-muted/40">
                      {fieldRows
                        ? fieldRows.map((entry) => (
                            <tr
                              key={entry.fieldPath}
                              className={cn(entry.action && "bg-rose-50/40")}
                            >
                              <td className="py-1 font-mono text-foreground">
                                {entry.fieldPath}
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
                                  <span className="text-muted-foreground/50">—</span>
                                )}
                              </td>
                              <td className="py-1 text-right tabular-nums text-muted-foreground">
                                {entry.count}
                              </td>
                            </tr>
                          ))
                        : piiEntries?.map((entry) => (
                            <tr key={`${entry.fieldPath}-${entry.action}`}>
                              <td className="py-1 font-mono text-foreground">
                                {entry.fieldPath}
                              </td>
                              <td className="py-1">
                                <span
                                  className={cn(
                                    "inline-block rounded px-1.5 py-0.5 text-[10px] font-medium",
                                    actionBadgeClass(entry.action),
                                  )}
                                >
                                  {entry.action}
                                </span>
                              </td>
                              <td className="py-1 text-right tabular-nums text-muted-foreground">
                                {entry.count}
                              </td>
                            </tr>
                          ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
