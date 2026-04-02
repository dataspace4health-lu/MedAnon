import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { ResourceTypeSummary } from "@/components/shared/ResourceTypeSummary";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Loader2, AlertCircle, Download, CheckCircle2, Inbox, Square,
  RefreshCw, User, Stethoscope, Database, Upload,
} from "lucide-react";
import { getJobResult, uploadJobToTarget } from "@/api/medanon";
import { useBulkExport } from "@/context/BulkExportContext";
import type { ExportJob } from "@/context/BulkExportContext";
import { buildPiiFromDeidentifiedOnly, buildFieldSummary } from "@/lib/piiDetection";
import type { PiiDetectionMap, FieldSummaryMap } from "@/lib/piiDetection";
import {
  PHASE_LABELS,
  parseNdjsonBlob,
  FORMAT_OPTIONS,
  buildPatientBundlesNdjson,
  triggerDownload,
  StatusIcon,
  statusBadge,
} from "./bulkHelpers.tsx";
import type { BulkFormat } from "./bulkHelpers.tsx";

export function JobDetailPanel({ job }: { job: ExportJob }) {
  const { cancelJob, dismissJob, reprocessJob } = useBulkExport();
  const navigate = useNavigate();
  const [resourceCounts, setResourceCounts] = useState<Record<string, number>>({});
  const [piiData, setPiiData] = useState<PiiDetectionMap>({});
  const [fieldSummary, setFieldSummary] = useState<FieldSummaryMap>({});
  const [allResources, setAllResources] = useState<Record<string, unknown>[]>([]);
  const [rawNdjson, setRawNdjson] = useState("");
  const [selectedFormat, setSelectedFormat] = useState<BulkFormat>("ndjson");
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [reprocessing, setReprocessing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<{ uploaded: number; errors: number } | null>(null);

  // Fetch and parse result when job is done
  useEffect(() => {
    if (!job.jobId || job.status !== "done" || parsed || parsing) return;
    let cancelled = false;
    setParsing(true);
    (async () => {
      try {
        const blob = await getJobResult(job.jobId!);
        if (cancelled) return;
        const { counts, allFieldCounts, piiSample, allResources: ar, rawNdjson: raw } = await parseNdjsonBlob(blob);
        if (cancelled) return;
        const pii = buildPiiFromDeidentifiedOnly(piiSample);
        if (cancelled) return;
        setResourceCounts(counts);
        setPiiData(pii);
        setFieldSummary(buildFieldSummary(allFieldCounts, pii));
        setAllResources(ar);
        setRawNdjson(raw);
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

  const handleDownload = useCallback(async () => {
    if (downloading) return;
    setDownloading(true);
    const base = job.filename.replace(/\.ndjson$/, "");
    try {
      // Yield one frame so the spinner renders before heavy serialization
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
      switch (selectedFormat) {
        case "ndjson":
          // rawNdjson is pre-built and pre-stripped — fastest path
          triggerDownload(rawNdjson, `${base}.ndjson`, "application/x-ndjson");
          break;
        case "bundle":
          triggerDownload(
            JSON.stringify({
              resourceType: "Bundle", id: crypto.randomUUID(), type: "collection",
              timestamp: new Date().toISOString(), total: allResources.length,
              entry: allResources.map((resource) => ({ resource })),
            }, null, 2),
            `${base}.bundle.json`,
            "application/fhir+json",
          );
          break;
        case "json-array":
          triggerDownload(JSON.stringify(allResources, null, 2), `${base}.json`, "application/json");
          break;
        case "patient-bundles":
          triggerDownload(
            buildPatientBundlesNdjson(allResources),
            `${base}.patient-bundles.ndjson`,
            "application/x-ndjson",
          );
          break;
      }
    } finally {
      setDownloading(false);
    }
  }, [selectedFormat, allResources, rawNdjson, job.filename, downloading, setDownloading]);

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

  function handleReprocess() {
    if (reprocessing) return;
    setReprocessing(true);
    const newId = reprocessJob(job.id, job.configProfile);
    navigate("/bulk-deidentify", { state: { autoSelectId: newId } });
  }

  async function handleUploadToTarget() {
    if (uploading || !job.jobId) return;
    setUploading(true);
    setUploadResult(null);
    try {
      const result = await uploadJobToTarget(job.jobId);
      setUploadResult({ uploaded: result.uploaded, errors: result.errors });
    } catch (err) {
      setUploadResult({ uploaded: 0, errors: -1 });
      setFetchError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setUploading(false);
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
        {job.source === "patient" && (
          <Badge variant="secondary" className="text-xs gap-1">
            <User className="size-3" />
            {job.patientName || "Patient"}
          </Badge>
        )}
        {job.source === "condition" && job.conditionName && (
          <Badge variant="secondary" className="text-xs gap-1">
            <Stethoscope className="size-3" />
            {job.conditionName}
          </Badge>
        )}
        {job.source === "all" && (
          <Badge variant="secondary" className="text-xs gap-1">
            <Database className="size-3" />
            Full Export
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
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-4">
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
            {job.stagedCount != null && job.stagedCount > 0 && (
              <div>
                <span className="text-muted-foreground">Staged</span>
                <p className="font-medium tabular-nums">{job.stagedCount}</p>
              </div>
            )}
            <div>
              <span className="text-muted-foreground">Started</span>
              <p className="font-medium">{new Date(job.startedAt).toLocaleTimeString()}</p>
            </div>
          </div>
          {job.stagedCount != null && job.stagedCount > 0 && job.phase === "processing" ? (
            <div className="mt-3">
              <Progress
                value={Math.round((job.processed / job.stagedCount) * 100)}
                className="h-1.5"
              />
              <p className="mt-0.5 text-[10px] text-muted-foreground text-right tabular-nums">
                {Math.round((job.processed / job.stagedCount) * 100)}%
              </p>
            </div>
          ) : (
            <Progress value={100} className="mt-3 h-1.5 [&>div]:animate-pulse" />
          )}
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

      {/* Download + Send to Target */}
      {isDone && job.processed > 0 && (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2">
            <Select
              value={selectedFormat}
              onValueChange={(v) => setSelectedFormat(v as BulkFormat)}
              disabled={!parsed}
            >
              <SelectTrigger className="w-[210px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {FORMAT_OPTIONS.map((opt) => (
                  <SelectItem key={opt.value} value={opt.value}>
                    <div>
                      <p className="font-medium text-sm">{opt.label}</p>
                      <p className="text-xs text-muted-foreground">{opt.description}</p>
                    </div>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button onClick={handleDownload} disabled={!parsed || downloading}>
              {parsing || downloading ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Download className="size-4" />
              )}
              {downloading
                ? "Building…"
                : parsed
                  ? `Download (${totalResources})`
                  : "Preparing…"}
            </Button>
            <Button
              variant="outline"
              onClick={handleUploadToTarget}
              disabled={uploading || !job.jobId}
            >
              {uploading ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Upload className="size-4" />
              )}
              {uploading ? "Sending…" : "Send to Target"}
            </Button>
          </div>
          {uploadResult && uploadResult.errors !== -1 && (
            <div className="flex items-center gap-2 text-sm">
              <CheckCircle2 className="size-4 text-green-600" />
              <span>
                Uploaded {uploadResult.uploaded} resource{uploadResult.uploaded !== 1 ? "s" : ""} to target server
                {uploadResult.errors > 0 && (
                  <span className="text-destructive"> ({uploadResult.errors} error{uploadResult.errors !== 1 ? "s" : ""})</span>
                )}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Reprocess — only available when staging is configured (stagedCount present) */}
      {isTerminal && job.stagedCount != null && job.jobId && (
        <div className="mt-4 flex items-center gap-2 border-t pt-4">
          <Button
            variant="outline"
            size="sm"
            className="text-xs gap-1.5"
            onClick={handleReprocess}
            disabled={reprocessing}
          >
            {reprocessing ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RefreshCw className="size-3.5" />
            )}
            Re-process with same profile
          </Button>
          <span className="text-xs text-muted-foreground">
            Replays staged rows — no new FHIR fetch required
          </span>
        </div>
      )}
    </div>
  );
}
