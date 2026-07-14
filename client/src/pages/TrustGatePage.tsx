import { useState, useEffect, useCallback, useRef } from "react";
import { Link, useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  Loader2,
  Play,
  FileCode,
  ChevronDown,
  Info,
  ServerCog,
  ClipboardPaste,
  CircleDot,
  Database,
  RefreshCw,
  Ban,
  Settings as SettingsIcon,
  Boxes,
  Activity,
  Cable,
} from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { assess, assessBatch, assessOmop, trustGateHealthy, listUseCases } from "@/api/trustGate";
import type { QualityPassport, AssessOptions } from "@/api/trustGate";
import { QcPipeline } from "./trust-gate/QcPipeline";
import { listTypeCounts, scanByPatient, fetchSample, fetchEverythingVia } from "@/api/fhirScan";
import type { TypeCount, ScanProgress } from "@/api/fhirScan";
import { ApiError } from "@/api/types";
import { useSettings } from "@/context/SettingsContext";
import { listTrustProfiles } from "@/api/trustProfiles";
import type { TrustProfileMeta } from "@/api/trustProfiles";
import { TrustGateResultPanel } from "./trust-gate/TrustGateResultPanel";
import { ConnectorPanel } from "./trust-gate/ConnectorPanel";
import {
  SAMPLE_CLEAN,
  SAMPLE_BLOCKED,
  SAMPLE_OMOP_CLEAN,
  SAMPLE_OMOP_TABULAR,
  nowInstant,
} from "./trust-gate/trustGateHelpers";

type ServerMode = "scan" | "mixed" | "everything";

