import { useState, useEffect, useCallback } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { ResourceTypeSummary } from "@/components/shared/ResourceTypeSummary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  ArrowLeft,
  Loader2,
  AlertCircle,
  Download,
  CheckCircle2,
  Inbox,
} from "lucide-react";
import { getJobResult } from "@/api/medanon";
import { useBulkExport } from "@/context/BulkExportContext";
import { buildPiiFromDeidentifiedOnly } from "@/lib/piiDetection";
import type { PiiDetectionMap } from "@/lib/piiDetection";

// ---------------------------------------------------------------------------
// Location state passed from PatientBrowser / ConditionBrowser
// ---------------------------------------------------------------------------

interface BulkDeidentifyState {
  source: "all" | "condition";
  conditionName?: string;
  exportId: string;          // local id from BulkExportContext
  configProfile: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const PHASE_LABELS: Record<string, string> = {
  queued: "Queued",
  fetching: "Fetching resources from FHIR server",
  processing: "De-identifying resources",
  done: "Complete",
};

async function parseNdjsonBlob(blob: Blob): Promise<{
  counts: Record<string, number>;
  resources: Record<string, unknown>[];
}> {
  const text = await blob.text();
  const counts: Record<string, number> = {};
  const resources: Record<string, unknown>[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const resource = JSON.parse(trimmed) as Record<string, unknown>;
      const type = (resource.resourceType as string) ?? "Unknown";
      counts[type] = (counts[type] ?? 0) + 1;
      resources.push(resource);
    } catch {
      // skip malformed lines
    }
  }
  return { counts, resources };
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
    case "error":
      return <Badge variant="destructive">Error</Badge>;
    default:
      return <Badge variant="secondary">{status}</Badge>;
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function BulkDeidentifyPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const locState = location.state as BulkDeidentifyState | null;
  const { jobs } = useBulkExport();

  // Redirect if no state (direct URL navigation)
  useEffect(() => {
    if (!locState) navigate("/patients", { replace: true });
  }, [locState, navigate]);

  const title =
    locState?.source === "condition" && locState.conditionName
      ? `De-identify Patients with ${locState.conditionName}`
      : "De-identify All";

  const backPath = locState?.source === "condition" ? "/conditions" : "/patients";

  // Find our job in the context
  const job = locState ? jobs.find((j) => j.id === locState.exportId) : undefined;

  // Local state for parsed results
  const [resourceCounts, setResourceCounts] = useState<Record<string, number>>({});
  const [piiData, setPiiData] = useState<PiiDetectionMap>({});
  const [resultBlob, setResultBlob] = useState<Blob | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);

  // Fetch + parse result when the context job reaches "done"
  useEffect(() => {
    if (!job?.jobId || job.status !== "done" || resultBlob) return;

    const fetchResult = async () => {
      try {
        const blob = await getJobResult(job.jobId!);
        setResultBlob(blob);
        const { counts, resources } = await parseNdjsonBlob(blob);
        setResourceCounts(counts);
        setPiiData(buildPiiFromDeidentifiedOnly(resources));
      } catch (err) {
        setFetchError(err instanceof Error ? err.message : "Failed to fetch results");
      }
    };
    fetchResult();
  }, [job?.jobId, job?.status, resultBlob]);

  const handleDownload = useCallback(() => {
    if (!resultBlob) return;
    const url = URL.createObjectURL(resultBlob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download =
      locState?.source === "condition"
        ? "conditions-deidentified.ndjson"
        : "all-patients-deidentified.ndjson";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  }, [resultBlob, locState?.source]);

  if (!locState) return null;

  const isDone = job?.status === "done";
  const isRunning = job?.status === "submitting" || job?.status === "pending" || job?.status === "running";
  const totalResources = Object.values(resourceCounts).reduce((s, c) => s + c, 0);
  const error = job?.error ?? fetchError;

  return (
    <div>
      <PageHeader title={title} description="Bulk de-identification job progress and results" />

      {/* Back link */}
      <button
        onClick={() => navigate(backPath)}
        className="mb-6 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="size-4" />
        Back
      </button>

      {/* Config profile */}
      <div className="mb-6 flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Profile
        </span>
        <Badge variant="outline" className="font-mono text-xs">
          {locState.configProfile}
        </Badge>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-4 flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Job status card */}
      {job && (
        <div className="mb-6 rounded-xl border bg-card p-4 shadow-sm">
          <div className="flex flex-wrap items-center gap-3">
            {isRunning && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
            {isDone && <CheckCircle2 className="size-4 text-green-600" />}
            <span className="text-sm font-medium">Job Status</span>
            {statusBadge(job.status)}
            {job.jobId && (
              <span className="text-xs font-mono text-muted-foreground">
                {job.jobId}
              </span>
            )}
          </div>
          <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3">
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
        </div>
      )}

      {/* Empty result */}
      {isDone && resultBlob && totalResources === 0 && (
        <div className="mb-6 flex flex-col items-center justify-center rounded-lg border border-dashed py-12 text-center text-muted-foreground">
          <Inbox className="mb-3 size-10" />
          <p className="text-sm font-medium">No resources found</p>
          <p className="mt-1 text-xs">
            The FHIR server returned no resources matching this export. Verify that the server contains data.
          </p>
        </div>
      )}

      {/* Resource summary */}
      {totalResources > 0 && (
        <div className="mb-6">
          <ResourceTypeSummary counts={resourceCounts} piiData={piiData} />
        </div>
      )}

      {/* Download */}
      {isDone && resultBlob && totalResources > 0 && (
        <Button onClick={handleDownload}>
          <Download className="size-4" />
          Download NDJSON ({totalResources} resources)
        </Button>
      )}
    </div>
  );
}
