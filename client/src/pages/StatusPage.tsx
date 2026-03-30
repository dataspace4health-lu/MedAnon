import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { health, ready } from '@/api/medanon';
import { capabilityStatement, patientCount } from '@/api/fhir';
import type { HealthResponse, ReadyResponse } from '@/api/types';
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/table';
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from '@/components/ui/collapsible';
import { HealthBadge } from '@/components/shared/HealthBadge';
import { MetricCard } from '@/components/shared/MetricCard';
import { FhirCodeViewer } from '@/components/shared/FhirCodeViewer';
import { RefreshCw, ChevronDown } from 'lucide-react';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const AUTO_REFRESH_INTERVAL_MS = 30_000;

// ---------------------------------------------------------------------------
// Types for fetched data
// ---------------------------------------------------------------------------

interface CapabilityData {
  fhirVersion: string;
  softwareName: string;
  softwareVersion: string;
  raw: Record<string, unknown>;
}

interface ServiceState {
  loading: boolean;
  medanon: { ok: boolean; data: HealthResponse | null; error: string | null };
  fhir: { ok: boolean; data: CapabilityData | null; error: string | null };
  gpas: { ok: boolean; status: string | null; error: string | null };
  ready: { ok: boolean; data: ReadyResponse | null; error: string | null };
  patientCount: { count: number | null; error: string | null };
}

const initialState: ServiceState = {
  loading: true,
  medanon: { ok: false, data: null, error: null },
  fhir: { ok: false, data: null, error: null },
  gpas: { ok: false, status: null, error: null },
  ready: { ok: false, data: null, error: null },
  patientCount: { count: null, error: null },
};

// ---------------------------------------------------------------------------
// Helper: extract CapabilityData from raw response
// ---------------------------------------------------------------------------

function parseCapability(raw: Record<string, unknown>): CapabilityData {
  const software = raw.software as Record<string, unknown> | undefined;
  return {
    fhirVersion: String(raw.fhirVersion ?? 'unknown'),
    softwareName: String(software?.name ?? 'unknown'),
    softwareVersion: String(software?.version ?? 'unknown'),
    raw,
  };
}

// ---------------------------------------------------------------------------
// Helper: format a Date as a short time string
// ---------------------------------------------------------------------------

