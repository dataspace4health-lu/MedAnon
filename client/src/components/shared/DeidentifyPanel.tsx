import { useState, useCallback, useRef, useMemo, useEffect } from "react";
import { toast } from "sonner";
import { Play, Square, AlertCircle, GitCompare, FileJson, TableProperties, Maximize2, Minimize2, Upload, CheckCircle2, Loader2, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { StreamProgress } from "@/components/shared/StreamProgress";
import { ResourceTypeSummary } from "@/components/shared/ResourceTypeSummary";
import { FhirCodeViewer } from "@/components/shared/FhirCodeViewer";
import { JsonDiffViewer } from "@/components/shared/JsonDiffViewer";
import { MultiFormatDownload } from "@/components/shared/MultiFormatDownload";
import { FhirTableView } from "@/components/shared/FhirTableView";
import { buildPiiFromDeidentifiedOnly, buildFieldSummary, stripManifestTag } from "@/lib/piiDetection";
import { extractFieldsDeep } from "@/lib/fhirFields";
import { getAuthHeaders } from "@/api/client";
import { uploadToTarget } from "@/api/medanon";
import { listDestinations, type OutputDestination } from "@/api/connectors";
import { getJobStatus, submitPatientExportJob } from "@/api/jobs";
import type { BatchPrivacy } from "@/api/jobs";
import { getGradeStyle } from "@/lib/qualityScore";
import type { LetterGrade } from "@/lib/qualityScore";
import {
  Tooltip, TooltipTrigger, TooltipContent, TooltipProvider,
} from "@/components/ui/tooltip";

interface DeidentifyPanelProps {
  patientId: string;
  patientName: string;
  configProfile: string;
}

/**
 * Max resources the inline preview sends to /process/batch.
 *
 * $everything is fetched with _count=5000; a dense patient can serialize to far
 * more than the server's 10 MB body cap (MEDANON_MAX_BODY_BYTES), which rejects
 * on Content-Length and drops the connection. The preview is a preview, the
 * full record is released through the async export job.
 */
const PREVIEW_RESOURCE_LIMIT = 500;

/**
 * PHI-safe download base name. The source `patientId` is a real identifier and
 * must never appear in a filename, even on de-identified content. Prefer the
 * de-identified output Patient's (pseudonymized) id when the profile changed it;
 * otherwise fall back to a generic timestamped name.
 */
function safeDownloadBase(
  resources: Record<string, unknown>[],
  sourceId: string,
): string {
  const patient = resources.find((r) => r?.resourceType === "Patient");
  const outId = typeof patient?.id === "string" ? patient.id : "";
  if (outId && outId !== sourceId) return `deidentified-${outId}`;
  const ts = new Date()
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d+Z$/, "Z");
  return `deidentified-patient-${ts}`;
}

interface StreamState {
  isStreaming: boolean;
  resources: Record<string, unknown>[];
  originalResources: Record<string, unknown>[];
  resourceCounts: Record<string, number>;
  errorCount: number;
  error: string | null;
  score: Record<string, unknown> | null;
}

import { cn } from "@/lib/utils";

