import { useState, useEffect } from "react";
import { cn } from "@/lib/utils";
import type { ExportJob } from "@/context/BulkExportContext";
import { PHASE_LABELS, formatElapsed } from "./bulkHelpers.tsx";
import { StatusIcon, statusBadge } from "./bulkStatusComponents.tsx";
import { getGradeStyle } from "@/lib/qualityScore";
import type { LetterGrade } from "@/lib/qualityScore";
import { ShieldCheck } from "lucide-react";
import { Progress } from "@/components/ui/progress";

export function JobCard({
  job,
  isSelected,
  onSelect,
}: {
  job: ExportJob;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const isActive =
    job.status === "submitting" ||
    job.status === "pending" ||
    job.status === "running";

  const frozenElapsed = job.completedAt != null ? job.completedAt - job.startedAt : null;
  const [elapsed, setElapsed] = useState(() => frozenElapsed ?? Date.now() - job.startedAt);

  useEffect(() => {
    if (frozenElapsed != null) { setElapsed(frozenElapsed); return; }
    if (!isActive) { setElapsed(Date.now() - job.startedAt); return; }
    const timer = setInterval(() => setElapsed(Date.now() - job.startedAt), 1000);
    return () => clearInterval(timer);
  }, [isActive, job.startedAt, frozenElapsed]);

  const hasProgress = job.stagedCount != null && job.stagedCount > 0 &&
    (job.phase === "processing" || job.phase === "uploading");
  const progressPct = hasProgress
    ? Math.round((job.processed / job.stagedCount!) * 100)
    : null;

  const grade: LetterGrade | null = job.backendScore?.computed
    ? (() => {
        const avg = job.backendScore.avg_composite ?? 0;
        return avg >= 90 ? "A" : avg >= 75 ? "B" : avg >= 60 ? "C" : avg >= 40 ? "D" : "F";
      })()
    : null;

  return (
    <div
      className={cn(
        "rounded-xl border bg-card cursor-pointer transition-all hover:shadow-md hover:-translate-y-0.5",
        isSelected
          ? "ring-2 ring-primary shadow-md border-primary/30"
          : "hover:border-primary/20",
      )}
      onClick={onSelect}
    >
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b">
        <StatusIcon status={job.status} />
        <p className="flex-1 min-w-0 text-sm font-semibold truncate" title={job.label}>
          {job.label}
        </p>
        <div className="flex items-center gap-1.5 shrink-0">
          {grade && (
            <span className={cn(
              "inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-xs font-bold",
              getGradeStyle(grade).bg,
              getGradeStyle(grade).text,
            )}>
              <ShieldCheck className="size-3" />
              {grade}
            </span>
          )}
          {statusBadge(job.status)}
        </div>
      </div>

      {/* Body */}
      <div className="px-4 py-3 space-y-2">
        <div className="grid grid-cols-2 gap-x-4 gap-y-2">
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Phase</p>
            <p className="text-sm font-medium mt-0.5">{PHASE_LABELS[job.phase] ?? job.phase}</p>
          </div>
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Processed</p>
            <p className="text-sm font-medium tabular-nums mt-0.5">
              {job.processed.toLocaleString()}
              {job.stagedCount != null && job.stagedCount > 0 && (
                <span className="text-muted-foreground text-xs font-normal"> / {job.stagedCount.toLocaleString()}</span>
              )}
            </p>
          </div>
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Elapsed</p>
            <p className="text-sm font-medium tabular-nums mt-0.5">{formatElapsed(elapsed)}</p>
          </div>
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">Profile</p>
            <p className="text-sm font-mono truncate mt-0.5" title={job.configProfile}>{job.configProfile}</p>
          </div>
        </div>

        {isActive && (
          progressPct != null ? (
            <div>
              <Progress value={progressPct} className="h-1.5" />
              <p className="mt-1 text-xs text-muted-foreground text-right tabular-nums">{progressPct}%</p>
            </div>
          ) : (
            <Progress value={100} className="h-1.5 [&>div]:animate-pulse" />
          )
        )}

        {job.error && (
          <p className="text-xs text-destructive bg-destructive/5 rounded px-2 py-1.5 border border-destructive/20 break-words">
            {job.error}
          </p>
        )}
      </div>
    </div>
  );
}
