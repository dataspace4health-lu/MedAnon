import { useState, useCallback, useRef, useMemo } from "react";
import { Play, Square, AlertCircle, GitCompare, FileJson, TableProperties, Maximize2, Minimize2 } from "lucide-react";
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
import { getAuthHeaders } from "@/api/client";

interface DeidentifyPanelProps {
  patientId: string;
  patientName: string;
  configProfile: string;
}

interface StreamState {
  isStreaming: boolean;
  resources: Record<string, unknown>[];
  originalResources: Record<string, unknown>[];
  resourceCounts: Record<string, number>;
  errorCount: number;
  error: string | null;
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
  });
  const [activeTab, setActiveTab] = useState<"output" | "diff" | "table">("output");
  const [fullView, setFullView] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

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
    });
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

      // Extract original resources for the diff view.
      const originalResources: Record<string, unknown>[] = (
        (bundle.entry as Array<{ resource?: Record<string, unknown> }>) ?? []
      )
        .map((e) => e.resource)
        .filter((r): r is Record<string, unknown> => !!r);

      setState((prev) => ({ ...prev, originalResources }));

      // Step 2: Stream the bundle through /process/batch for de-identification.
      const params = new URLSearchParams({ config_profile: configProfile });
      const response = await fetch(`/api/v1/process/batch?${params}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getAuthHeaders(),
        },
        body: JSON.stringify(bundle),
        signal,
      });

      if (!response.ok) {
        let detail = `Processing failed (${response.status})`;
        try {
          const body = await response.json();
          if (body?.detail) detail = String(body.detail);
        } catch {
          // ignore
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
            const resource =
              "data" in parsed
                ? (parsed.data as Record<string, unknown>)
                : parsed;

            if ("error" in parsed && !("data" in parsed)) {
              errors++;
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

        setState((prev) => ({
          ...prev,
          resources: [...collectedResources],
          resourceCounts: { ...counts },
          errorCount: errors,
        }));
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

  const handleXmlDownload = useCallback(async () => {
    try {
      const response = await fetch(`/api/v1/process/batch?config_profile=${configProfile}&output_format=xml`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getAuthHeaders(),
        },
        body: JSON.stringify({
          resourceType: "Bundle",
          type: "collection",
          entry: state.originalResources.map(resource => ({ resource })),
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
      anchor.download = `deidentified-${patientId}.xml`;
      document.body.appendChild(anchor);
      anchor.click();

      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Failed to download XML:", error);
      alert("Failed to generate XML download. Please try again or use a different format.");
    }
  }, [patientId, configProfile, state.originalResources]);

  const jsonOutput = useMemo(() => {
    return JSON.stringify(state.resources, null, 2);
  }, [state.resources]);

  const jsonOriginal = useMemo(() => {
    return JSON.stringify(state.originalResources, null, 2);
  }, [state.originalResources]);

  const hasResults = !state.isStreaming && state.resources.length > 0;

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
        <ResourceTypeSummary counts={state.resourceCounts} />
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
                resources={state.resources}
                baseFilename={`deidentified-${patientId}`}
                defaultFormat="ndjson"
                onXmlDownload={handleXmlDownload}
              />
            </>
          )}

          {/* Diff tab */}
          {activeTab === "diff" && (
            <JsonDiffViewer
              original={jsonOriginal}
              modified={jsonOutput}
              maxHeight={fullView ? "none" : "520px"}
              context={fullView ? 20 : 4}
              disableGapCompression={fullView}
              fullHeight={fullView}
            />
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
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
