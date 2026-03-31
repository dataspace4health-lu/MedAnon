import { Loader2, CheckCircle2 } from "lucide-react";
import { cn } from "@/lib/utils";

interface StreamProgressProps {
  count: number;
  isStreaming: boolean;
  errorCount?: number;
}

export function StreamProgress({
  count,
  isStreaming,
  errorCount = 0,
}: StreamProgressProps) {
  return (
    <div className="flex items-center justify-between rounded-lg border bg-muted/30 px-4 py-3">
      <div className="flex items-center gap-2.5">
        {isStreaming ? (
          <Loader2 className="size-4 shrink-0 animate-spin text-primary" />
        ) : (
          <CheckCircle2
            className={cn(
              "size-4 shrink-0",
              errorCount > 0 ? "text-amber-500" : "text-emerald-500"
            )}
          />
        )}
        <span className="text-sm text-muted-foreground">
          {isStreaming ? (
            <>
              Streaming{" "}
              <span className="font-semibold tabular-nums text-foreground">
                {count}
              </span>{" "}
              resource{count !== 1 ? "s" : ""}…
            </>
          ) : (
            <>
              <span className="font-semibold tabular-nums text-foreground">
                {count}
              </span>{" "}
              resource{count !== 1 ? "s" : ""} processed
            </>
          )}
        </span>
      </div>

      {errorCount > 0 && (
        <span className="rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
          {errorCount} error{errorCount !== 1 ? "s" : ""}
        </span>
      )}
    </div>
  );
}
