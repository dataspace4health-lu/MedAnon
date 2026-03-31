import { cn } from "@/lib/utils";

interface ResourceTypeSummaryProps {
  counts: Record<string, number>;
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

export function ResourceTypeSummary({ counts }: ResourceTypeSummaryProps) {
  const sorted = Object.entries(counts).sort(([, a], [, b]) => b - a);

  if (sorted.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">No resources to display.</p>
    );
  }

  const total = sorted.reduce((sum, [, count]) => sum + count, 0);

  return (
    <div className="rounded-lg border overflow-hidden">
      <div className="bg-muted/30 px-3 py-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Resource Summary &middot; {total} total
      </div>
      <div className="divide-y">
        {sorted.map(([type, count], index) => {
          const pct = total > 0 ? (count / total) * 100 : 0;
          const barColor = getBarColor(type, index);

          return (
            <div key={type} className="flex items-center gap-3 px-3 py-2.5">
              {/* Color dot */}
              <span
                className={cn(
                  "size-2 shrink-0 rounded-full",
                  barColor
                )}
              />

              {/* Type name */}
              <span className="min-w-0 flex-1 truncate text-sm font-medium">
                {type}
              </span>

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
          );
        })}
      </div>
    </div>
  );
}
