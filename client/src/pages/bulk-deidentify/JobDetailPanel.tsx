import { useState, useEffect, useCallback, useRef } from "react";
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
  RefreshCw, User, Users, Stethoscope, Database, Upload, ShieldCheck,
} from "lucide-react";
import { getJobResult, getJobStatus, submitBulkImport, getJobScore, triggerJobScore, getJobDetail, saveJobDetail } from "@/api/medanon";
import type { JobResponse, BatchPrivacy, UploadErrorDetail } from "@/api/medanon";
import { useBulkExport } from "@/context/BulkExportContext";
import type { ExportJob } from "@/context/BulkExportContext";
import { buildPiiFromDeidentifiedOnly, buildFieldSummary, stripManifestTag } from "@/lib/piiDetection";
import type { PiiDetectionMap, FieldSummaryMap } from "@/lib/piiDetection";
import { getGradeStyle } from "@/lib/qualityScore";
import type { LetterGrade } from "@/lib/qualityScore";
import {
  Tooltip, TooltipTrigger, TooltipContent, TooltipProvider,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import {
  PHASE_LABELS,
  parseNdjsonBlob,
  FORMAT_OPTIONS,
  buildPatientBundlesNdjson,
  triggerDownload,
} from "./bulkHelpers.tsx";
import type { BulkFormat } from "./bulkHelpers.tsx";
import { StatusIcon, statusBadge } from "./bulkStatusComponents.tsx";

export function JobDetailPanel({ job }: { job: ExportJob }) {
  const { cancelJob, dismissJob, reprocessJob, setJobBackendScore } = useBulkExport();
  const navigate = useNavigate();
  const [resourceCounts, setResourceCounts] = useState<Record<string, number>>({});
  const [piiData, setPiiData] = useState<PiiDetectionMap>({});
  const [fieldSummary, setFieldSummary] = useState<FieldSummaryMap>({});
  const [totalResources, setTotalResources] = useState(0);
  const [selectedFormat, setSelectedFormat] = useState<BulkFormat>("ndjson");
  const [parsing, setParsing] = useState(false);
  const [parsed, setParsed] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [reprocessing, setReprocessing] = useState(false);
  const [importJob, setImportJob] = useState<JobResponse | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [confirmDismiss, setConfirmDismiss] = useState(false);
  const importPollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Cache parsed job ID to avoid re-parsing the same result
  const parsedJobIdRef = useRef<string | null>(null);

  // Stop import polling on unmount
  useEffect(() => () => {
    if (importPollRef.current !== null) clearInterval(importPollRef.current);
  }, []);

  // Fetch and parse result when job is done, tries backend cache first.
  // Resource counts are shown immediately from job.summary (already fetched).
  useEffect(() => {
    // Import jobs have no NDJSON result to parse
    if (isImport) return;
    // Skip if already parsed for this exact job
    if (!job.jobId || job.status !== "done" || parsing) return;
    if (parsedJobIdRef.current === job.jobId) return;

    // Show resource counts immediately from the API summary, no download needed
    if (job.summary?.resource_type_counts) {
      const counts = job.summary.resource_type_counts;
      const total = job.summary.total_resources ?? Object.values(counts).reduce((a, b) => a + b, 0);
      setResourceCounts(counts);
      setTotalResources(total);
    }

    let cancelled = false;
    setParsing(true);
    setFetchError(null);
    (async () => {
      try {
        // Try backend cache first, avoids downloading the NDJSON entirely
        try {
          const cached = await getJobDetail(job.jobId!);
          if (cancelled) return;
          setResourceCounts(cached.resource_counts);
          setPiiData(cached.pii_data as PiiDetectionMap);
          setFieldSummary(cached.field_summary as FieldSummaryMap);
          setTotalResources(cached.total_resources);
          setParsed(true);
          parsedJobIdRef.current = job.jobId!;
          // Fetch score even on cache hit
          try {
            let scoreResult = await getJobScore(job.jobId!);
            if (!scoreResult.computed) scoreResult = await triggerJobScore(job.jobId!);
            setJobBackendScore(job.id, scoreResult);
          } catch { /* non-fatal */ }
          return; // skip NDJSON download
        } catch {
          // cache miss (404) or store unavailable (503), fall through to NDJSON parse
        }

        // Cache miss: download and parse NDJSON
        const blob = await getJobResult(job.jobId!);
        if (cancelled) return;
        // Yield a frame so the loading spinner renders before heavy parsing
        await new Promise<void>((r) => setTimeout(r, 0));
        const { counts, allFieldCounts, piiSample, totalResources: total } = await parseNdjsonBlob(blob);
        if (cancelled) return;
        await new Promise<void>((r) => setTimeout(r, 0));
        const pii = buildPiiFromDeidentifiedOnly(piiSample);
        if (cancelled) return;
        const builtFieldSummary = buildFieldSummary(allFieldCounts, pii, counts);
        setResourceCounts(counts);
        setPiiData(pii);
        setFieldSummary(builtFieldSummary);
        setTotalResources(total);
        setParsed(true);
        parsedJobIdRef.current = job.jobId!;

        // Save parsed result to backend cache (fire-and-forget)
        saveJobDetail(job.jobId!, {
          resource_counts: counts,
          total_resources: total,
          pii_data: pii as Record<string, unknown>,
          field_summary: builtFieldSummary as Record<string, unknown>,
        }).catch(() => {}); // cache write failures are non-fatal

        // Fetch backend score
        try {
          let scoreResult = await getJobScore(job.jobId!);
          if (!scoreResult.computed) {
            scoreResult = await triggerJobScore(job.jobId!);
          }
          setJobBackendScore(job.id, scoreResult);
        } catch (scoreErr) {
          console.warn("Failed to fetch backend score:", scoreErr);
        }
      } catch (err) {
        if (!cancelled)
          setFetchError(err instanceof Error ? err.message : "Failed to fetch results");
      } finally {
        if (!cancelled) setParsing(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.jobId, job.status]);

  const handleDownload = useCallback(async () => {
    if (downloading || !job.jobId) return;
    setDownloading(true);
    const base = job.filename.replace(/\.ndjson$/, "");
    try {
      if (selectedFormat === "ndjson") {
        // Fastest path: download the blob directly, zero client-side parsing
        const blob = await getJobResult(job.jobId);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${base}.ndjson`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } else {
        // Other formats: fetch blob, parse transiently, format, download
        const blob = await getJobResult(job.jobId);
        const text = await blob.text();
        const resources: Record<string, unknown>[] = [];
        for (const line of text.split("\n")) {
          const trimmed = line.trim();
          if (!trimmed) continue;
          try {
            resources.push(stripManifestTag(JSON.parse(trimmed) as Record<string, unknown>));
          } catch { /* skip */ }
        }
        // Yield one frame so the spinner renders before heavy serialization
        await new Promise<void>((resolve) => setTimeout(resolve, 0));
        switch (selectedFormat) {
          case "bundle":
            triggerDownload(
              JSON.stringify({
                resourceType: "Bundle", id: crypto.randomUUID(), type: "collection",
                timestamp: new Date().toISOString(), total: resources.length,
                entry: resources.map((resource) => ({ resource })),
              }, null, 2),
              `${base}.bundle.json`,
              "application/fhir+json",
            );
            break;
          case "json-array":
            triggerDownload(JSON.stringify(resources, null, 2), `${base}.json`, "application/json");
            break;
          case "patient-bundles":
            triggerDownload(
              buildPatientBundlesNdjson(resources),
              `${base}.patient-bundles.ndjson`,
              "application/x-ndjson",
            );
            break;
        }
      }
    } catch (err) {
      setFetchError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  }, [selectedFormat, job.jobId, job.filename, downloading]);

  const isImport = job.type === 'bulk-import';
  const isDone = job.status === "done";
  const isRunning =
    job.status === "submitting" ||
    job.status === "pending" ||
    job.status === "running";
  const isCancelled = job.status === "cancelled";
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

  async function handleStartUpload() {
    if (!job.jobId || importJob?.status === "running" || importJob?.status === "pending") return;
    setUploadError(null);
    setImportJob(null);
    try {
      const imp = await submitBulkImport({ job_id: job.jobId });
      setImportJob(imp);
      // Start polling
      if (importPollRef.current !== null) clearInterval(importPollRef.current);
      importPollRef.current = setInterval(async () => {
        try {
          const updated = await getJobStatus(imp.job_id);
          setImportJob(updated);
          if (updated.status === "done" || updated.status === "error" || updated.status === "cancelled") {
            clearInterval(importPollRef.current!);
            importPollRef.current = null;
          }
        } catch {
          clearInterval(importPollRef.current!);
          importPollRef.current = null;
        }
      }, 2000);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Failed to start upload");
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
          confirmDismiss ? (
            <div className="flex items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Dismiss this job?</span>
              <Button
                variant="destructive"
                size="sm"
                className="text-xs"
                onClick={() => dismissJob(job.id)}
              >
                Confirm
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className="text-xs"
                onClick={() => setConfirmDismiss(false)}
              >
                Cancel
              </Button>
            </div>
          ) : (
            <Button
              variant="ghost"
              size="sm"
              className="text-xs text-muted-foreground"
              onClick={() => setConfirmDismiss(true)}
            >
              Dismiss
            </Button>
          )
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
        {job.source === "patients" && (
          <Badge variant="secondary" className="text-xs gap-1">
            <Users className="size-3" />
            {job.patientCount ? `${job.patientCount} Patients` : "Multiple Patients"}
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
        {job.backendScore?.computed && (() => {
          const avg = job.backendScore.avg_composite ?? 0;
          const grade: LetterGrade = avg >= 90 ? "A" : avg >= 75 ? "B" : avg >= 60 ? "C" : avg >= 40 ? "D" : "F";
          const s = getGradeStyle(grade);
          const decision = job.backendScore.pass_count != null && job.backendScore.total_scored != null
            ? `${job.backendScore.pass_count}/${job.backendScore.total_scored} passed`
            : "";
          return (
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger>
                  <span className={cn(
                    "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold",
                    s.bg, s.text, s.border,
                  )}>
                    <ShieldCheck className="size-3" />
                    {grade} ({Math.round(avg)}%)
                  </span>
                </TooltipTrigger>
                <TooltipContent>
                  <div className="text-xs space-y-1 min-w-[180px]">
                    <p className="font-semibold">De-identification Quality</p>
                    <p>Composite: {Math.round(avg)}%</p>
                    {job.backendScore.avg_utility != null && <p>Utility: {Math.round(job.backendScore.avg_utility * 100)}%</p>}
                    {job.backendScore.avg_quality != null && <p>Quality: {Math.round(job.backendScore.avg_quality * 100)}%</p>}
                    {job.backendScore.batch_privacy && (() => {
                      const bp = job.backendScore.batch_privacy as BatchPrivacy;
                      return (
                        <>
                          <hr className="border-muted my-1" />
                          <p className="font-semibold">Privacy Gate ({bp.passed ? "PASS" : "FAIL"})</p>
                          <p>Risk score: {(bp.risk_score * 100).toFixed(1)}% (threshold {(bp.threshold * 100).toFixed(0)}%)</p>
                          <p>Attacker: {(bp.attacker_risk * 100).toFixed(1)}%</p>
                          <p>Identifiers: {(bp.identifier_risk * 100).toFixed(1)}%</p>
                          <p>Config coverage: {(bp.config_identifier_risk * 100).toFixed(1)}%</p>
                          <p>Text scan: {(bp.text_risk * 100).toFixed(1)}%</p>
                        </>
                      );
                    })()}
                    <hr className="border-muted my-1" />
                    <p>{decision}</p>
                  </div>
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
          );
        })()}
        {job.backendScore && !job.backendScore.computed && (
          <span className="text-xs text-muted-foreground">No data to score</span>
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
          {job.stagedCount != null && job.stagedCount > 0 && (job.phase === "processing" || job.phase === "uploading") ? (
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

      {/* Empty result, only show after parsing completes with 0 results */}
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
      {isDone && job.processed > 0 && !isImport && (
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
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              onClick={handleStartUpload}
              disabled={!job.jobId || importJob?.status === "running" || importJob?.status === "pending"}
            >
              {(importJob?.status === "running" || importJob?.status === "pending") ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Upload className="size-4" />
              )}
              {(importJob?.status === "running" || importJob?.status === "pending")
                ? "Uploading…"
                : importJob?.status === "done"
                  ? "Re-upload"
                  : "Send to Target"}
            </Button>
          </div>
          {/* Upload progress bar */}
          {(importJob?.status === "running" || importJob?.status === "pending") && (
            <div className="flex flex-col gap-1">
              {(importJob.staged_count ?? 0) > 0 ? (
                <>
                  <Progress
                    value={Math.round((importJob.processed / importJob.staged_count!) * 100)}
                    className="h-1.5"
                  />
                  <p className="text-[10px] text-muted-foreground tabular-nums text-right">
                    {importJob.processed.toLocaleString()} / {importJob.staged_count!.toLocaleString()} uploaded
                    {" "}({Math.round((importJob.processed / importJob.staged_count!) * 100)}%)
                  </p>
                </>
              ) : (
                <>
                  <Progress value={100} className="h-1.5 [&>div]:animate-pulse" />
                  <p className="text-[10px] text-muted-foreground">
                    {importJob.phase === "loading" ? "Reading export file…" : "Uploading…"}
                  </p>
                </>
              )}
            </div>
          )}
          {/* Upload done */}
          {importJob?.status === "done" && (() => {
            const total = importJob.staged_count ?? importJob.processed;
            const failCount = importJob.upload_errors ?? 0;
            const successCount = importJob.processed;
            const details = importJob.upload_error_details ?? [];
            // Group errors by type for a compact summary
            const byType: Record<string, { count: number; sample: string }> = {};
            for (const d of details) {
              if (!byType[d.resourceType]) byType[d.resourceType] = { count: 0, sample: d.error };
              byType[d.resourceType].count++;
            }
            return (
              <div className="flex flex-col gap-1.5 text-sm">
                <div className="flex items-center gap-2">
                  {failCount === 0 ? (
                    <CheckCircle2 className="size-4 text-green-600 shrink-0" />
                  ) : (
                    <AlertCircle className="size-4 text-amber-500 shrink-0" />
                  )}
                  <span>
                    Uploaded <span className="font-medium tabular-nums">{successCount.toLocaleString()}</span>
                    {" "}of <span className="tabular-nums">{total.toLocaleString()}</span> resources
                    {failCount > 0 && (
                      <span className="text-amber-600 ml-1">
                       , {failCount.toLocaleString()} rejected by target server
                      </span>
                    )}
                  </span>
                </div>
                {/* Per-type error breakdown */}
                {Object.keys(byType).length > 0 && (
                  <div className="ml-6 flex flex-col gap-0.5">
                    {Object.entries(byType).map(([rt, info]) => (
                      <p key={rt} className="text-xs text-muted-foreground">
                        <span className="font-medium text-destructive/80">{rt}</span>
                        {" "}({info.count} rejected):{" "}
                        <span className="italic">{info.sample}</span>
                      </p>
                    ))}
                    {failCount > details.length && (
                      <p className="text-xs text-muted-foreground">
                        …and {(failCount - details.length).toLocaleString()} more. Check server logs for details.
                      </p>
                    )}
                  </div>
                )}
              </div>
            );
          })()}
          {/* Upload submit error (before job started) */}
          {!importJob && uploadError && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4 shrink-0" />
              {uploadError}
            </div>
          )}
          {/* Upload job failed (worker-level error, not FHIR rejections) */}
          {importJob?.status === "error" && (
            <div className="flex items-center gap-2 text-sm text-destructive">
              <AlertCircle className="size-4 shrink-0" />
              {importJob.error ?? "Upload failed, check server logs for details"}
            </div>
          )}
        </div>
      )}

      {/* Import result summary, shown for recovered/completed bulk-import jobs */}
      {isDone && isImport && (() => {
        const total = job.stagedCount ?? job.processed;
        const failCount = job.uploadErrors ?? 0;
        const successCount = job.processed;
        const details: UploadErrorDetail[] = job.uploadErrorDetails ?? [];
        const byType: Record<string, { count: number; sample: string }> = {};
        for (const d of details) {
          if (!byType[d.resourceType]) byType[d.resourceType] = { count: 0, sample: d.error };
          byType[d.resourceType].count++;
        }
        return (
          <div className="flex flex-col gap-1.5 text-sm">
            <div className="flex items-center gap-2">
              {failCount === 0 ? (
                <CheckCircle2 className="size-4 text-green-600 shrink-0" />
              ) : (
                <AlertCircle className="size-4 text-amber-500 shrink-0" />
              )}
              <span>
                Uploaded <span className="font-medium tabular-nums">{successCount.toLocaleString()}</span>
                {' '}of <span className="tabular-nums">{total.toLocaleString()}</span> resources
                {failCount > 0 && (
                  <span className="text-amber-600 ml-1">
                   , {failCount.toLocaleString()} rejected by target server
                  </span>
                )}
              </span>
            </div>
            {Object.keys(byType).length > 0 && (
              <div className="ml-6 flex flex-col gap-0.5">
                {Object.entries(byType).map(([rt, info]) => (
                  <p key={rt} className="text-xs text-muted-foreground">
                    <span className="font-medium text-destructive/80">{rt}</span>
                    {' '}({info.count} rejected):{' '}
                    <span className="italic">{info.sample}</span>
                  </p>
                ))}
                {failCount > details.length && (
                  <p className="text-xs text-muted-foreground">
                    …and {(failCount - details.length).toLocaleString()} more. Check server logs for details.
                  </p>
                )}
              </div>
            )}
          </div>
        );
      })()}

      {/* Reprocess, only available when staging is configured (stagedCount present) */}
      {isTerminal && job.stagedCount != null && job.jobId && !isImport && (
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
            Replays staged rows, no new FHIR fetch required
          </span>
        </div>
      )}
    </div>
  );
}
