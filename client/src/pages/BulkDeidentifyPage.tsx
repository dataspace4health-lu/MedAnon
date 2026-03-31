import { useState, useEffect, useRef } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { ResourceTypeSummary } from "@/components/shared/ResourceTypeSummary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import {
  Loader2, AlertCircle, Download, CheckCircle2, Inbox, Square,
  XCircle,
} from "lucide-react";
import { getJobResult } from "@/api/medanon";
import { useBulkExport } from "@/context/BulkExportContext";
import type { ExportJob, ExportJobStatus } from "@/context/BulkExportContext";
import { buildPiiFromDeidentifiedOnly, buildFieldSummary } from "@/lib/piiDetection";
import type { PiiDetectionMap, FieldSummaryMap } from "@/lib/piiDetection";
import { extractFields } from "@/lib/fhirFields";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const PHASE_LABELS: Record<string, string> = {
  queued: "Queued",
  fetching: "Fetching from FHIR server",
  processing: "De-identifying resources",
  done: "Complete",
};

async function parseNdjsonBlob(blob: Blob): Promise<{
  counts: Record<string, number>;
  allFieldCounts: Record<string, Record<string, number>>;
  resources: Record<string, unknown>[];
}> {
  const text = await blob.text();
  const counts: Record<string, number> = {};
  const allFieldCounts: Record<string, Record<string, number>> = {};
  const resources: Record<string, unknown>[] = [];
  // PII detection sample cap — regex matching is heavier than plain field counting
  const PII_SAMPLE_LIMIT = 200;
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const resource = JSON.parse(trimmed) as Record<string, unknown>;
      const type = (resource.resourceType as string) ?? "Unknown";
      counts[type] = (counts[type] ?? 0) + 1;

      // Count every field occurrence across ALL resources (not just the sample)
      if (!allFieldCounts[type]) allFieldCounts[type] = {};
      const fc = allFieldCounts[type];
      for (const { field } of extractFields(resource)) {
        if (field !== "resourceType") fc[field] = (fc[field] ?? 0) + 1;
      }

      if (resources.length < PII_SAMPLE_LIMIT) {
        resources.push(resource);
      }
    } catch {
      // skip malformed lines
    }
  }
  return { counts, allFieldCounts, resources };
}

function StatusIcon({ status }: { status: ExportJobStatus }) {
  switch (status) {
    case "submitting":
    case "pending":
    case "running":
      return <Loader2 className="size-4 animate-spin text-primary shrink-0" />;
    case "done":
      return <CheckCircle2 className="size-4 text-green-600 shrink-0" />;
    case "cancelled":
      return <XCircle className="size-4 text-muted-foreground shrink-0" />;
    case "error":
      return <AlertCircle className="size-4 text-destructive shrink-0" />;
  }
}

function statusBadge(status: string) {
  switch (status) {
    case "submitting":
    case "pending":
      return <Badge variant="secondary">Pending</Badge>;
    case "running":
      return <Badge className="bg-blue-100 text-blue-800 hover:bg-blue-100">Running</Badge>;
    case "done":
      return <Badge className="bg-green-100 text-green-800 hover:bg-green-100">Done</Badge>;
    case "cancelled":
      return <Badge variant="secondary">Cancelled</Badge>;
    case "error":
      return <Badge variant="destructive">Error</Badge>;
    default:
      return <Badge variant="secondary">{status}</Badge>;
  }
}

function formatElapsed(ms: number): string {
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const remSec = sec % 60;
  return `${min}m ${remSec}s`;
}

// ---------------------------------------------------------------------------
// Job Card
// ---------------------------------------------------------------------------