function formatTime(date: Date): string {
  return date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

// ---------------------------------------------------------------------------
// StatusPage
// ---------------------------------------------------------------------------

export default function StatusPage() {
  const [state, setState] = useState<ServiceState>(initialState);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // -----------------------------------------------------------------------
  // Refresh logic: fetch all endpoints in parallel
  // -----------------------------------------------------------------------

  const refresh = useCallback(async () => {
    setRefreshing(true);

    const [healthResult, readyResult, capResult, countResult] =
      await Promise.allSettled([
        health(),
        ready(),
        capabilityStatement(),
        patientCount(),
      ]);

    setState((prev) => {
      const next: ServiceState = { ...prev, loading: false };

      // MedAnon health
      if (healthResult.status === 'fulfilled') {
        const h = healthResult.value;
        next.medanon = {
          ok: h.status === 'ok' || h.status === 'healthy',
          data: h,
          error: null,
        };
      } else {
        next.medanon = {
          ok: false,
          data: null,
          error: String(healthResult.reason),
        };
      }

      // MedAnon readiness
      if (readyResult.status === 'fulfilled') {
        const r = readyResult.value;
        next.ready = { ok: r.ready, data: r, error: null };

        // Extract gPAS status from readiness checks
        const gpasCheck = r.checks?.gpas ?? r.checks?.gPAS ?? null;
        if (gpasCheck !== null && gpasCheck !== undefined) {
          const gpasOk =
            gpasCheck === 'ok' ||
            gpasCheck === 'healthy' ||
            gpasCheck === 'available';
          next.gpas = { ok: gpasOk, status: String(gpasCheck), error: null };
        } else {
          next.gpas = {
            ok: false,
            status: 'not configured',
            error: null,
          };
        }
      } else {
        next.ready = {
          ok: false,
          data: null,
          error: String(readyResult.reason),
        };
        next.gpas = {
          ok: false,
          status: null,
          error: String(readyResult.reason),
        };
      }

      // HAPI FHIR capability
      if (capResult.status === 'fulfilled') {
        next.fhir = {
          ok: true,
          data: parseCapability(capResult.value),
          error: null,
        };
      } else {
        next.fhir = {
          ok: false,
          data: null,
          error: String(capResult.reason),
        };
      }

      // Patient count
      if (countResult.status === 'fulfilled') {
        next.patientCount = { count: countResult.value, error: null };
      } else {
        next.patientCount = {
          count: null,
          error: String(countResult.reason),
        };
      }

      return next;
    });

    setLastChecked(new Date());
    setRefreshing(false);
  }, []);

  // -----------------------------------------------------------------------
  // Initial fetch + auto-refresh interval
  // -----------------------------------------------------------------------

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }

    if (autoRefresh) {
      intervalRef.current = setInterval(() => {
        refresh();
      }, AUTO_REFRESH_INTERVAL_MS);
    }

    return () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    };
  }, [autoRefresh, refresh]);

  // -----------------------------------------------------------------------
  // Dependency checks from the ready response
  // -----------------------------------------------------------------------

  const checks = state.ready.data?.checks ?? {};
  const checkEntries = Object.entries(checks);

  // -----------------------------------------------------------------------
  // Memoized JSON strings for code viewers — avoids recomputing on every
  // render and prevents wrapLongLines from re-running the expensive highlight
  // pass each time an unrelated state field changes.
  // -----------------------------------------------------------------------

  const MAX_CAP_CHARS = 30_000;

  const healthJson = useMemo(
    () => (state.medanon.data ? JSON.stringify(state.medanon.data, null, 2) : ''),
    [state.medanon.data],
  );

  const readyJson = useMemo(
    () => (state.ready.data ? JSON.stringify(state.ready.data, null, 2) : ''),
    [state.ready.data],
  );

  const capabilityJson = useMemo(() => {
    if (!state.fhir.data) return '';
    const full = JSON.stringify(state.fhir.data.raw, null, 2);
    if (full.length <= MAX_CAP_CHARS) return full;
    return (
      full.slice(0, MAX_CAP_CHARS) +
      '\n\n// ... document truncated (too large to display in full)'
    );
  }, [state.fhir.data]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Status Dashboard"
        description="Live health, readiness, and dependency status of all services"
      />

      {/* Auto-refresh controls */}
      <div className="mb-6 flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={autoRefresh}
            onCheckedChange={(checked) => setAutoRefresh(Boolean(checked))}
          />
          Auto-refresh (30s)
        </label>

        {lastChecked && (
          <span className="text-sm text-muted-foreground">
            Last checked: {formatTime(lastChecked)}
          </span>
        )}

        <Button
          variant="outline"
          size="sm"
          onClick={() => refresh()}
          disabled={refreshing}
        >
          <RefreshCw
            className={`size-3.5 ${refreshing ? 'animate-spin' : ''}`}
          />
          Refresh now
        </Button>
      </div>

      {/* Service health cards */}
      <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {/* MedAnon */}
        <Card>
          <CardHeader>
            <CardTitle>MedAnon</CardTitle>
            <CardDescription>De-identification engine</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <HealthBadge
              ok={state.medanon.ok}
              version={state.medanon.data?.version}
              loading={state.loading}
            />
            {state.medanon.error && (
              <p className="text-xs text-destructive">{state.medanon.error}</p>
            )}
            {state.medanon.data && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                <dt className="text-muted-foreground">Status</dt>
                <dd>{state.medanon.data.status}</dd>
                {state.medanon.data.version && (
                  <>
                    <dt className="text-muted-foreground">Version</dt>
                    <dd>{state.medanon.data.version}</dd>
                  </>
                )}
              </dl>
            )}
          </CardContent>
        </Card>

        {/* HAPI FHIR */}
        <Card>
          <CardHeader>
            <CardTitle>HAPI FHIR</CardTitle>
            <CardDescription>FHIR R4 server</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <HealthBadge ok={state.fhir.ok} loading={state.loading} />
            {state.fhir.error && (
              <p className="text-xs text-destructive">{state.fhir.error}</p>
            )}
            {state.fhir.data && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                <dt className="text-muted-foreground">FHIR Version</dt>
                <dd>{state.fhir.data.fhirVersion}</dd>
                <dt className="text-muted-foreground">Software</dt>
                <dd>{state.fhir.data.softwareName}</dd>
                <dt className="text-muted-foreground">Software Version</dt>
                <dd>{state.fhir.data.softwareVersion}</dd>
              </dl>
            )}
          </CardContent>
        </Card>

        {/* gPAS */}
        <Card>
          <CardHeader>
            <CardTitle>gPAS</CardTitle>
            <CardDescription>Pseudonymization service</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <HealthBadge ok={state.gpas.ok} loading={state.loading} />
            {state.gpas.error && (
              <p className="text-xs text-destructive">{state.gpas.error}</p>
            )}
            {state.gpas.status !== null && (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                <dt className="text-muted-foreground">Status</dt>
                <dd>{state.gpas.status}</dd>
              </dl>
            )}
          </CardContent>
        </Card>
      </div>

      {/* HAPI FHIR statistics */}
      <div className="mb-6">
        <h2 className="mb-3 text-lg font-semibold">HAPI FHIR Statistics</h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <MetricCard
            label="Patient Count"
            value={
              state.patientCount.count !== null
                ? state.patientCount.count
                : state.patientCount.error
                  ? 'N/A'
                  : '...'
            }
            variant={
              state.patientCount.error
                ? 'destructive'
                : state.patientCount.count !== null
                  ? 'default'
                  : 'default'
            }
          />
        </div>
        {state.patientCount.error && (
          <p className="mt-2 text-xs text-destructive">
            {state.patientCount.error}
          </p>
        )}
      </div>

      {/* Dependency checks table */}
      {checkEntries.length > 0 && (
        <div className="mb-6">
          <h2 className="mb-3 text-lg font-semibold">Dependency Checks</h2>
          <Card>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Check</TableHead>
                    <TableHead>Status</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {checkEntries.map(([name, status]) => {
                    const isOk =
                      status === 'ok' ||
                      status === 'healthy' ||
                      status === 'available';
                    return (
                      <TableRow key={name}>
                        <TableCell className="font-medium">{name}</TableCell>
                        <TableCell>
                          <span
                            className={
                              isOk
                                ? 'text-emerald-600 dark:text-emerald-400'
                                : 'text-destructive'
                            }
                          >
                            {status}
                          </span>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Raw response viewers */}
      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Raw Responses</h2>

        {/* MedAnon /health */}
        <Collapsible>
          <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg border px-4 py-2 text-sm font-medium hover:bg-muted/50 transition-colors">
            <ChevronDown className="size-4 transition-transform [[data-panel-open]_&]:rotate-180" />
            MedAnon Health Response
          </CollapsibleTrigger>
          <CollapsibleContent className="pt-2">
            {healthJson ? (
              <FhirCodeViewer code={healthJson} />
            ) : (
              <p className="px-4 text-sm text-muted-foreground">
                {state.loading ? 'Loading...' : 'No data available'}
              </p>
            )}
          </CollapsibleContent>
        </Collapsible>

        {/* MedAnon /ready */}
        <Collapsible>
          <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg border px-4 py-2 text-sm font-medium hover:bg-muted/50 transition-colors">
            <ChevronDown className="size-4 transition-transform [[data-panel-open]_&]:rotate-180" />
            MedAnon Readiness Response
          </CollapsibleTrigger>
          <CollapsibleContent className="pt-2">
            {readyJson ? (
              <FhirCodeViewer code={readyJson} />
            ) : (
              <p className="px-4 text-sm text-muted-foreground">
                {state.loading ? 'Loading...' : 'No data available'}
              </p>
            )}
          </CollapsibleContent>
        </Collapsible>

        {/* HAPI FHIR /metadata */}
        <Collapsible>
          <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg border px-4 py-2 text-sm font-medium hover:bg-muted/50 transition-colors">
            <ChevronDown className="size-4 transition-transform [[data-panel-open]_&]:rotate-180" />
            HAPI FHIR CapabilityStatement
          </CollapsibleTrigger>
          <CollapsibleContent className="pt-2">
            {capabilityJson ? (
              <FhirCodeViewer code={capabilityJson} />
            ) : (
              <p className="px-4 text-sm text-muted-foreground">
                {state.loading ? 'Loading...' : 'No data available'}
              </p>
            )}
          </CollapsibleContent>
        </Collapsible>
      </div>
    </div>
  );
}
