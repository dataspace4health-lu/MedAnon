import { Card, CardContent, CardFooter } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

interface PatientCardProps {
  patient: {
    id: string;
    name: string;
    birthDate: string;
    gender: string;
  };
  onSelect?: () => void;
  selected?: boolean;
}

function getInitials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function getAge(birthDate: string): string {
  if (!birthDate) return "";
  const birth = new Date(birthDate);
  const today = new Date();
  const age = today.getFullYear() - birth.getFullYear();
  return `${age} yrs`;
}

const GENDER_COLORS: Record<string, string> = {
  male: "bg-blue-100 text-blue-700 dark:bg-blue-950/50 dark:text-blue-300",
  female: "bg-pink-100 text-pink-700 dark:bg-pink-950/50 dark:text-pink-300",
};

export function PatientCard({
  patient,
  onSelect,
  selected = false,
}: PatientCardProps) {
  const initials = getInitials(patient.name);
  const genderColor =
    GENDER_COLORS[patient.gender?.toLowerCase()] ??
    "bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300";

  return (
    <Card
      className={cn(
        "transition-all duration-200",
        selected
          ? "border-primary/60 bg-primary/5 ring-1 ring-primary/30 shadow-sm"
          : "hover:border-border hover:shadow-sm"
      )}
    >
      <CardContent className="pt-4 pb-3">
        <div className="flex items-start gap-3">
          {/* Initials avatar */}
          <div
            className={cn(
              "flex size-10 shrink-0 items-center justify-center rounded-full text-sm font-semibold",
              selected
                ? "bg-primary text-primary-foreground"
                : "bg-muted text-muted-foreground"
            )}
          >
            {initials}
          </div>

          {/* Patient info */}
          <div className="min-w-0 flex-1">
            <p className="truncate font-semibold leading-tight text-foreground">
              {patient.name || <span className="text-muted-foreground italic">Unknown</span>}
            </p>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
              {patient.gender && (
                <span
                  className={cn(
                    "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium capitalize",
                    genderColor
                  )}
                >
                  {patient.gender}
                </span>
              )}
              {patient.birthDate && (
                <Badge variant="secondary" className="text-xs font-normal">
                  {getAge(patient.birthDate)} · {patient.birthDate}
                </Badge>
              )}
            </div>
            <p className="mt-1.5 truncate font-mono text-[11px] text-muted-foreground/60">
              {patient.id}
            </p>
          </div>
        </div>
      </CardContent>

      {onSelect && (
        <CardFooter className="pt-0 pb-3 px-4">
          <Button
            variant={selected ? "secondary" : "default"}
            size="sm"
            onClick={onSelect}
            className="w-full"
          >
            {selected ? "Deselect" : "De-identify"}
          </Button>
        </CardFooter>
      )}
    </Card>
  );
}
