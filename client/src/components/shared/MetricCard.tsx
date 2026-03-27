import { cn } from "@/lib/utils";

interface MetricCardProps {
  label: string;
  value: string | number;
  variant?: "default" | "success" | "warning" | "destructive";
}

const VARIANTS: Record<
  NonNullable<MetricCardProps["variant"]>,
  { bar: string; value: string; bg: string }
> = {
  default: {
    bar: "bg-primary/60",
    value: "text-foreground",
    bg: "bg-card",
  },
  success: {
    bar: "bg-emerald-500",
    value: "text-emerald-600 dark:text-emerald-400",
    bg: "bg-emerald-50/50 dark:bg-emerald-950/20",
  },
  warning: {
    bar: "bg-amber-400",
    value: "text-amber-600 dark:text-amber-400",
    bg: "bg-amber-50/50 dark:bg-amber-950/20",
  },
  destructive: {
    bar: "bg-destructive",
    value: "text-destructive",
    bg: "bg-red-50/50 dark:bg-red-950/20",
  },
};

export function MetricCard({
  label,
  value,
  variant = "default",
}: MetricCardProps) {
  const v = VARIANTS[variant];

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-xl border px-4 py-3 shadow-sm",
        v.bg
      )}
    >
      {/* Accent bar */}
      <div className={cn("absolute left-0 top-0 h-full w-1", v.bar)} />

      <div className="pl-1">
        <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
          {label}
        </p>
        <p
          className={cn(
            "mt-1 text-2xl font-bold tabular-nums leading-none",
            v.value
          )}
        >
          {value}
        </p>
      </div>
    </div>
  );
}
