import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { health, ready, listJobs } from '@/api/medanon';
import { capabilityStatement, fetchResourceTypeCounts } from '@/api/fhir';
import type { HealthResponse, ReadyResponse } from '@/api/types';
import type { ResourceTypeCount } from '@/api/fhir';
import type { JobResponse } from '@/api/medanon';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from '@/components/ui/collapsible';
import { FhirCodeViewer } from '@/components/shared/FhirCodeViewer';
import {
  RefreshCw,
  ChevronDown,
  Activity,
  Server,
  Database,
  Brain,
  BarChart3,
  CircleCheck,
  CircleX,
  Clock,
  Loader2,
  ShieldCheck,
  Cpu,
  AlertTriangle,
  CheckCircle2,
} from 'lucide-react';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const AUTO_REFRESH_SEC = 30;

const RESOURCE_TYPE_COLORS: Record<string, string> = {
  Patient:              'bg-blue-500',
  Observation:          'bg-emerald-500',
  Condition:            'bg-amber-500',
  MedicationRequest:    'bg-purple-500',
  Encounter:            'bg-orange-500',
  Procedure:            'bg-pink-500',
  DiagnosticReport:     'bg-teal-500',
  AllergyIntolerance:   'bg-red-400',
  Immunization:         'bg-lime-500',
  Claim:                'bg-indigo-400',
  ExplanationOfBenefit: 'bg-cyan-500',
  CarePlan:             'bg-violet-400',
};
const FALLBACK_COLORS = ['bg-slate-400', 'bg-sky-400', 'bg-rose-400', 'bg-fuchsia-400'];