function JobCard({
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
            <p className="font-medium tabular-nums">{job.processed}</p>
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
          <Progress value={100} className="mt-2 h-1 [&>div]:animate-pulse" />
        )}
        {job.error && (
          <p className="mt-2 text-xs text-destructive truncate">{job.error}</p>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Job Detail Panel
// ---------------------------------------------------------------------------

function JobDetailPanel({ job }: { job: ExportJob }) {
  const { downloadResult, cancelJob, dismissJob } = useBulkExport();
  const [resourceCounts, setResourceCounts] = useState<Record<string, number>>({});
  const [piiData, setPiiData] = useState<PiiDetectionMap>({});
  const [fieldSummary, setFieldSummary] = useState<FieldSummaryMap>({});
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);

  // Fetch and parse result when job is done
  useEffect(() => {
    if (!job.jobId || job.status !== "done" || parsed || parsing) return;
    let cancelled = false;
    setParsing(true);
    (async () => {
      try {
        const blob = await getJobResult(job.jobId!);
        if (cancelled) return;
        const { counts, allFieldCounts, resources } = await parseNdjsonBlob(blob);
        if (cancelled) return;
        const pii = buildPiiFromDeidentifiedOnly(resources);
        if (cancelled) return;
        setResourceCounts(counts);
        setPiiData(pii);
        setFieldSummary(buildFieldSummary(allFieldCounts, pii));
        setParsed(true);
      } catch (err) {
        if (!cancelled)
          setFetchError(err instanceof Error ? err.message : "Failed to fetch results");
      } finally {
        setParsing(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.jobId, job.status, parsed]);

  const isDone = job.status === "done";
  const isRunning =
    job.status === "submitting" ||
    job.status === "pending" ||
    job.status === "running";
  const isCancelled = job.status === "cancelled";
  const totalResources = Object.values(resourceCounts).reduce((s, c) => s + c, 0);
  const error = job.error ?? fetchError;
  const isTerminal = isDone || job.status === "error" || isCancelled;

  async function handleStop() {
    setStopping(true);
    try {
      await cancelJob(job.id);
    } finally {
      setStopping(false);
    }
  }

  return (
    <div className="rounded-xl border bg-card p-6 shadow-sm">
      {/* Header */}
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <StatusIcon status={job.status} />
        <h3 className="text-lg font-semibold flex-1 min-w-0 truncate">{job.label}</h3>
        {statusBadge(job.status)}
        {job.jobId && (
          <span className="text-xs font-mono text-muted-foreground">{job.jobId}</span>
        )}
        {/* Stop / Dismiss buttons */}
        {isRunning && (
          <Button
            variant="outline"
            size="sm"
            className="text-xs text-destructive border-destructive/40 hover:bg-destructive/5"
            onClick={handleStop}
            disabled={stopping}
          >
            {stopping ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Square className="size-3.5" />
            )}
            Stop
          </Button>
        )}
        {isTerminal && (
          <Button
            variant="ghost"
            size="sm"
            className="text-xs text-muted-foreground"
            onClick={() => dismissJob(job.id)}
          >
            Dismiss
          </Button>
        )}
      </div>

      {/* Metadata badges */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Badge variant="outline" className="font-mono text-xs">
          {job.configProfile}
        </Badge>
        {job.source === "condition" && job.conditionName && (
          <Badge variant="secondary" className="text-xs">
            {job.conditionName}
          </Badge>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Cancelled notice */}
      {isCancelled && (
        <div className="mb-4 rounded-lg border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          This job was cancelled after processing {job.processed} resource{job.processed !== 1 ? "s" : ""}.
        </div>
      )}

      {/* Running stats */}
      {isRunning && (
        <div className="mb-4">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
            <div>
              <span className="text-muted-foreground">Phase</span>
              <p className="font-medium">{PHASE_LABELS[job.phase] ?? job.phase}</p>
            </div>
            <div>
              <span className="text-muted-foreground">Processed</span>
              <p className="font-medium tabular-nums">{job.processed}</p>
            </div>
            <div>
              <span className="text-muted-foreground">Started</span>
              <p className="font-medium">{new Date(job.startedAt).toLocaleTimeString()}</p>
            </div>
          </div>
          <Progress value={100} className="mt-3 h-1.5 [&>div]:animate-pulse" />
        </div>
      )}

      {/* Loading results */}
      {isDone && parsing && (
        <div className="mb-4 flex items-center gap-2.5 rounded-lg border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading resource summary and PII analysis...
        </div>
      )}

      {/* Empty result — only show after parsing completes with 0 results */}
      {isDone && parsed && totalResources === 0 && job.processed === 0 && (
        <div className="mb-4 flex flex-col items-center justify-center rounded-lg border border-dashed py-12 text-center text-muted-foreground">
          <Inbox className="mb-3 size-10" />
          <p className="text-sm font-medium">No resources found</p>
          <p className="mt-1 text-xs">
            The FHIR server returned no resources matching this export.
          </p>
        </div>
      )}

      {/* Resource + field summary */}
      {totalResources > 0 && (
        <div className="mb-4">
          <ResourceTypeSummary
            counts={resourceCounts}
            piiData={piiData}
            fieldSummary={fieldSummary}
          />
        </div>
      )}

      {/* Download */}
      {isDone && job.processed > 0 && (
        <Button onClick={() => downloadResult(job.id)}>
          <Download className="size-4" />
          Download NDJSON ({parsed ? totalResources : job.processed} resources)
        </Button>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function BulkDeidentifyPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const autoSelectId = (location.state as { autoSelectId?: string } | null)?.autoSelectId;
  const { jobs } = useBulkExport();

  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Auto-select on arrival or when a new job appears
  const prevJobCount = useRef(jobs.length);
  useEffect(() => {
    if (autoSelectId && jobs.some((j) => j.id === autoSelectId) && selectedId !== autoSelectId) {
      setSelectedId(autoSelectId);
    } else if (jobs.length > prevJobCount.current) {
      setSelectedId(jobs[jobs.length - 1].id);
    } else if (selectedId === null && jobs.length > 0) {
      setSelectedId(jobs[jobs.length - 1].id);
    }
    prevJobCount.current = jobs.length;
  }, [autoSelectId, jobs, selectedId]);

  // If selected job was dismissed, clear selection
  const selectedJob = jobs.find((j) => j.id === selectedId) ?? null;
  useEffect(() => {
    if (selectedId && !selectedJob && jobs.length > 0) {
      setSelectedId(jobs[jobs.length - 1].id);
    }
  }, [selectedId, selectedJob, jobs]);

  return (
    <div>
      <PageHeader
        title="Bulk De-identification"
        description="View and manage all bulk de-identification jobs"
      />

      {jobs.length === 0 ? (
        /* Empty state */
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16 text-center text-muted-foreground">
          <Inbox className="mb-3 size-10" />
          <p className="text-sm font-medium">No bulk de-identification jobs</p>
          <p className="mt-1 max-w-sm text-xs">
            Start a bulk export from the Patient Browser or Condition Browser to see jobs here.
          </p>
          <div className="mt-4 flex gap-2">
            <Button variant="outline" size="sm" onClick={() => navigate("/patients")}>
              Patient Browser
            </Button>
            <Button variant="outline" size="sm" onClick={() => navigate("/conditions")}>
              Condition Browser
            </Button>
          </div>
        </div>
      ) : (
        <>
          {/* Job card grid */}
          <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[...jobs].reverse().map((job) => (
              <JobCard
                key={job.id}
                job={job}
                isSelected={job.id === selectedId}
                onSelect={() => setSelectedId(job.id)}
              />
            ))}
          </div>

          {/* Detail panel */}
          {selectedJob && (
            <div className="space-y-4">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                Job Details
              </h2>
              <JobDetailPanel key={selectedJob.id} job={selectedJob} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