export default function TrustGatePage() {
  // -- service health -------------------------------------------------------
  const [online, setOnline] = useState<boolean | null>(null);
  useEffect(() => {
    trustGateHealthy().then(setOnline);
  }, []);

  // -- connections (shared registry from Settings) --------------------------
  const { connections, activeConnectionId, activeConnection, setActiveConnection, preferences } =
    useSettings();
  const conn = activeConnection;
  const navigate = useNavigate();

  // -- shared dataset (provenance is auto-derived from the connection) -------
  const [datasetId, setDatasetId] = useState(preferences.datasetId);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  // -- server-pull state ----------------------------------------------------
  const [serverMode, setServerMode] = useState<ServerMode>("scan");
  const [typeCounts, setTypeCounts] = useState<TypeCount[]>([]);
  const [loadingTypes, setLoadingTypes] = useState(false);
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [perType, setPerType] = useState(10);
  const [patientId, setPatientId] = useState("");
  const [progress, setProgress] = useState<ScanProgress | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // -- paste state ----------------------------------------------------------
  const [pasteText, setPasteText] = useState("");

  // -- OMOP / tabular state -------------------------------------------------
  const [omopText, setOmopText] = useState("");

  // -- result / misc --------------------------------------------------------
  const [passport, setPassport] = useState<QualityPassport | null>(null);
  const [busy, setBusy] = useState(false);
  const [howOpen, setHowOpen] = useState(false);
  const [lastSource, setLastSource] = useState<"fhir" | "omop">("fhir");

  // -- platform attribution (provider, use case, lifecycle, org role) -------
  const [providerId, setProviderId] = useState("provider-001");
  const [useCase, setUseCase] = useState("");
  const [lifecycleStage, setLifecycleStage] = useState("operation");
  const [orgRole, setOrgRole] = useState("data-receiving");
  const [useCases, setUseCases] = useState<Array<{ id: string; description?: string }>>([]);
  useEffect(() => {
    listUseCases().then(setUseCases);
  }, []);

  // -- trust profile selection (tunes which phases run + intended use) ------
  const [profiles, setProfiles] = useState<TrustProfileMeta[]>([]);
  const [selectedProfile, setSelectedProfile] = useState("");

  useEffect(() => {
    listTrustProfiles().then(setProfiles).catch(() => setProfiles([]));
  }, []);

  /** Tuning options derived from the selected profile (empty → all phases) plus
   * the run-level platform attribution (provider, use case, lifecycle, org). */
  const tuneOpts = useCallback((): AssessOptions => {
    const attribution: AssessOptions = {
      providerId: providerId.trim() || undefined,
      useCase: useCase || undefined,
      lifecycleStage,
      orgRole,
    };
    const p = profiles.find((x) => x.name === selectedProfile);
    if (!p) return attribution;
    return {
      ...attribution,
      phases: p.phases,
      intendedUse: p.intended_use || undefined,
      targets: p.targets.length ? (p.targets as Array<Record<string, unknown>>) : undefined,
    };
  }, [profiles, selectedProfile, providerId, useCase, lifecycleStage, orgRole]);

  // Provenance is derived automatically: the source system from the selected
  // connection, the extraction time from "now", no manual fields to fill.
  const provenance = useCallback(
    () => {
      const src = conn?.label || conn?.baseUrl || "";
      return {
        ...(src ? { source_system: src } : {}),
        extraction_time: nowInstant(),
      };
    },
    [conn],
  );

  const refreshTypes = useCallback(async () => {
    setLoadingTypes(true);
    setTypeCounts([]);
    try {
      const counts = await listTypeCounts(conn);
      setTypeCounts(counts);
      const preferred = ["Patient", "Observation", "Condition", "Encounter"];
      setSelectedTypes(counts.map((c) => c.type).filter((t) => preferred.includes(t)));
    } catch (err) {
      const msg = err instanceof ApiError ? `${err.status}: ${err.message}` : String(err);
      toast.error(`Could not read ${conn.label}`, { description: msg });
    } finally {
      setLoadingTypes(false);
    }
  }, [conn]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    refreshTypes();
  }, [activeConnectionId]);

  const runAssess = useCallback(
    async (fn: () => Promise<QualityPassport>, what: string, sourceModel: "fhir" | "omop" = "fhir") => {
      setBusy(true);
      setPassport(null);
      setLastSource(sourceModel);
      try {
        const result = await fn();
        setPassport(result);
        toast.success(`Quality Passport: ${result.decision.replace("_", " ")}`, {
          description: `${what}, ${result.overall_score != null ? Math.round(result.overall_score) : "not assessed"}% checks passing`,
        });
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") {
          toast.info("Scan cancelled.");
          return;
        }
        const msg =
          err instanceof ApiError
            ? `${err.status}: ${err.message}`
            : err instanceof Error
              ? err.message
              : String(err);
        toast.error("Assessment failed", { description: msg });
      } finally {
        setBusy(false);
        setProgress(null);
        abortRef.current = null;
      }
    },
    [],
  );

  const toggleType = (t: string) =>
    setSelectedTypes((cur) => (cur.includes(t) ? cur.filter((x) => x !== t) : [...cur, t]));

  // -- handlers -------------------------------------------------------------

  const handleScan = () =>
    runAssess(async () => {
      // Patient-compartment scan: ingest the WHOLE server (no cap), assessing each
      // patient's complete record together so relational/temporal scores are real.
      if (!typeCounts.length) throw new Error("No resource types found. Reload types from the connection.");
      const controller = new AbortController();
      abortRef.current = controller;
      setProgress({ type: "", fetched: 0, target: 0, chunksAssessed: 0 });
      const tuned = tuneOpts();
      return scanByPatient(conn, typeCounts, {
        datasetId,
        provenance: provenance(),
        pageSize: preferences.fhirPageSize,
        phases: tuned.phases,
        intendedUse: tuned.intendedUse,
        onProgress: setProgress,
        signal: controller.signal,
      });
    }, `patient-compartment scan of ${conn.label}`);

  const handlePullMixed = () =>
    runAssess(async () => {
      if (!selectedTypes.length) throw new Error("Select at least one resource type.");
      const resources = await fetchSample(conn, selectedTypes, perType);
      if (!resources.length) throw new Error("The server returned no resources.");
      return assessBatch(resources, { datasetId, provenance: provenance(), ...tuneOpts() });
    }, `${perType}× ${selectedTypes.join(", ")} from ${conn.label}`);

  const handlePullEverything = () =>
    runAssess(async () => {
      const id = patientId.trim();
      if (!id) throw new Error("Enter a Patient id.");
      const resources = await fetchEverythingVia(conn, id);
      if (!resources.length) throw new Error(`No resources in Patient/${id}/$everything.`);
      return assessBatch(resources, {
        datasetId: `patient-${id}-everything`,
        provenance: provenance(),
        ...tuneOpts(),
      });
    }, `Patient/${patientId.trim()}/$everything`);

  const handleAssessPaste = () =>
    runAssess(async () => {
      const trimmed = pasteText.trim();
      if (!trimmed) throw new Error("Paste a FHIR resource, list, or Bundle first.");
      let parsed: unknown;
      try {
        parsed = JSON.parse(trimmed);
      } catch (e) {
        throw new Error(`Invalid JSON: ${(e as Error).message}`);
      }
      return assess(parsed as Record<string, unknown>, {
        datasetId,
        provenance: provenance(),
        ...tuneOpts(),
      });
    }, "pasted FHIR");

  const handleAssessOmop = () =>
    runAssess(async () => {
      const trimmed = omopText.trim();
      if (!trimmed) throw new Error("Paste OMOP tables (or source tables + mapping) first.");
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(trimmed);
      } catch (e) {
        throw new Error(`Invalid JSON: ${(e as Error).message}`);
      }
      const input: Parameters<typeof assessOmop>[0] = {};
      if (parsed.tables) input.tables = parsed.tables as Record<string, Array<Record<string, unknown>>>;
      if (parsed.mapping) input.mapping = parsed.mapping as Record<string, Record<string, string>>;
      if (parsed.resources) input.resources = parsed.resources as Array<Record<string, unknown>>;
      if (!input.tables && !input.resources) input.tables = parsed as Record<string, Array<Record<string, unknown>>>;
      return assessOmop(input, { datasetId, provenance: provenance(), sourceTypes: ["omop"], ...tuneOpts() });
    }, "OMOP / tabular submission", "omop");

  const cancelScan = () => abortRef.current?.abort();

  // -- render ---------------------------------------------------------------

  const pct =
    progress && progress.patientsTotal
      ? Math.min(100, Math.round(((progress.patientsDone ?? 0) / progress.patientsTotal) * 100))
      : progress && progress.target > 0
        ? Math.min(100, Math.round((progress.fetched / progress.target) * 100))
        : 0;

  return (
    <div>
      <PageHeader
        title="Trust Gate"
        description="Assess FHIR data quality before privacy processing. Connect to a FHIR server (or paste data) and get a Quality Passport (PASS / CONDITIONAL / BLOCK) grounded in the Kahn 2016 framework and OHDSI Data Quality Dashboard scoring."
        actions={
          <Badge variant="outline" className="gap-1.5">
            <CircleDot
              className={`size-3 ${
                online === null ? "text-muted-foreground" : online ? "text-emerald-500" : "text-destructive"
              }`}
            />
            {online === null ? "checking…" : online ? "service online" : "service offline"}
          </Badge>
        }
      />

      {/* How it works -------------------------------------------------------- */}
      <Collapsible open={howOpen} onOpenChange={setHowOpen} className="mb-6">
        <Card>
          <CardHeader>
            <CollapsibleTrigger className="flex w-full items-center justify-between">
              <CardTitle className="flex items-center gap-2">
                <Info className="size-4" /> How this works
              </CardTitle>
              <ChevronDown className={`size-4 text-muted-foreground transition-transform ${howOpen ? "rotate-180" : ""}`} />
            </CollapsibleTrigger>
          </CardHeader>
          <CollapsibleContent>
            <CardContent className="flex flex-col gap-4 text-sm text-muted-foreground">
              <p>
                The Trust Gate gates what <strong>enters</strong> the privacy engine. It scores
                conformance, completeness, and plausibility checks OHDSI-DQD style (violation
                fraction vs a per-check threshold); category and overall scores are the{" "}
                <strong>% of applicable checks passing</strong>.
              </p>
              <div>
                <p className="font-medium text-foreground">Ingestion modes</p>
                <ul className="mt-1 list-inside list-disc space-y-1">
                  <li><strong>Full-server scan</strong>, paginate ALL selected types, assess in chunks, aggregate one server-wide passport.</li>
                  <li><strong>Patient $everything</strong>, a self-contained compartment where references resolve (cleanest relational signal).</li>
                  <li><strong>Mixed sample / Paste</strong>, quick spot checks.</li>
                </ul>
              </div>
            </CardContent>
          </CollapsibleContent>
        </Card>
      </Collapsible>

      {/* Connection ---------------------------------------------------------- */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="size-4" /> FHIR connection
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-64">
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Connection</label>
              <Select value={activeConnectionId} onValueChange={(v) => setActiveConnection(v ?? "source")}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {connections.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.label}
                      {c.baseUrl ? `, ${c.baseUrl}` : ""}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <Button variant="outline" onClick={refreshTypes} disabled={loadingTypes}>
              <RefreshCw className={`size-4 ${loadingTypes ? "animate-spin" : ""}`} />
              Reload types
            </Button>
            <Button variant="ghost" onClick={() => navigate("/settings")}>
              <SettingsIcon className="size-4" /> Manage servers
            </Button>
          </div>
          {typeCounts.length > 0 && (
            <p className="text-xs text-muted-foreground">
              {conn.label}: {typeCounts.reduce((s, t) => s + t.count, 0).toLocaleString()} resources
              across {typeCounts.length} types.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Dataset & options --------------------------------------------------- */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Dataset</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Dataset ID</label>
              <Input value={datasetId} onChange={(e) => setDatasetId(e.target.value)} />
              <p className="mt-1 text-xs text-muted-foreground">
                Identifies this dataset across runs (history &amp; audit trail).
              </p>
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                Intended use (selects the checks that matter)
              </label>
              <Select value={useCase || "__all__"} onValueChange={(v) => setUseCase(!v || v === "__all__" ? "" : v)}>
                <SelectTrigger className="w-full">
                  <SelectValue placeholder="All checks (default)" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="__all__">All checks (default)</SelectItem>
                  {useCases.map((u) => (
                    <SelectItem key={u.id} value={u.id}>
                      {u.id.replace(/_/g, " ")}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
            <CollapsibleTrigger className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
              <ChevronDown className={`size-4 transition-transform ${advancedOpen ? "rotate-180" : ""}`} />
              Advanced, provider, reporting context &amp; profile
            </CollapsibleTrigger>
            <CollapsibleContent className="grid gap-4 pt-4 sm:grid-cols-2">
              <div>
                <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Provider ID</label>
                <Input value={providerId} onChange={(e) => setProviderId(e.target.value)} placeholder="provider-001" />
              </div>
              <div>
                <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Trust profile (tunes phases)</label>
                <Select value={selectedProfile || "__all__"} onValueChange={(v) => setSelectedProfile(!v || v === "__all__" ? "" : v)}>
                  <SelectTrigger className="w-full"><SelectValue placeholder="All phases (default)" /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__all__">All phases (default)</SelectItem>
                    {profiles.map((p) => (
                      <SelectItem key={p.name} value={p.name}>
                        {p.name}{p.intended_use ? `, ${p.intended_use}` : ""}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div>
                <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Lifecycle stage (reporting)</label>
                <Select value={lifecycleStage} onValueChange={(v) => setLifecycleStage(v ?? "operation")}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="planning">Planning</SelectItem>
                    <SelectItem value="construction">Construction</SelectItem>
                    <SelectItem value="operation">Operation</SelectItem>
                    <SelectItem value="utilization">Utilization</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div>
                <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Org role (reporting)</label>
                <Select value={orgRole} onValueChange={(v) => setOrgRole(v ?? "data-receiving")}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="data-generating">Data generating</SelectItem>
                    <SelectItem value="data-receiving">Data receiving</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <p className="text-xs text-muted-foreground sm:col-span-2">
                Source system &amp; extraction time are recorded automatically from the selected connection.
                Lifecycle stage and org role only label the audit report (Wassell reporting). Manage profiles under
                Configure → Trust Profiles.
              </p>
            </CollapsibleContent>
          </Collapsible>
        </CardContent>
      </Card>

      {/* Input --------------------------------------------------------------- */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Input</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="server">
            <TabsList>
              <TabsTrigger value="server">
                <ServerCog className="size-4" /> Pull from server
              </TabsTrigger>
              <TabsTrigger value="paste">
                <ClipboardPaste className="size-4" /> Paste JSON
              </TabsTrigger>
              <TabsTrigger value="omop">
                <Boxes className="size-4" /> OMOP / tabular
              </TabsTrigger>
              <TabsTrigger value="connectors">
                <Cable className="size-4" /> Connectors
              </TabsTrigger>
            </TabsList>

            {/* -- Server pull -- */}
            <TabsContent value="server">
              <div className="flex flex-col gap-4">
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Ingestion mode</label>
                  <Select value={serverMode} onValueChange={(v) => setServerMode((v as ServerMode) ?? "scan")}>
                    <SelectTrigger className="w-64">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="scan">Full-server scan (all data)</SelectItem>
                      <SelectItem value="mixed">Mixed sample (by type)</SelectItem>
                      <SelectItem value="everything">Patient $everything</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {serverMode === "mixed" && (
                  <div>
                    <label className="mb-2 block text-xs font-medium text-muted-foreground">
                      Resource types {loadingTypes && "(loading…)"}
                    </label>
                    {typeCounts.length === 0 && !loadingTypes ? (
                      <p className="text-sm text-muted-foreground">No types loaded. Use “Reload types” above.</p>
                    ) : (
                      <div className="flex flex-wrap gap-2">
                        {typeCounts.map(({ type, count }) => {
                          const on = selectedTypes.includes(type);
                          return (
                            <button
                              key={type}
                              type="button"
                              onClick={() => toggleType(type)}
                              className={[
                                "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
                                on
                                  ? "border-primary bg-primary text-primary-foreground"
                                  : "border-border text-muted-foreground hover:bg-muted",
                              ].join(" ")}
                            >
                              {type} <span className={on ? "opacity-80" : "opacity-60"}>({count.toLocaleString()})</span>
                            </button>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}

                {serverMode === "scan" && (
                  <div className="flex flex-col gap-2">
                    <p className="text-xs text-muted-foreground">
                      Ingests the <strong>whole</strong> {conn.label} server (no cap) and assesses each patient&apos;s
                      complete record together, so referential integrity, the patient timeline, and clinical-logic
                      checks are scored against real context. Non-patient resources are swept after.
                    </p>
                    <div>
                      {busy ? (
                        <Button variant="destructive" onClick={cancelScan}>
                          <Ban className="size-4" /> Cancel
                        </Button>
                      ) : (
                        <Button onClick={handleScan} disabled={online === false || !typeCounts.length}>
                          <Play className="size-4" /> Scan &amp; assess
                        </Button>
                      )}
                    </div>
                  </div>
                )}

                {serverMode === "mixed" && (
                  <div className="flex items-end gap-3">
                    <div className="w-40">
                      <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Per type</label>
                      <Input
                        type="number"
                        min={1}
                        max={1000}
                        value={perType}
                        onChange={(e) => setPerType(Math.max(1, Number(e.target.value) || 1))}
                      />
                    </div>
                    <Button onClick={handlePullMixed} disabled={busy || online === false}>
                      {busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                      Fetch &amp; assess
                    </Button>
                  </div>
                )}

                {serverMode === "everything" && (
                  <div className="flex items-end gap-3">
                    <div className="max-w-xs flex-1">
                      <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Patient id</label>
                      <Input placeholder="e.g. 12345" value={patientId} onChange={(e) => setPatientId(e.target.value)} />
                    </div>
                    <Button onClick={handlePullEverything} disabled={busy || online === false}>
                      {busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                      Fetch &amp; assess
                    </Button>
                  </div>
                )}

                {progress && (
                  <div className="rounded-lg border bg-muted/30 p-4">
                    <div className="mb-2 flex items-center justify-between text-sm">
                      <span className="text-muted-foreground">
                        {progress.patientsTotal
                          ? `Assessing patient records${progress.type === "Patient" ? "" : ` · ${progress.type || "finalizing"}`}…`
                          : progress.type ? `Fetching ${progress.type}…` : "Assessing…"}
                      </span>
                      <span className="tabular-nums">
                        {progress.patientsTotal
                          ? `${(progress.patientsDone ?? 0).toLocaleString()} / ${progress.patientsTotal.toLocaleString()} patients · `
                          : ""}
                        {progress.fetched.toLocaleString()}
                        {progress.target ? ` / ${progress.target.toLocaleString()}` : ""} resources
                      </span>
                    </div>
                    <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                      {pct > 0 ? (
                        <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${pct}%` }} />
                      ) : (
                        <div className="h-full w-1/3 animate-[shimmer_1.2s_ease-in-out_infinite] rounded-full bg-primary/60" />
                      )}
                    </div>
                  </div>
                )}

                <p className="text-xs text-muted-foreground">
                  Data is fetched from <strong>{conn.label}</strong>
                  {conn.baseUrl ? ` (${conn.baseUrl})` : ""}. Add or edit servers on the{" "}
                  <Link to="/settings" className="underline">Settings</Link> page.
                </p>
              </div>
            </TabsContent>

            {/* -- Paste -- */}
            <TabsContent value="paste">
              <div className="flex flex-col gap-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" onClick={() => setPasteText(SAMPLE_CLEAN)}>
                    <FileCode className="size-4" /> Load clean sample
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => setPasteText(SAMPLE_BLOCKED)}>
                    <FileCode className="size-4" /> Load blocked sample
                  </Button>
                </div>
                <Textarea
                  className="h-[280px] resize-none font-mono text-xs"
                  placeholder='{ "resourceType": "Patient", "id": "p1", ... } , single resource, list, or Bundle'
                  value={pasteText}
                  onChange={(e) => setPasteText(e.target.value)}
                  spellCheck={false}
                />
                <div>
                  <Button onClick={handleAssessPaste} disabled={busy || online === false || !pasteText.trim()}>
                    {busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                    Assess quality
                  </Button>
                </div>
              </div>
            </TabsContent>

            {/* -- OMOP / tabular -- */}
            <TabsContent value="omop">
              <div className="flex flex-col gap-4">
                <p className="text-xs text-muted-foreground">
                  Provider data arrives in many shapes. Submit <strong>OMOP CDM</strong> tables directly, or
                  source/tabular tables with a column <strong>mapping</strong>, both are normalized onto OMOP and
                  assessed with OHDSI DQD-style checks plus demographically-stratified clinical evaluation.
                </p>
                <div className="flex flex-wrap items-center gap-2">
                  <Button variant="outline" size="sm" onClick={() => setOmopText(SAMPLE_OMOP_CLEAN)}>
                    <FileCode className="size-4" /> OMOP sample
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => setOmopText(SAMPLE_OMOP_TABULAR)}>
                    <FileCode className="size-4" /> Tabular + mapping sample
                  </Button>
                </div>
                <Textarea
                  className="h-[280px] resize-none font-mono text-xs"
                  placeholder='{ "tables": { "person": [...], "measurement": [...] }, "mapping": { ... } }'
                  value={omopText}
                  onChange={(e) => setOmopText(e.target.value)}
                  spellCheck={false}
                />
                <div>
                  <Button onClick={handleAssessOmop} disabled={busy || online === false || !omopText.trim()}>
                    {busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                    Assess quality
                  </Button>
                </div>
              </div>
            </TabsContent>

            {/* -- Connectors (file upload + SQL) -- */}
            <TabsContent value="connectors">
              <ConnectorPanel
                datasetId={datasetId}
                busy={busy}
                onAssess={runAssess}
              />
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      {/* QC pipeline (structured, sequential) -------------------------------- */}
      {(busy || passport) && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Activity className="size-4" /> QC pipeline
            </CardTitle>
            <p className="text-sm text-muted-foreground">
              Each stage maps to the checks that actually ran, revealed in sequence as the assessment completes.
            </p>
          </CardHeader>
          <CardContent>
            <QcPipeline running={busy} passport={passport} sourceModel={lastSource} />
          </CardContent>
        </Card>
      )}

      {/* Result -------------------------------------------------------------- */}
      {passport ? (
        <TrustGateResultPanel passport={passport} />
      ) : (
        !busy && (
          <Card>
            <CardContent className="py-16 text-center text-sm text-muted-foreground">
              Pick a connection and scan the server, paste a resource, or submit OMOP/tabular data to generate a
              Quality Passport.
            </CardContent>
          </Card>
        )
      )}
    </div>
  );
}
