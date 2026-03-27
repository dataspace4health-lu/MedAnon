import { cn } from "@/lib/utils";

interface HealthBadgeProps {
  ok: boolean;
  version?: string;
  loading?: boolean;
}

export function HealthBadge({ ok, version, loading }: HealthBadgeProps) {
  if (loading) {
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full border bg-muted/40 px-2 py-0.5 text-xs text-muted-foreground">
        <span className="size-1.5 animate-pulse rounded-full bg-muted-foreground/50" />
        Checking
      </span>
    );
  }

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        ok
          ? "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300"
          : "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-400"
      )}
    >
      <span className="relative flex size-1.5">
        {ok && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
        )}
        <span
          className={cn(
            "relative inline-flex size-1.5 rounded-full",
            ok ? "bg-emerald-500" : "bg-red-500"
          )}
        />
      </span>
      {ok ? (version ? `v${version}` : "Online") : "Offline"}
    </span>
  );
}
