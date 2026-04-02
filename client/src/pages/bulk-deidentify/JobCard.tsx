import { useState, useEffect } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import type { ExportJob } from "@/context/BulkExportContext";
import { PHASE_LABELS, StatusIcon, statusBadge, formatElapsed } from "./bulkHelpers.tsx";

export function JobCard({
  job,
  isSelected,
  onSelect,
}: {
  job: ExportJob;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const [elapsed, setElapsed] = useState(() => Date.now() - job.startedAt);
  const isActive =
    job.status === "submitting" ||
    job.status === "pending" ||
    job.status === "running";

  useEffect(() => {
    if (!isActive) {
      setElapsed(Date.now() - job.startedAt);
      return;
    }
    const timer = setInterval(() => setElapsed(Date.now() - job.startedAt), 1000);
    return () => clearInterval(timer);
  }, [isActive, job.startedAt]);

  // Compute real progress percentage when staged_count is available
  const hasProgress = job.stagedCount != null && job.stagedCount > 0 && job.phase === "processing";
  const progressPct = hasProgress ? Math.round((job.processed / job.stagedCount!) * 100) : null;

  return (
    <Card
      className={cn(
        "cursor-pointer transition-all hover:ring-2 hover:ring-primary/30",
        isSelected && "ring-2 ring-primary shadow-md",
      )}
      size="sm"
      onClick={onSelect}
    >
      <CardHeader>
        <div className="flex items-center gap-2">
          <StatusIcon status={job.status} />
          <CardTitle className="flex-1 truncate">{job.label}</CardTitle>
          {statusBadge(job.status)}
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <div>
            <span className="text-muted-foreground">Phase</span>
            <p className="font-medium">{PHASE_LABELS[job.phase] ?? job.phase}</p>
          </div>
          <div>
            <span className="text-muted-foreground">Processed</span>
            <p className="font-medium tabular-nums">
              {job.processed}
              {job.stagedCount != null && job.stagedCount > 0 && (
                <span className="text-muted-foreground"> / {job.stagedCount}</span>
              )}
            </p>
          </div>
          <div>
            <span className="text-muted-foreground">Elapsed</span>
            <p className="font-medium tabular-nums">{formatElapsed(elapsed)}</p>
          </div>
          <div>
            <span className="text-muted-foreground">Profile</span>
            <p className="font-mono text-[11px] truncate">{job.configProfile}</p>
          </div>
        </div>
        {isActive && (
          progressPct != null ? (
            <div className="mt-2">
              <Progress value={progressPct} className="h-1" />
              <p className="mt-0.5 text-[10px] text-muted-foreground text-right tabular-nums">{progressPct}%</p>
            </div>
          ) : (
            <Progress value={100} className="mt-2 h-1 [&>div]:animate-pulse" />
          )
        )}
        {job.error && (
          <p className="mt-2 text-xs text-destructive truncate">{job.error}</p>
        )}
      </CardContent>
    </Card>
  );
}
