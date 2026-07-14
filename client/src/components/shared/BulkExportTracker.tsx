import { useState, useEffect } from "react";
import {
  X, Download, Loader2, CheckCircle2, AlertCircle,
  ChevronDown, ChevronUp, PackageOpen,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { useBulkExport } from "@/context/BulkExportContext";
import type { ExportJobStatus, ExportJob } from "@/context/BulkExportContext";

function StatusIcon({ status }: { status: ExportJobStatus }) {
  switch (status) {
    case "submitting":
    case "pending":
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-primary shrink-0" />;
    case "done":
      return <CheckCircle2 className="h-4 w-4 text-emerald-600 shrink-0" />;
    case "error":
      return <AlertCircle className="h-4 w-4 text-destructive shrink-0" />;
  }
}

function formatElapsed(ms: number): string {
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  return `${min}m ${sec % 60}s`;
}

function JobStatusLine({ job }: { job: ExportJob }) {
  const isActive = job.status === "submitting" || job.status === "pending" || job.status === "running";
  const frozenElapsed = job.completedAt != null ? job.completedAt - job.startedAt : null;
  const [elapsed, setElapsed] = useState(() => frozenElapsed ?? Date.now() - job.startedAt);

  useEffect(() => {
    if (frozenElapsed != null) { setElapsed(frozenElapsed); return; }
    if (!isActive) { setElapsed(Date.now() - job.startedAt); return; }
    const timer = setInterval(() => setElapsed(Date.now() - job.startedAt), 1000);
    return () => clearInterval(timer);
  }, [isActive, job.startedAt, frozenElapsed]);

  if (job.error) return <span className="text-xs text-destructive truncate">{job.error}</span>;
  if (job.status === "submitting") return <span className="text-xs text-muted-foreground">Submitting…</span>;
  if (job.status === "pending") return <span className="text-xs text-muted-foreground">Queued, {formatElapsed(elapsed)}</span>;
  return (
    <span className="text-xs text-muted-foreground">
      {job.processed.toLocaleString()} resources processed, {formatElapsed(elapsed)}
    </span>
  );
}

export function BulkExportTracker() {
  const { jobs, downloadResult, dismissJob, clearCompleted } = useBulkExport();
  // Start collapsed, expand when a new active job arrives
  const [collapsed, setCollapsed] = useState(true);

  const activeJobs = jobs.filter(
    (j) => j.status === "submitting" || j.status === "pending" || j.status === "running",
  );
  const doneCount  = jobs.filter((j) => j.status === "done").length;
  const errorCount = jobs.filter((j) => j.status === "error").length;

  // Auto-expand when a new active job appears
  useEffect(() => {
    if (activeJobs.length > 0) setCollapsed(false);
  }, [activeJobs.length]);

  if (jobs.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 w-80 rounded-xl border bg-background shadow-xl">
      {/* Header */}
      <div
        className="flex items-center justify-between gap-2 px-3 py-2.5 cursor-pointer select-none"
        onClick={() => setCollapsed((c) => !c)}
      >
        <div className="flex items-center gap-2">
          <PackageOpen className="h-4 w-4 text-primary shrink-0" />
          <span className="text-sm font-semibold">Bulk Exports</span>
          {activeJobs.length > 0 && (
            <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-primary text-[10px] font-bold text-primary-foreground px-1.5">
              {activeJobs.length}
            </span>
          )}
          {errorCount > 0 && activeJobs.length === 0 && (
            <span className="flex h-5 min-w-5 items-center justify-center rounded-full bg-destructive text-[10px] font-bold text-destructive-foreground px-1.5">
              {errorCount}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          {doneCount > 0 && !collapsed && (
            <Button
              variant="ghost" size="sm"
              className="h-6 px-1.5 text-xs text-muted-foreground"
              onClick={(e) => { e.stopPropagation(); clearCompleted(); }}
            >
              Clear done
            </Button>
          )}
          {collapsed
            ? <ChevronUp className="h-4 w-4 text-muted-foreground" />
            : <ChevronDown className="h-4 w-4 text-muted-foreground" />
          }
        </div>
      </div>

      {/* Job list */}
      {!collapsed && (
        <div className="border-t max-h-64 overflow-y-auto divide-y">
          {jobs.map((job) => (
            <div key={job.id} className="px-3 py-2.5">
              <div className="flex items-center gap-2">
                <StatusIcon status={job.status} />
                <p className="flex-1 min-w-0 text-sm font-medium truncate" title={job.label}>{job.label}</p>
                <div className="flex items-center gap-0.5 shrink-0">
                  {job.status === "done" && (
                    <Button variant="ghost" size="sm" className="h-7 w-7 p-0"
                      onClick={() => downloadResult(job.id)} title="Download result"
                    >
                      <Download className="h-3.5 w-3.5" />
                    </Button>
                  )}
                  {(job.status === "done" || job.status === "error") && (
                    <Button variant="ghost" size="sm"
                      className="h-7 w-7 p-0 text-muted-foreground hover:text-foreground"
                      onClick={() => dismissJob(job.id)} title="Dismiss"
                    >
                      <X className="h-3.5 w-3.5" />
                    </Button>
                  )}
                </div>
              </div>
              <div className="mt-1 pl-6">
                <JobStatusLine job={job} />
                {job.status === "running" && (
                  <Progress value={100} className="mt-1.5 h-1 [&>div]:animate-pulse" />
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
