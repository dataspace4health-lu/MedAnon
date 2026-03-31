import { useState } from "react";
import { Check, ChevronDown, User, UserCog } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface ConditionPatient {
  patient_id: string;
  patient_name: string;
  patient_birth_date: string;
  patient_gender: string;
  clinical_status: string;
}

export interface ConditionGroup {
  /** Dedup key: `code` when available, otherwise `display`. */
  key: string;
  code: string;
  display: string;
  patients: ConditionPatient[];
}

interface ConditionGroupCardProps {
  group: ConditionGroup;
  selected?: boolean;
  onSelect?: () => void;
  onDeidentify: (patientId: string, patientName: string) => void;
}

const STATUS_STYLES: Record<string, { dot: string; badge: string }> = {
  active: {
    dot: "bg-emerald-500",
    badge:
      "bg-emerald-50 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-300 dark:border-emerald-800",
  },
  resolved: {
    dot: "bg-slate-400",
    badge:
      "bg-slate-50 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700",
  },
  inactive: {
    dot: "bg-amber-400",
    badge:
      "bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-300 dark:border-amber-800",
  },
};

function getStatusStyle(status: string) {
  return (
    STATUS_STYLES[status?.toLowerCase()] ?? {
      dot: "bg-muted-foreground",
      badge: "bg-muted text-muted-foreground border-border",
    }
  );
}

const MAX_COLLAPSED = 3;

export function ConditionGroupCard({
  group,
  selected = false,
  onSelect,
  onDeidentify,
}: ConditionGroupCardProps) {
  const [expanded, setExpanded] = useState(false);

  const showAll = expanded || group.patients.length <= MAX_COLLAPSED;
  const visiblePatients = showAll ? group.patients : group.patients.slice(0, MAX_COLLAPSED);
  const hiddenCount = group.patients.length - MAX_COLLAPSED;

  // Summarise statuses for the header badge
  const statusCounts = group.patients.reduce<Record<string, number>>((acc, p) => {
    const s = p.clinical_status?.toLowerCase() || "unknown";
    acc[s] = (acc[s] ?? 0) + 1;
    return acc;
  }, {});
  const uniqueStatuses = Object.keys(statusCounts);
  const headerStatus = uniqueStatuses.length === 1 ? uniqueStatuses[0] : "mixed";
  const headerStyle = getStatusStyle(headerStatus);

  return (
    <Card
      className={cn(
        "overflow-hidden transition-shadow hover:shadow-sm",
        selected && "ring-2 ring-primary bg-primary/5",
      )}
    >
      {/* Condition header */}
      <div className="flex items-start gap-3 px-4 pt-4 pb-3">
        {onSelect ? (
          <button
            type="button"
            onClick={onSelect}
            aria-label={selected ? "Deselect condition" : "Select condition for export"}
            className={cn(
              "mt-0.5 flex shrink-0 size-5 items-center justify-center rounded border-2 transition-colors",
              selected
                ? "border-primary bg-primary text-primary-foreground"
                : "border-muted-foreground/40 bg-background hover:border-primary",
            )}
          >
            {selected && <Check className="size-3" strokeWidth={3} />}
          </button>
        ) : (
          <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", headerStyle.dot)} />
        )}

        <div className="min-w-0 flex-1">
          <p className="font-semibold leading-snug text-foreground">
            {group.display || group.code || "Unknown condition"}
          </p>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
            {onSelect && (
              <span className={cn("size-2 rounded-full shrink-0", headerStyle.dot)} />
            )}
            {uniqueStatuses.length === 1 ? (
              <span
                className={cn(
                  "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium capitalize",
                  headerStyle.badge,
                )}
              >
                {headerStatus}
              </span>
            ) : (
              <span className="inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium bg-muted text-muted-foreground border-border gap-1">
                {uniqueStatuses.map((s) => (
                  <span key={s} className={cn("size-1.5 rounded-full", getStatusStyle(s).dot)} />
                ))}
                mixed status
              </span>
            )}
            {group.code && (
              <span className="font-mono text-xs text-muted-foreground">
                {group.code}
              </span>
            )}
            <span className="text-xs text-muted-foreground">
              {group.patients.length} patient{group.patients.length !== 1 ? "s" : ""}
            </span>
          </div>
        </div>
      </div>

      {/* Patient rows */}
      <CardContent className="border-t bg-muted/20 px-0 py-0">
        <ul className="divide-y">
          {visiblePatients.map((p) => {
            const pStyle = getStatusStyle(p.clinical_status);
            return (
              <li key={p.patient_id} className="flex items-center gap-3 px-4 py-2.5">
                <User className="size-4 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    <p className="truncate text-sm font-medium">
                      {p.patient_name || "Unknown patient"}
                    </p>
                    {uniqueStatuses.length > 1 && (
                      <span
                        className={cn(
                          "inline-flex items-center rounded-full border px-1.5 py-0 text-xs capitalize shrink-0",
                          pStyle.badge,
                        )}
                      >
                        {p.clinical_status || "unknown"}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {[p.patient_gender, p.patient_birth_date].filter(Boolean).join(" · ") || p.patient_id}
                  </p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  className="shrink-0 h-7 gap-1 text-xs"
                  onClick={() => onDeidentify(p.patient_id, p.patient_name)}
                >
                  <UserCog className="size-3" />
                  De-identify
                </Button>
              </li>
            );
          })}
        </ul>

        {group.patients.length > MAX_COLLAPSED && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="flex w-full items-center justify-center gap-1 border-t py-2 text-xs text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          >
            <ChevronDown
              className={cn("size-3.5 transition-transform", expanded && "rotate-180")}
            />
            {expanded
              ? "Show less"
              : `Show ${hiddenCount} more patient${hiddenCount !== 1 ? "s" : ""}`}
          </button>
        )}
      </CardContent>
    </Card>
  );
}