function barColor(type: string, idx: number) {
  return RESOURCE_TYPE_COLORS[type] ?? FALLBACK_COLORS[idx % FALLBACK_COLORS.length];
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface CapabilityData {
  fhirVersion: string;
  softwareName: string;
  softwareVersion: string;
  raw: Record<string, unknown>;
}

interface ServiceDef {
  key: string;
  name: string;
  description: string;
  icon: React.ReactNode;
  ok: boolean;
  loading: boolean;
  detail: string | null;
  version?: string;
  error?: string | null;
}

interface PageState {
  loading: boolean;
  medanon:        { ok: boolean; data: HealthResponse | null; error: string | null };
  fhir:           { ok: boolean; data: CapabilityData | null; error: string | null };
  ready:          { ok: boolean; data: ReadyResponse  | null; error: string | null };
  resourceCounts: { data: ResourceTypeCount[] | null; error: string | null };
  jobs:           { data: JobResponse[] | null;       error: string | null };
}

const initialState: PageState = {
  loading: true,
  medanon:        { ok: false, data: null, error: null },
  fhir:           { ok: false, data: null, error: null },
  ready:          { ok: false, data: null, error: null },
  resourceCounts: { data: null, error: null },
  jobs:           { data: null, error: null },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function parseCapability(raw: Record<string, unknown>): CapabilityData {
  const sw = raw.software as Record<string, unknown> | undefined;
  return {
    fhirVersion:     String(raw.fhirVersion ?? 'unknown'),
    softwareName:    String(sw?.name    ?? 'unknown'),
    softwareVersion: String(sw?.version ?? 'unknown'),
    raw,
  };
}

function formatRelative(date: Date): string {
  const sec = Math.floor((Date.now() - date.getTime()) / 1000);
  if (sec < 5)  return 'just now';
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  return min === 1 ? '1 min ago' : `${min} mins ago`;
}

function fmtNum(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000)     return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function StatusPulse({ ok, loading }: { ok: boolean; loading: boolean }) {
  if (loading) return (
    <span className="size-2.5 flex items-center justify-center">
      <span className="size-2 animate-pulse rounded-full bg-muted-foreground/40" />
    </span>
  );
  return (
    <span className="relative inline-flex size-2.5">
      {ok && (
        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-60" />
      )}
      <span className={cn(
        'relative inline-flex size-2.5 rounded-full',
        ok ? 'bg-emerald-500' : 'bg-red-500',
      )} />
    </span>
  );
}

function ServiceCard({ svc }: { svc: ServiceDef }) {
  return (
    <Card className={cn(
      'relative overflow-hidden transition-colors',
      !svc.loading && (svc.ok ? 'border-emerald-200/70' : 'border-red-200/70'),
    )}>
      <div className={cn(
        'h-0.5 w-full',
        svc.loading ? 'bg-muted/40' : svc.ok ? 'bg-emerald-400' : 'bg-red-400',
      )} />
      <CardContent className="flex flex-col gap-2.5 pb-3 pt-4">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <span className={cn(
              'flex size-8 shrink-0 items-center justify-center rounded-lg',
              svc.loading ? 'bg-muted text-muted-foreground'
                : svc.ok   ? 'bg-emerald-50 text-emerald-600'
                           : 'bg-red-50 text-red-500',
            )}>
              {svc.icon}
            </span>
            <div>
              <p className="text-sm font-semibold leading-none">{svc.name}</p>
              <p className="mt-0.5 text-[11px] text-muted-foreground">{svc.description}</p>
            </div>
          </div>
          <div className="mt-0.5 flex shrink-0 items-center gap-1.5">
            <StatusPulse ok={svc.ok} loading={svc.loading} />
            <span className={cn(
              'text-xs font-medium',
              svc.loading ? 'text-muted-foreground'
                : svc.ok  ? 'text-emerald-600'
                          : 'text-red-500',
            )}>
              {svc.loading ? 'Checking' : svc.ok ? 'Online' : 'Offline'}
            </span>
          </div>
        </div>

        {svc.version && (
          <p className="font-mono text-[11px] text-muted-foreground">{svc.version}</p>
        )}
        {svc.detail && !svc.error && (
          <p className="text-[11px] text-muted-foreground">{svc.detail}</p>
        )}
        {svc.error && (
          <p className="truncate text-[11px] text-destructive" title={svc.error}>{svc.error}</p>
        )}
      </CardContent>
    </Card>
  );
}

function StatBox({
  label, value, accent,
}: { label: string; value: number; accent: string }) {
  return (
    <div className="relative overflow-hidden rounded-xl border bg-card px-4 py-3 shadow-sm">
      <div className={cn('absolute left-0 top-0 h-full w-1', accent)} />
      <p className="pl-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="pl-1 mt-0.5 text-2xl font-bold tabular-nums leading-tight">
        {value}
      </p>
    </div>
  );
}

function JobRow({ job }: { job: JobResponse }) {
  const isActive = job.status === 'pending' || job.status === 'running';
  const statusStyles: Record<string, string> = {
    pending:   'bg-amber-100 text-amber-800',
    running:   'bg-blue-100 text-blue-800',
    done:      'bg-emerald-100 text-emerald-800',
    error:     'bg-red-100 text-red-800',
    cancelled: 'bg-slate-100 text-slate-600',
  };
  return (
    <div className="flex items-center gap-3 px-4 py-2.5">
      {isActive ? (
        <Loader2 className="size-3.5 shrink-0 animate-spin text-blue-500" />
      ) : job.status === 'done' ? (
        <CircleCheck className="size-3.5 shrink-0 text-emerald-500" />
      ) : job.status === 'error' ? (
        <CircleX className="size-3.5 shrink-0 text-red-500" />
      ) : (
        <span className="size-3.5 shrink-0 rounded-full border border-muted-foreground/30" />
      )}
      <span className="font-mono text-[11px] text-muted-foreground">
        {job.job_id.slice(0, 8)}…
      </span>
      <span className="flex-1 truncate text-xs capitalize text-foreground">
        {job.type.replace(/-/g, ' ')}
      </span>
      <span className="tabular-nums text-xs text-muted-foreground">
        {job.processed.toLocaleString()} res
      </span>
      <span className={cn(
        'inline-block rounded px-1.5 py-0.5 text-[10px] font-medium uppercase',
        statusStyles[job.status] ?? 'bg-muted text-muted-foreground',
      )}>
        {job.status}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// StatusPage
// ---------------------------------------------------------------------------

export default function StatusPage() {
  const [state, setState] = useState<PageState>(initialState);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [relTime, setRelTime] = useState('');
  const [refreshing, setRefreshing] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [countdown, setCountdown] = useState(AUTO_REFRESH_SEC);
  const autoIntervalRef  = useRef<ReturnType<typeof setInterval> | null>(null);
  const countdownRef     = useRef<ReturnType<typeof setInterval> | null>(null);
  // Track mount status so we don't setState on an unmounted component when
  // the user navigates away mid-request (5 parallel fetches can each settle
  // after unmount, producing console warnings + wasted work).
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // ── Refresh ───────────────────────────────────────────────────────────────

  const refresh = useCallback(async () => {
    setRefreshing(true);
    setCountdown(AUTO_REFRESH_SEC);

    const [healthRes, readyRes, capRes, countsRes, jobsRes] =
      await Promise.allSettled([
        health(),
        ready(),
        capabilityStatement(),
        fetchResourceTypeCounts(),
        listJobs({ limit: 100 }),
      ]);

    if (!mountedRef.current) return;

    setState((prev) => {
      const next: PageState = { ...prev, loading: false };

      next.medanon = healthRes.status === 'fulfilled'
        ? { ok: ['ok', 'healthy'].includes(healthRes.value.status), data: healthRes.value, error: null }
        : { ok: false, data: null, error: String(healthRes.reason) };

      next.ready = readyRes.status === 'fulfilled'
        ? { ok: readyRes.value.ready, data: readyRes.value, error: null }
        : { ok: false, data: null, error: String(readyRes.reason) };

      next.fhir = capRes.status === 'fulfilled'
        ? { ok: true, data: parseCapability(capRes.value), error: null }
        : { ok: false, data: null, error: String(capRes.reason) };

      next.resourceCounts = countsRes.status === 'fulfilled'
        ? { data: countsRes.value, error: null }
        : { data: null, error: String(countsRes.reason) };

      next.jobs = jobsRes.status === 'fulfilled'
        ? { data: jobsRes.value, error: null }
        : { data: null, error: String(jobsRes.reason) };

      return next;
    });

    setLastChecked(new Date());
    setRefreshing(false);
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // ── Relative time ticker ──────────────────────────────────────────────────
  useEffect(() => {
    if (lastChecked) setRelTime(formatRelative(lastChecked));
    const t = setInterval(() => {
      if (lastChecked) setRelTime(formatRelative(lastChecked));
    }, 5000);
    return () => clearInterval(t);
  }, [lastChecked]);

  // ── Auto-refresh ──────────────────────────────────────────────────────────
  useEffect(() => {
    if (autoIntervalRef.current) clearInterval(autoIntervalRef.current);
    if (countdownRef.current)    clearInterval(countdownRef.current);

    if (autoRefresh) {
      setCountdown(AUTO_REFRESH_SEC);
      autoIntervalRef.current = setInterval(() => {
        refresh();
        setCountdown(AUTO_REFRESH_SEC);
      }, AUTO_REFRESH_SEC * 1000);
      countdownRef.current = setInterval(
        () => setCountdown((c) => Math.max(0, c - 1)),
        1000,
      );
    }

    return () => {
      if (autoIntervalRef.current) clearInterval(autoIntervalRef.current);
      if (countdownRef.current)    clearInterval(countdownRef.current);
    };
  }, [autoRefresh, refresh]);

  // ── Derived values ────────────────────────────────────────────────────────

  const checks = useMemo(() => state.ready.data?.checks ?? {}, [state.ready.data]);

  const services: ServiceDef[] = useMemo(() => {
    const CHECK_META: Record<string, { name: string; description: string; icon: React.ReactNode }> = {
      gpas:      { name: 'gPAS',      description: 'Pseudonymization service', icon: <Cpu       className="size-4" /> },
      gPAS:      { name: 'gPAS',      description: 'Pseudonymization service', icon: <Cpu       className="size-4" /> },
      redis:     { name: 'Redis',     description: 'Shared cache & job queue',  icon: <Server    className="size-4" /> },
      nlp:       { name: 'NLP',       description: 'Presidio NLP microservice', icon: <Brain     className="size-4" /> },
      analytics: { name: 'Analytics', description: 'Risk & synthetic data',     icon: <BarChart3 className="size-4" /> },
    };

    const defs: ServiceDef[] = [
      {
        key: 'medanon',
        name: 'MedAnon',
        description: 'De-identification engine',
        icon: <ShieldCheck className="size-4" />,
        ok: state.medanon.ok,
        loading: state.loading,
        detail: state.medanon.data?.status ?? null,
        version: state.medanon.data?.version ? `v${state.medanon.data.version}` : undefined,
        error: state.medanon.error,
      },
      {
        key: 'fhir',
        name: 'HAPI FHIR',
        description: 'FHIR R4 source server',
        icon: <Database className="size-4" />,
        ok: state.fhir.ok,
        loading: state.loading,
        detail: state.fhir.data
          ? `${state.fhir.data.softwareName} ${state.fhir.data.softwareVersion}`
          : null,
        version: state.fhir.data ? `FHIR ${state.fhir.data.fhirVersion}` : undefined,
        error: state.fhir.error,
      },
    ];

    const seen = new Set<string>();
    for (const [k, status] of Object.entries(checks)) {
      const meta = CHECK_META[k];
      if (!meta || seen.has(meta.name)) continue;
      seen.add(meta.name);
      const isOk  = ['ok', 'healthy', 'available'].includes(status);
      const isOff = ['not configured', 'disabled'].includes(status);
      defs.push({
        key: k,
        name: meta.name,
        description: meta.description,
        icon: meta.icon,
        ok:      isOk,
        loading: state.loading,
        detail:  isOff ? 'Not configured' : isOk ? null : status,
        error:   !isOk && !isOff ? `Status: ${status}` : null,
      });
    }

    return defs;
  }, [state.loading, state.medanon, state.fhir, checks]);

  const servicesOnline = services.filter((s) => !s.loading && s.ok).length;
  const servicesTotal  = services.filter((s) => !s.loading).length;
  const allOk   = servicesTotal > 0 && servicesOnline === servicesTotal;
  const someDown = servicesTotal > 0 && servicesOnline < servicesTotal;

  const totalResources = state.resourceCounts.data?.reduce((s, r) => s + r.count, 0) ?? 0;
  const topResources   = state.resourceCounts.data?.slice(0, 14) ?? [];

  const jobData = useMemo(() => state.jobs.data ?? [], [state.jobs.data]);
  const jobStats = useMemo(() => ({
    total:     jobData.length,
    pending:   jobData.filter((j) => j.status === 'pending').length,
    running:   jobData.filter((j) => j.status === 'running').length,
    done:      jobData.filter((j) => j.status === 'done').length,
    error:     jobData.filter((j) => j.status === 'error').length,
    cancelled: jobData.filter((j) => j.status === 'cancelled').length,
  }), [jobData]);

  const activeJobs  = useMemo(() => jobData.filter((j) => j.status === 'pending' || j.status === 'running').slice(0, 8),  [jobData]);
  const recentJobs  = useMemo(() => jobData.filter((j) => j.status === 'done'    || j.status === 'error').slice(0, 5),  [jobData]);

  const MAX_CAP_CHARS = 30_000;
  const healthJson     = useMemo(() => state.medanon.data ? JSON.stringify(state.medanon.data, null, 2) : '', [state.medanon.data]);
  const readyJson      = useMemo(() => state.ready.data   ? JSON.stringify(state.ready.data,   null, 2) : '', [state.ready.data]);
  const capabilityJson = useMemo(() => {
    if (!state.fhir.data) return '';
    const full = JSON.stringify(state.fhir.data.raw, null, 2);
    return full.length <= MAX_CAP_CHARS ? full : full.slice(0, MAX_CAP_CHARS) + '\n\n// … truncated';
  }, [state.fhir.data]);

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col gap-8">
      <PageHeader
        title="Status Dashboard"
        description="Live health, readiness, and dependency status of all services"
      />

      {/* ── Toolbar ── */}
      <div className="flex flex-wrap items-center gap-3">
        {/* Overall health pill */}
        <div className={cn(
          'flex items-center gap-2 rounded-full border px-3 py-1 text-sm font-medium',
          state.loading
            ? 'border-muted bg-muted/30 text-muted-foreground'
            : allOk
              ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
              : someDown
                ? 'border-amber-200  bg-amber-50  text-amber-700'
                : 'border-muted bg-muted/30 text-muted-foreground',
        )}>
          {state.loading ? (
            <Loader2       className="size-3.5 animate-spin" />
          ) : allOk ? (
            <CheckCircle2  className="size-3.5" />
          ) : (
            <AlertTriangle className="size-3.5" />
          )}
          {state.loading
            ? 'Checking services…'
            : allOk
              ? `All ${servicesTotal} services operational`
              : `${servicesOnline} / ${servicesTotal} services online`}
        </div>

        <div className="ml-auto flex items-center gap-3">
          {lastChecked && (
            <span className="flex items-center gap-1 text-xs text-muted-foreground">
              <Clock className="size-3" />
              {relTime}
            </span>
          )}

          {/* Auto-refresh toggle */}
          <button
            onClick={() => setAutoRefresh((v) => !v)}
            className={cn(
              'flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
              autoRefresh
                ? 'border-primary/30 bg-primary/10 text-primary'
                : 'border-border text-muted-foreground hover:bg-muted/50',
            )}
          >
            <Activity className="size-3" />
            {autoRefresh ? `Auto (${countdown}s)` : 'Auto-refresh'}
          </button>

          <Button variant="outline" size="sm" onClick={refresh} disabled={refreshing}>
            <RefreshCw className={cn('size-3.5', refreshing && 'animate-spin')} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Service Cards ── */}
      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          Services
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {services.map((svc) => <ServiceCard key={svc.key} svc={svc} />)}
        </div>
      </section>

      {/* ── FHIR Resource Inventory ── */}
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
            FHIR Source — Resource Inventory
          </h2>
          {totalResources > 0 && (
            <span className="text-sm font-semibold tabular-nums">
              {totalResources.toLocaleString()} total resources
            </span>
          )}
        </div>

        {state.resourceCounts.error ? (
          <p className="text-xs text-destructive">{state.resourceCounts.error}</p>
        ) : state.loading ? (
          <div className="flex gap-3">
            {[1, 2, 3, 4].map((i) => (
              <div key={i} className="h-10 flex-1 animate-pulse rounded-xl border bg-muted/30" />
            ))}
          </div>
        ) : topResources.length === 0 ? (
          <p className="text-sm text-muted-foreground">No resources found in FHIR server.</p>
        ) : (
          <div className="rounded-xl border overflow-hidden">
            <div className="divide-y">
              {topResources.map((rc, idx) => {
                const pct = totalResources > 0 ? (rc.count / totalResources) * 100 : 0;
                const color = barColor(rc.type, idx);
                return (
                  <div key={rc.type} className="flex items-center gap-3 px-4 py-2.5 hover:bg-muted/20 transition-colors">
                    <span className={cn('size-2 shrink-0 rounded-full', color)} />
                    <span className="w-44 shrink-0 text-sm font-medium truncate">{rc.type}</span>
                    <div className="flex-1">
                      <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
                        <div
                          className={cn('h-full rounded-full transition-all duration-500', color)}
                          style={{ width: `${Math.max(pct, 0.4)}%` }}
                        />
                      </div>
                    </div>
                    <span className="w-14 text-right text-xs text-muted-foreground tabular-nums">
                      {pct.toFixed(1)}%
                    </span>
                    <span className="w-20 text-right text-sm font-semibold tabular-nums">
                      {rc.count.toLocaleString()}
                    </span>
                  </div>
                );
              })}
            </div>
            {totalResources > 0 && (
              <div className="flex items-center justify-between border-t bg-muted/20 px-4 py-2">
                <span className="text-xs text-muted-foreground">
                  {topResources.length} resource type{topResources.length !== 1 ? 's' : ''} shown
                </span>
                <span className="text-xs font-semibold tabular-nums">
                  Total: {totalResources.toLocaleString()} ({fmtNum(totalResources)})
                </span>
              </div>
            )}
          </div>
        )}
      </section>

      {/* ── Job Queue ── */}
      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          Job Queue
        </h2>

        {state.jobs.error ? (
          <p className="text-xs text-destructive">{state.jobs.error}</p>
        ) : (
          <>
            <div className="mb-4 grid grid-cols-3 gap-3 sm:grid-cols-6">
              <StatBox label="Total"     value={jobStats.total}     accent="bg-primary/60" />
              <StatBox label="Pending"   value={jobStats.pending}   accent={jobStats.pending > 0   ? 'bg-amber-400'   : 'bg-muted/40'} />
              <StatBox label="Running"   value={jobStats.running}   accent={jobStats.running > 0   ? 'bg-blue-500'    : 'bg-muted/40'} />
              <StatBox label="Done"      value={jobStats.done}      accent={jobStats.done > 0      ? 'bg-emerald-500' : 'bg-muted/40'} />
              <StatBox label="Error"     value={jobStats.error}     accent={jobStats.error > 0     ? 'bg-red-500'     : 'bg-muted/40'} />
              <StatBox label="Cancelled" value={jobStats.cancelled} accent="bg-muted/40" />
            </div>

            {activeJobs.length > 0 && (
              <div className="mb-3 rounded-xl border overflow-hidden">
                <div className="border-b bg-blue-50/60 px-4 py-2 text-[11px] font-semibold uppercase tracking-wide text-blue-700">
                  Active — {activeJobs.length} job{activeJobs.length !== 1 ? 's' : ''}
                </div>
                <div className="divide-y">
                  {activeJobs.map((j) => <JobRow key={j.job_id} job={j} />)}
                </div>
              </div>
            )}

            {recentJobs.length > 0 && (
              <div className="rounded-xl border overflow-hidden">
                <div className="border-b bg-muted/30 px-4 py-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  Recent — {recentJobs.length} job{recentJobs.length !== 1 ? 's' : ''}
                </div>
                <div className="divide-y">
                  {recentJobs.map((j) => <JobRow key={j.job_id} job={j} />)}
                </div>
              </div>
            )}

            {jobStats.total === 0 && !state.loading && (
              <p className="text-sm text-muted-foreground">No jobs in the queue.</p>
            )}
          </>
        )}
      </section>

      {/* ── Dependency Check Pills ── */}
      {Object.keys(checks).length > 0 && (
        <section>
          <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
            Dependency Checks
          </h2>
          <div className="flex flex-wrap gap-2">
            {Object.entries(checks).map(([name, status]) => {
              const ok  = ['ok', 'healthy', 'available'].includes(status);
              const off = ['not configured', 'disabled'].includes(status);
              return (
                <div key={name} className={cn(
                  'flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium',
                  ok  ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                  : off ? 'border-muted bg-muted/30 text-muted-foreground'
                       : 'border-red-200 bg-red-50 text-red-700',
                )}>
                  <StatusPulse ok={ok} loading={false} />
                  <span className="font-semibold">{name}</span>
                  <span className="opacity-60">·</span>
                  <span>{status}</span>
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* ── Raw Responses ── */}
      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-widest text-muted-foreground">
          Raw Responses
        </h2>
        <div className="space-y-2">
          {([
            { label: 'MedAnon /health',    json: healthJson },
            { label: 'MedAnon /ready',     json: readyJson },
            { label: 'HAPI FHIR /metadata', json: capabilityJson },
          ] as const).map(({ label, json }) => (
            <Collapsible key={label}>
              <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-medium transition-colors hover:bg-muted/50">
                <ChevronDown className="size-4 shrink-0 transition-transform [[data-panel-open]_&]:rotate-180" />
                {label}
              </CollapsibleTrigger>
              <CollapsibleContent className="pt-2">
                {json ? (
                  <FhirCodeViewer code={json} maxHeight="320px" />
                ) : (
                  <p className="px-4 py-3 text-sm text-muted-foreground">
                    {state.loading ? 'Loading…' : 'No data available'}
                  </p>
                )}
              </CollapsibleContent>
            </Collapsible>
          ))}
        </div>
      </section>
    </div>
  );
}