function ScoreBadge({ score }: { score: Record<string, unknown> }) {
  const avg = typeof score.avg_composite === "number" ? score.avg_composite : null;
  const avgUtility = typeof score.avg_utility === "number" ? score.avg_utility : null;
  const avgQuality = typeof score.avg_quality === "number" ? score.avg_quality : null;
  const batchPrivacy = score.batch_privacy && typeof score.batch_privacy === "object"
    ? (score.batch_privacy as BatchPrivacy)
    : null;
  const passCount   = typeof score.pass_count   === "number" ? score.pass_count   : null;
  const totalScored = typeof score.total_scored === "number" ? score.total_scored : null;

  if (avg === null) return null;

  const grade: LetterGrade = avg >= 90 ? "A" : avg >= 75 ? "B" : avg >= 60 ? "C" : avg >= 40 ? "D" : "F";
  const s = getGradeStyle(grade);
  const decision = passCount != null && totalScored != null ? `${passCount}/${totalScored} passed` : "";

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger>
          <span className={cn(
            "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-xs font-semibold cursor-default",
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
            {avgUtility != null && <p>Utility: {Math.round(avgUtility * 100)}%</p>}
            {avgQuality != null && <p>Quality: {Math.round(avgQuality * 100)}%</p>}
            {batchPrivacy && (
              <>
                <hr className="border-muted my-1" />
                <p className="font-semibold">Privacy Gate ({batchPrivacy.passed ? "PASS" : "FAIL"})</p>
                <p>Risk score: {(batchPrivacy.risk_score * 100).toFixed(1)}% (threshold {(batchPrivacy.threshold * 100).toFixed(0)}%)</p>
                <p>Attacker: {(batchPrivacy.attacker_risk * 100).toFixed(1)}%</p>
                <p>Identifiers: {(batchPrivacy.identifier_risk * 100).toFixed(1)}%</p>
                <p>Config coverage: {(batchPrivacy.config_identifier_risk * 100).toFixed(1)}%</p>
                <p>Text scan: {(batchPrivacy.text_risk * 100).toFixed(1)}%</p>
              </>
            )}
            {decision && (
              <>
                <hr className="border-muted my-1" />
                <p>{decision}</p>
              </>
            )}
          </div>
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export function DeidentifyPanel({
  patientId,
  patientName,
  configProfile,
}: DeidentifyPanelProps) {
  const [state, setState] = useState<StreamState>({
    isStreaming: false,
    resources: [],
    originalResources: [],
    resourceCounts: {},
    errorCount: 0,
    error: null,
    score: null,
  });
  const [activeTab, setActiveTab] = useState<"output" | "diff" | "table">("output");
  const [fullView, setFullView] = useState(false);
  // > 0 when the preview was capped: holds the TRUE resource count so the UI can
  // say how much of the record it is not showing.
  const [previewTruncated, setPreviewTruncated] = useState(0);
  const abortRef = useRef<AbortController | null>(null);

  // Saved S3 destinations for the optional "send to S3" delivery (admin only;
  // a 403/empty for non-admins is non-fatal, the control just stays hidden).
  const [destinations, setDestinations] = useState<OutputDestination[]>([]);
  const [selectedDest, setSelectedDest] = useState<string>("");
  const [delivering, setDelivering] = useState(false);
  useEffect(() => {
    listDestinations()
      .then((d) => {
        setDestinations(d);
        if (d.length > 0) setSelectedDest(d[0].id);
      })
      .catch(() => setDestinations([]));
  }, []);

  const handleRun = useCallback(async () => {
    abortRef.current = new AbortController();
    const signal = abortRef.current.signal;

    setState({
      isStreaming: true,
      resources: [],
      originalResources: [],
      resourceCounts: {},
      errorCount: 0,
      error: null,
      score: null,
    });
    setPreviewTruncated(0);
    setActiveTab("output");

    try {
      // Step 1: Fetch $everything from the FHIR server via the frontend proxy.
      const fhirRes = await fetch(
        `/fhir/Patient/${patientId}/$everything?_count=5000`,
        {
          headers: { Accept: "application/fhir+json" },
          signal,
        }
      );

      if (!fhirRes.ok) {
        throw new Error(
          `FHIR server returned ${fhirRes.status} for $everything`
        );
      }

      const bundle = await fhirRes.json();

      const allResources: Record<string, unknown>[] = (
        (bundle.entry as Array<{ resource?: Record<string, unknown> }>) ?? []
      )
        .map((e) => e.resource)
        .filter((r): r is Record<string, unknown> => !!r);

      // This panel is a PREVIEW. Posting an unbounded $everything bundle (up to
      // _count=5000 resources) routinely exceeds the server's 10 MB body cap
      // (MEDANON_MAX_BODY_BYTES), which rejects on Content-Length and closes the
      // connection, surfacing in the browser as "Failed to fetch" rather than a
      // readable 413. Cap what we send; the full dataset is released through the
      // async export job (Deliver to S3), which never round-trips the browser.
      const truncated = allResources.length > PREVIEW_RESOURCE_LIMIT;
      const originalResources = truncated
        ? allResources.slice(0, PREVIEW_RESOURCE_LIMIT)
        : allResources;

      setState((prev) => ({ ...prev, originalResources }));
      setPreviewTruncated(truncated ? allResources.length : 0);

      // Step 2: Stream the bundle through /process/batch for de-identification.
      const params = new URLSearchParams({ config_profile: configProfile });
      const response = await fetch(`/api/v1/process/batch?${params}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getAuthHeaders(),
        },
        body: JSON.stringify({
          resourceType: "Bundle",
          type: "collection",
          entry: originalResources.map((resource) => ({ resource })),
        }),
        signal,
      });

      if (!response.ok) {
        let detail = `Processing failed (${response.status})`;
        if (response.status === 413) {
          detail =
            "This patient's record is too large to preview inline. Use \"Deliver to S3\" to run it as an export job.";
        } else {
          try {
            const body = await response.json();
            if (body?.detail) detail = String(body.detail);
          } catch {
            // ignore
          }
        }
        throw new Error(detail);
      }

      if (!response.body) {
        throw new Error("No response body received");
      }

      // Step 3: Stream NDJSON lines from the response.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const collectedResources: Record<string, unknown>[] = [];
      const counts: Record<string, number> = {};
      let errors = 0;
      let fatalError: string | null = null;
      let streamScore: Record<string, unknown> | null = null;
      let lastFlush = 0;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          const trimmed = line.trim();
          if (!trimmed) continue;

          try {
            const parsed = JSON.parse(trimmed) as Record<string, unknown>;

            // Trailer line, capture score and skip as resource
            if ("__stream_complete" in parsed) {
              if (parsed.score && typeof parsed.score === "object") {
                streamScore = parsed.score as Record<string, unknown>;
              }
              continue;
            }

            const resource =
              "data" in parsed
                ? (parsed.data as Record<string, unknown>)
                : parsed;

            if ("error" in parsed && !("data" in parsed)) {
              errors++;
              if (parsed.fatal) {
                fatalError = String(parsed.error ?? "Fatal processing error, stream halted");
              }
              continue;
            }

            collectedResources.push(resource);
            const resourceType =
              (resource.resourceType as string) ?? "Unknown";
            counts[resourceType] = (counts[resourceType] ?? 0) + 1;
          } catch (parseError) {
            console.error("Failed to parse NDJSON line:", trimmed, parseError);
            errors++;
          }
        }

        if (fatalError) break;

        // Throttle UI updates to avoid excessive re-renders
        const now = Date.now();
        if (now - lastFlush >= 250) {
          lastFlush = now;
          setState((prev) => ({
            ...prev,
            resources: collectedResources.slice(),
            resourceCounts: { ...counts },
            errorCount: errors,
          }));
        }
      }

      // Flush any remaining buffer
      if (buffer.trim()) {
        try {
          const parsed = JSON.parse(buffer.trim()) as Record<string, unknown>;
          const resource =
            "data" in parsed ? (parsed.data as Record<string, unknown>) : parsed;
          if (!("error" in parsed && !("data" in parsed))) {
            collectedResources.push(resource);
            const resourceType =
              (resource.resourceType as string) ?? "Unknown";
            counts[resourceType] = (counts[resourceType] ?? 0) + 1;
          }
        } catch {
          errors++;
        }
      }

      setState((prev) => ({
        ...prev,
        isStreaming: false,
        resources: collectedResources,
        resourceCounts: counts,
        errorCount: errors,
        error: fatalError,
        score: streamScore,
      }));
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        setState((prev) => ({ ...prev, isStreaming: false }));
        return;
      }
      setState((prev) => ({
        ...prev,
        isStreaming: false,
        error: err instanceof Error ? err.message : "An unknown error occurred",
      }));
    }
  }, [patientId, configProfile]);

  const handleAbort = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // Strip manifest tags for output/download (keep internal for PII detection)
  const cleanResources = useMemo(
    () => state.resources.map(stripManifestTag),
    [state.resources],
  );

  // PHI-safe download filename base, never the real source patient id.
  const downloadBase = useMemo(
    () => safeDownloadBase(cleanResources, patientId),
    [cleanResources, patientId],
  );

  const handleXmlDownload = useCallback(async () => {
    try {
      // Use already-processed resources instead of re-processing originals
      // (which would generate different pseudonyms).
      const response = await fetch(`/api/v1/process/raw?config_profile=${configProfile}&output_format=xml`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getAuthHeaders(),
        },
        body: JSON.stringify({
          resourceType: "Bundle",
          type: "collection",
          entry: cleanResources.map(resource => ({ resource })),
        }),
      });

      if (!response.ok) {
        throw new Error(`Failed to generate XML: ${response.status}`);
      }

      const xmlData = await response.text();
      const blob = new Blob([xmlData], { type: "application/fhir+xml" });
      const url = URL.createObjectURL(blob);

      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${downloadBase}.xml`;
      document.body.appendChild(anchor);
      anchor.click();

      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Failed to download XML:", error);
      alert("Failed to generate XML download. Please try again or use a different format.");
    }
  }, [downloadBase, configProfile, cleanResources]);

  // Deliver to S3 by queueing a patient-export JOB, not by POSTing the resources.
  //
  // The panel used to serialize every original resource into one Bundle and POST
  // it to /process/batch. That shipped raw PHI back through the browser and blew
  // past MEDANON_MAX_BODY_BYTES (10 MB) on any sizeable patient, the server
  // rejected on Content-Length and closed the connection mid-upload, which the
  // browser surfaces as "TypeError: Failed to fetch", not a readable 413.
  //
  // The job carries only the patient id: the worker re-fetches $everything
  // server-side, so raw PHI never leaves the server, the body is a few hundred
  // bytes, and the release runs through the score gate before it is delivered.
  const handleDeliverToS3 = useCallback(async () => {
    if (!selectedDest) return;
    setDelivering(true);
    const dest = destinations.find((d) => d.id === selectedDest);
    const destLabel = dest?.name ?? selectedDest;
    try {
      const job = await submitPatientExportJob({
        patient_id: patientId,
        config_profile: configProfile,
        destination_id: selectedDest,
      });
      const jobId = job.job_id;
      if (!jobId) throw new Error("Export job did not return a job id");
      toast.info(`Export queued for ${destLabel}…`);

      // Poll to completion so the button reflects the real outcome, a job can
      // still be withheld by the score gate after it is accepted.
      for (;;) {
        await new Promise((r) => setTimeout(r, 2000));
        const status = await getJobStatus(jobId);
        if (status.status === "done") {
          toast.success(`Delivered to S3 (${destLabel})`);
          return;
        }
        if (status.status === "error") {
          throw new Error(status.error ?? "Export job failed");
        }
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "S3 delivery failed");
    } finally {
      setDelivering(false);
    }
  }, [selectedDest, configProfile, patientId, destinations]);

  // Limit resources for diff/table views to prevent browser freeze
  const DIFF_LIMIT = 200;
  const resourceCount = cleanResources.length;
  const isDiffLimited = resourceCount > DIFF_LIMIT;

  const jsonOutput = useMemo(() => {
    if (activeTab !== "output" && activeTab !== "diff") return "";
    const subset = isDiffLimited && activeTab === "diff" ? cleanResources.slice(0, DIFF_LIMIT) : cleanResources;
    return JSON.stringify(subset, null, 2);
  }, [cleanResources, activeTab, isDiffLimited]);

  const jsonOriginal = useMemo(() => {
    if (activeTab !== "diff") return "";
    const subset = isDiffLimited ? state.originalResources.slice(0, DIFF_LIMIT) : state.originalResources;
    return JSON.stringify(subset, null, 2);
  }, [state.originalResources, activeTab, isDiffLimited]);

  const hasResults = !state.isStreaming && state.resources.length > 0;

  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<{ uploaded: number; errors: number } | null>(null);

  const handleUploadToTarget = useCallback(async () => {
    if (uploading || cleanResources.length === 0) return;
    setUploading(true);
    setUploadResult(null);
    try {
      const result = await uploadToTarget(cleanResources);
      setUploadResult({ uploaded: result.uploaded, errors: result.errors });
    } catch {
      setUploadResult({ uploaded: 0, errors: -1 });
    } finally {
      setUploading(false);
    }
  }, [uploading, cleanResources]);

  const piiDetectionMap = useMemo(
    () => (hasResults ? buildPiiFromDeidentifiedOnly(state.resources) : {}),
    [hasResults, state.resources],
  );

  const fieldSummary = useMemo(() => {
    if (!hasResults) return undefined;
    const allFieldCounts: Record<string, Record<string, number>> = {};
    for (const resource of state.resources) {
      const type = String(resource.resourceType ?? "Unknown");
      if (!allFieldCounts[type]) allFieldCounts[type] = {};
      for (const { field } of extractFieldsDeep(resource)) {
        allFieldCounts[type][field] = (allFieldCounts[type][field] ?? 0) + 1;
      }
    }
    return buildFieldSummary(allFieldCounts, piiDetectionMap);
  }, [hasResults, state.resources, piiDetectionMap]);

  return (
    <div className="flex flex-col gap-4">
      {/* Header row */}
      <div className="flex items-center justify-between rounded-xl border bg-muted/30 px-4 py-3">
        <div className="flex flex-col gap-0.5">
          <span className="text-sm font-semibold">{patientName}</span>
          <span className="font-mono text-[11px] text-muted-foreground">
            {patientId}
          </span>
        </div>
        <div className="flex items-center gap-2">
          {state.score && !state.isStreaming && (
            <ScoreBadge score={state.score} />
          )}
          {state.isStreaming ? (
            <Button variant="destructive" size="sm" onClick={handleAbort}>
              <Square className="h-3.5 w-3.5" />
              Stop
            </Button>
          ) : (
            <Button size="sm" onClick={handleRun}>
              <Play className="h-3.5 w-3.5" />
              Run $everything
            </Button>
          )}
        </div>
      </div>

      {/* Progress */}
      {(state.isStreaming || state.resources.length > 0) && (
        <StreamProgress
          count={state.resources.length}
          isStreaming={state.isStreaming}
          errorCount={state.errorCount}
        />
      )}

      {/* Error */}
      {state.error && (
        <div className="flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3">
          <AlertCircle className="mt-0.5 size-4 shrink-0 text-destructive" />
          <div>
            <p className="text-sm font-medium text-destructive">Error</p>
            <p className="mt-0.5 text-sm text-destructive/80">{state.error}</p>
          </div>
        </div>
      )}

      {/* Resource breakdown */}
      {Object.keys(state.resourceCounts).length > 0 && (
        <ResourceTypeSummary counts={state.resourceCounts} piiData={piiDetectionMap} fieldSummary={fieldSummary} />
      )}

      {/* Results */}
      {hasResults && (
        <>
          {/* Tab bar */}
          <div className="flex items-center justify-between gap-2">
            <div className="flex gap-1 rounded-lg border bg-muted/30 p-1">
              <button
                onClick={() => setActiveTab("output")}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  activeTab === "output"
                    ? "bg-background shadow-sm text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <FileJson className="h-3.5 w-3.5" />
                Output
              </button>
              <button
                onClick={() => setActiveTab("diff")}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  activeTab === "diff"
                    ? "bg-background shadow-sm text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <GitCompare className="h-3.5 w-3.5" />
                Compare
              </button>
              <button
                onClick={() => setActiveTab("table")}
                className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  activeTab === "table"
                    ? "bg-background shadow-sm text-foreground"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                <TableProperties className="h-3.5 w-3.5" />
                Table
              </button>
            </div>

            {/* Full view toggle */}
            <Button
              variant="outline"
              size="sm"
              onClick={() => setFullView(!fullView)}
              className="shrink-0"
            >
              {fullView ? (
                <>
                  <Minimize2 className="h-3.5 w-3.5 mr-1.5" />
                  Compact View
                </>
              ) : (
                <>
                  <Maximize2 className="h-3.5 w-3.5 mr-1.5" />
                  Full View
                </>
              )}
            </Button>
          </div>

          {previewTruncated > 0 && (
            <p className="rounded-md border bg-amber-50 px-3 py-2 text-xs text-amber-800">
              Preview limited to the first {PREVIEW_RESOURCE_LIMIT} of {previewTruncated} resources.
              Use &quot;Deliver to S3&quot; to de-identify and release the complete record as an export job.
            </p>
          )}

          {/* Output tab */}
          {activeTab === "output" && (
            <>
              <Collapsible>
                <CollapsibleTrigger className="flex w-full items-center justify-start gap-2 rounded-md border bg-background px-3 py-1.5 text-sm font-medium hover:bg-accent">
                  View FHIR Output ({state.resources.length} resources)
                </CollapsibleTrigger>
                <CollapsibleContent>
                  <div className="mt-2">
                    <FhirCodeViewer
                      code={jsonOutput}
                      language="json"
                      maxHeight={fullView ? "none" : "400px"}
                    />
                  </div>
                </CollapsibleContent>
              </Collapsible>
              <MultiFormatDownload
                resources={cleanResources}
                baseFilename={downloadBase}
                defaultFormat="ndjson"
                onXmlDownload={handleXmlDownload}
              />
              {destinations.length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-dashed p-3">
                  <span className="text-xs text-muted-foreground">
                    Send de-identified data + audit to S3:
                  </span>
                  <select
                    className="h-8 rounded-md border bg-background px-2 text-xs"
                    value={selectedDest}
                    onChange={(e) => setSelectedDest(e.target.value)}
                  >
                    {destinations.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name} ({d.bucket})
                      </option>
                    ))}
                  </select>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={handleDeliverToS3}
                    disabled={delivering || cleanResources.length === 0}
                  >
                    {delivering ? (
                      <Loader2 className="size-3.5 animate-spin" />
                    ) : (
                      <Upload className="size-3.5" />
                    )}
                    Send to S3
                  </Button>
                </div>
              )}
              <div className="flex flex-col gap-2">
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={handleUploadToTarget}
                    disabled={uploading}
                  >
                    {uploading ? (
                      <Loader2 className="size-3.5 animate-spin" />
                    ) : (
                      <Upload className="size-3.5" />
                    )}
                    {uploading ? "Sending…" : "Send to Target"}
                  </Button>
                </div>
                {uploadResult && uploadResult.errors !== -1 && (
                  <span className="flex items-center gap-1.5 text-xs text-green-700">
                    <CheckCircle2 className="size-3.5" />
                    {uploadResult.uploaded} uploaded
                    {uploadResult.errors > 0 && (
                      <span className="text-destructive">({uploadResult.errors} error{uploadResult.errors !== 1 ? "s" : ""})</span>
                    )}
                  </span>
                )}
                {uploadResult && uploadResult.errors === -1 && (
                  <span className="text-xs text-destructive">Upload failed, check that the target FHIR server URL is correct or that FHIR_TARGET_URL is set</span>
                )}
              </div>
            </>
          )}

          {/* Diff tab */}
          {activeTab === "diff" && (
            <>
              {isDiffLimited && (
                <p className="rounded-md border bg-amber-50 px-3 py-2 text-xs text-amber-800">
                  Showing diff for the first {DIFF_LIMIT} of {resourceCount} resources. Download the full result for a complete view.
                </p>
              )}
              <JsonDiffViewer
                original={jsonOriginal}
                modified={jsonOutput}
                maxHeight={fullView ? "none" : "520px"}
                context={fullView ? 20 : 4}
                disableGapCompression={fullView}
                fullHeight={fullView}
              />
            </>
          )}

          {/* Table tab */}
          {activeTab === "table" && (
            <div
              className="overflow-auto"
              style={{ maxHeight: fullView ? "none" : "520px" }}
            >
              <FhirTableView
                originalResources={state.originalResources}
                resources={state.resources}
                piiData={piiDetectionMap}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
