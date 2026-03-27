import { ChevronDown, User } from "lucide-react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ConditionCardProps {
  condition: {
    condition_id: string;
    code: string;
    display: string;
    clinical_status: string;
    patient_id: string;
    patient_name: string;
    patient_birth_date: string;
    patient_gender: string;
  };
  selected?: boolean;
  onDeidentify?: () => void;
}

const STATUS_STYLES: Record<
  string,
  { dot: string; badge: string }
> = {
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

export function ConditionCard({ condition, selected = false, onDeidentify }: ConditionCardProps) {
  const style = getStatusStyle(condition.clinical_status);

  return (
    <Card className={cn("overflow-hidden transition-shadow hover:shadow-sm", selected && "ring-2 ring-primary")}>
      <Collapsible>
        {/* Header — always visible */}
        <div className="flex items-start gap-3 px-4 pt-4 pb-3">
          {/* Status dot */}
          <div className="mt-1 flex shrink-0 items-center">
            <span className={cn("size-2 rounded-full", style.dot)} />
          </div>

          {/* Main content */}
          <div className="min-w-0 flex-1">
            <p className="font-semibold leading-snug text-foreground">
              {condition.display || condition.code || "Unknown condition"}
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              <span
                className={cn(
                  "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium capitalize",
                  style.badge
                )}
              >
                {condition.clinical_status || "unknown"}
              </span>
              {condition.code && (
                <span className="font-mono text-xs text-muted-foreground">
                  {condition.code}
                </span>
              )}
            </div>
            {/* Patient name preview */}
            {condition.patient_name && (
              <div className="mt-2 flex items-center gap-1 text-xs text-muted-foreground">
                <User className="size-3 shrink-0" />
                <span className="truncate">{condition.patient_name}</span>
              </div>
            )}
          </div>

          {/* Expand toggle */}
          <CollapsibleTrigger className="shrink-0 mt-0.5 flex size-7 items-center justify-center rounded-md hover:bg-accent">
            <ChevronDown className="h-4 w-4 transition-transform [[data-panel-open]_&]:rotate-180" />
            <span className="sr-only">Toggle details</span>
          </CollapsibleTrigger>
        </div>

        {/* Expandable details */}
        <CollapsibleContent>
          <CardContent className="border-t bg-muted/20 px-4 py-3">
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
              <dt className="text-muted-foreground">Patient</dt>
              <dd className="font-medium">{condition.patient_name || "—"}</dd>

              <dt className="text-muted-foreground">Patient ID</dt>
              <dd className="font-mono text-xs text-muted-foreground/70">
                {condition.patient_id}
              </dd>

              <dt className="text-muted-foreground">Gender</dt>
              <dd className="capitalize">{condition.patient_gender || "—"}</dd>

              <dt className="text-muted-foreground">Birth Date</dt>
              <dd>{condition.patient_birth_date || "—"}</dd>
            </dl>

            {onDeidentify && (
              <div className="mt-3">
                <Button
                  size="sm"
                  variant={selected ? "outline" : "default"}
                  onClick={onDeidentify}
                  className="w-full"
                >
                  {selected ? 'Deselect Patient' : 'De-identify Patient'}
                </Button>
              </div>
            )}
          </CardContent>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  );
}
