import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { ConfirmDialog } from '@/components/shared/ConfirmDialog';
import { toast } from 'sonner';
import { PageHeader } from '@/components/layout/PageHeader';
import {
  listProcessingRuns,
  getProcessingRunStats,
  purgeProcessingRuns,
} from '@/api/processingRuns';
import type { ProcessingRun, ProcessingRunStats, TrustPassport } from '@/api/processingRuns';
import { ApiError } from '@/api/types';
import { QualityPassportPanel } from '@/components/shared/QualityPassportPanel';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  RefreshCw,
  ChevronDown,
  Trash2,
  ChevronLeft,
  ChevronRight,
  Loader2,
  CheckCircle2,
  ShieldAlert,
} from 'lucide-react';
import { cn } from '@/lib/utils';

const PAGE_SIZE = 25;

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const min = Math.floor(sec / 60);
  return `${min}m ${Math.round(sec % 60)}s`;
}

function fmtDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  } catch {
    return iso;
  }
}

function ScoreBadge({ score }: { score: Record<string, unknown> | null }) {
  if (!score) return <span className="text-xs text-muted-foreground">—</span>;
  const avgComposite = typeof score.avg_composite === 'number' ? score.avg_composite : null;
  if (avgComposite === null) return <span className="text-xs text-muted-foreground">N/A</span>;
  const pct = Math.round(avgComposite);
  const color =
    pct >= 80 ? 'bg-emerald-100 text-emerald-800' :
    pct >= 60 ? 'bg-amber-100 text-amber-800' :
    'bg-red-100 text-red-800';
  return (
    <span className={cn('inline-block rounded px-1.5 py-0.5 text-[11px] font-semibold tabular-nums', color)}>
      {pct}%
    </span>
  );
}

function PassportChip({ passport }: { passport: TrustPassport | null }) {
  if (!passport) return null;
  const map: Record<string, string> = {
    PASS: 'bg-emerald-100 text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300',
    CONDITIONAL_PASS: 'bg-amber-100 text-amber-800 dark:bg-amber-950/40 dark:text-amber-300',
    BLOCK: 'bg-red-100 text-red-800 dark:bg-red-950/40 dark:text-red-300',
  };
  const label = passport.decision === 'CONDITIONAL_PASS' ? 'COND' : passport.decision;
  return (
    <span
      className={cn('inline-block rounded px-1.5 py-0.5 text-[10px] font-semibold', map[passport.decision] ?? 'bg-muted')}
      title={`Trust Gate: ${passport.decision} · ${passport.overall_score != null ? Math.round(passport.overall_score) : "n/a"}% checks passing`}
    >
      {label}
    </span>
  );
}

// ── Expanded detail panel ────────────────────────────────────────────────────

interface ScoreObj {
  computed?: boolean;
  total_scored?: number;
  pass_count?: number;
  fail_count?: number;
  error_count?: number;
  avg_composite?: number;
  min_composite?: number;
  avg_utility?: number;
  avg_quality?: number;
  identifier_risk_hits?: number;
  text_risk_hits?: number;
  batch_privacy?: {
    passed: boolean;
    risk_score: number;
    threshold: number;
    attacker_risk?: number;
    identifier_risk?: number;
    text_risk?: number;
    evidence?: { check: string; value: number; details?: Record<string,unknown>; severity?: string }[];
  } | null;
}

function Bar({ value, color }: { value: number; color: string }) {
  const pct = Math.min(100, Math.max(0, Math.round(value)));
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 rounded-full bg-muted overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums font-semibold w-9 text-right">{pct}%</span>
    </div>
  );
}

function KV({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 py-1.5 border-b last:border-0">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-xs font-medium tabular-nums">{value}</span>
    </div>
  );
}

function RunDetailPanel({ run }: { run: ProcessingRun }) {
  const score = run.score as ScoreObj | null;
  const privacy = score?.batch_privacy;

  // Collect scalar summary fields not already displayed elsewhere
  const SUMMARY_SKIP = new Set(['total_resources', 'config_profile', 'error_count', 'completed_at']);
  const summaryExtras = run.summary
    ? Object.entries(run.summary).filter(([k, v]) =>
        !SUMMARY_SKIP.has(k) && typeof v !== 'object'
      )
    : [];

  const identifierHits = score?.identifier_risk_hits ?? 0;
  const textHits       = score?.text_risk_hits       ?? 0;
  const evidence       = privacy?.evidence ?? [];

  return (
    <div className="bg-muted/10 border-t px-4 py-4">
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">

        {/* 1, Score breakdown */}
        {score?.computed && (
          <div className="space-y-2">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Score Breakdown</p>
            {score.avg_composite != null && (
              <div><p className="text-[10px] text-muted-foreground mb-0.5">Composite</p><Bar value={score.avg_composite} color="bg-primary" /></div>
            )}
            {score.avg_utility != null && (
              <div><p className="text-[10px] text-muted-foreground mb-0.5">Utility (data preserved)</p><Bar value={score.avg_utility * 100} color="bg-sky-500" /></div>
            )}
            {score.avg_quality != null && (
              <div><p className="text-[10px] text-muted-foreground mb-0.5">Quality (structure)</p><Bar value={score.avg_quality * 100} color="bg-amber-500" /></div>
            )}
            {score.min_composite != null && (
              <p className="text-[10px] text-muted-foreground">Min: <span className="font-mono font-semibold">{score.min_composite.toFixed(1)}%</span></p>
            )}
          </div>
        )}

        {/* 2, De-identification actions */}
        <div className="space-y-1">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-2">De-identification Actions</p>

          {/* Pseudonymization, 0 risk hits = successfully protected */}
          <div className={cn(
            'flex items-center justify-between rounded-lg border px-3 py-2 text-xs',
            identifierHits === 0
              ? 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-800/30 dark:bg-emerald-950/20'
              : 'border-amber-200 bg-amber-50/60 dark:border-amber-800/30 dark:bg-amber-950/20',
          )}>
            <span className="font-medium">Identifier pseudonymisation</span>
            <span className={cn('font-semibold', identifierHits === 0 ? 'text-emerald-600' : 'text-amber-600')}>
              {identifierHits === 0 ? 'Protected' : `${identifierHits} residual risks`}
            </span>
          </div>

          {/* NLP text scrub, 0 hits = no PHI text found */}
          <div className={cn(
            'flex items-center justify-between rounded-lg border px-3 py-2 text-xs',
            textHits === 0
              ? 'border-emerald-200 bg-emerald-50/60 dark:border-emerald-800/30 dark:bg-emerald-950/20'
              : 'border-amber-200 bg-amber-50/60 dark:border-amber-800/30 dark:bg-amber-950/20',
          )}>
            <span className="font-medium">NLP text scrubbing</span>
            <span className={cn('font-semibold', textHits === 0 ? 'text-emerald-600' : 'text-amber-600')}>
              {textHits === 0 ? 'Clean' : `${textHits} PHI hits`}
            </span>
          </div>

          {score?.total_scored != null && (
            <div className="flex items-center justify-between rounded-lg border border-muted bg-muted/30 px-3 py-2 text-xs">
              <span className="font-medium">Resources scored</span>
              <span className="font-mono font-bold tabular-nums">{score.total_scored.toLocaleString()}</span>
            </div>
          )}
          {score?.pass_count != null && (
            <div className="flex items-center justify-between rounded-lg border border-muted bg-muted/30 px-3 py-2 text-xs">
              <span className="font-medium">Pass / fail</span>
              <span className="font-mono font-bold tabular-nums">
                <span className="text-emerald-600">{score.pass_count}</span>
                {' / '}
                <span className={score.fail_count ? 'text-destructive' : 'text-muted-foreground'}>{score.fail_count ?? 0}</span>
              </span>
            </div>
          )}
          {summaryExtras.map(([k, v]) => (
            <div key={k} className="flex items-center justify-between rounded-lg border border-muted bg-muted/30 px-3 py-2 text-xs">
              <span className="font-medium capitalize">{k.replace(/_/g, ' ')}</span>
              <span className="font-mono tabular-nums">{String(v)}</span>
            </div>
          ))}
        </div>

        {/* 3, Run details */}
        <div className="space-y-1">
          <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-2">Run Details</p>
          <KV label="Resources" value={run.resource_count.toLocaleString()} />
          <KV label="Errors" value={
            run.error_count > 0
              ? <span className="text-destructive font-semibold">{run.error_count}</span>
              : <span className="text-emerald-600">0</span>
          } />
          <KV label="Duration" value={fmtDuration(run.duration_ms)} />
          <KV label="Input type" value={run.input_type} />
          <KV label="Config profile" value={
            <span className="font-mono text-primary">{run.config_profile}</span>
          } />
        </div>

        {/* 4, Privacy gate */}
        <div className="space-y-3">
          {privacy != null && (
            <div>
              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-2">Privacy Gate</p>
              <div className={cn(
                'flex items-center gap-2.5 rounded-lg border px-3 py-2.5 mb-2',
                privacy.passed
                  ? 'border-emerald-200 bg-emerald-50 dark:border-emerald-800/30 dark:bg-emerald-950/20'
                  : 'border-red-200 bg-red-50 dark:border-red-800/30 dark:bg-red-950/20',
              )}>
                {privacy.passed
                  ? <CheckCircle2 className="size-4 text-emerald-600 shrink-0" />
                  : <ShieldAlert className="size-4 text-destructive shrink-0" />
                }
                <div>
                  <p className="text-xs font-bold">{privacy.passed ? 'PASSED' : 'FAILED'}</p>
                  <p className="text-[10px] text-muted-foreground font-mono">
                    Risk {privacy.risk_score.toFixed(3)} / threshold {privacy.threshold}
                  </p>
                </div>
              </div>
              {evidence.length > 0 && (
                <div className="space-y-1">
                  {evidence.map((ev, i) => (
                    <div key={i} className="rounded border bg-muted/30 px-2.5 py-1.5 text-[10px]">
                      <span className="font-semibold">{ev.check.replace(/_/g,' ')}</span>
                      {ev.details && typeof ev.details.reason === 'string' && (
                        <p className="text-muted-foreground mt-0.5">{ev.details.reason}</p>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-1">Run ID</p>
            <p className="font-mono text-[10px] text-muted-foreground break-all">{run.id}</p>
          </div>
        </div>

      </div>

      {run.trust_passport && (
        <div className="mt-4 border-t pt-4 max-w-2xl">
          <QualityPassportPanel passport={run.trust_passport} />
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, accent }: { label: string; value: string | number; accent: string }) {
  return (
    <Card className="relative overflow-hidden">
      <div className={cn('absolute left-0 top-0 h-full w-1', accent)} />
      <CardContent className="py-3 pl-5 pr-4">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</p>
        <p className="mt-0.5 text-2xl font-bold tabular-nums leading-tight">{value}</p>
      </CardContent>
    </Card>
  );
}

export default function ProcessingHistoryPage() {
  const [runs, setRuns] = useState<ProcessingRun[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<ProcessingRunStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [endpointOpen, setEndpointOpen] = useState(false);
  const [purging, setPurging] = useState(false);
  const [confirmPurge, setConfirmPurge] = useState(false);

  const refresh = useCallback(async (pg = page) => {
    setLoading(true);
    setUnavailable(null);
    const [listRes, statsRes] = await Promise.allSettled([
      listProcessingRuns({ limit: PAGE_SIZE, offset: pg * PAGE_SIZE }),
      getProcessingRunStats(),
    ]);
    if (listRes.status === 'fulfilled') {
      setRuns(listRes.value.runs);
      setTotal(listRes.value.total);
    } else if (listRes.reason instanceof ApiError && listRes.reason.status === 503) {
      setUnavailable(listRes.reason.detail);
    } else if (listRes.status === 'rejected') {
      toast.error('Failed to load processing runs', {
        description: (listRes.reason as Error)?.message,
        id: 'history-load-error',
      });
    }
    if (statsRes.status === 'fulfilled') {
      setStats(statsRes.value);
    } else if (statsRes.status === 'rejected') {
      toast.error('Failed to load stats', { id: 'history-stats-error' });
    }
    setLoading(false);
  }, [page]);

  useEffect(() => { refresh(page); }, [page, refresh]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const endpointCounts = useMemo(() => {
    if (!stats?.runs_by_endpoint) return [];
    return Object.entries(stats.runs_by_endpoint)
      .sort(([, a], [, b]) => b - a);
  }, [stats]);

  const handlePurge = useCallback(async () => {
    setPurging(true);
    try {
      const result = await purgeProcessingRuns(30);
      await refresh(0);
      setPage(0);
      toast.success(`Purged ${result.deleted} run${result.deleted !== 1 ? 's' : ''}`);
    } catch (e) {
      toast.error('Purge failed', { description: (e as Error).message });
    }
    setPurging(false);
    setConfirmPurge(false);
  }, [refresh]);

  return (
    <div className="flex flex-col gap-8">
      <PageHeader
        title="Processing History"
        description="De-identification processing runs with scoring and metadata"
      />

      {/* Unavailable banner */}
      {unavailable && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          <span className="font-semibold">Store unavailable:</span> {unavailable}
        </div>
      )}

      {/* Stats row */}
      {stats && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatCard
            label="Total Runs"
            value={
              stats.scored_runs != null && stats.scored_runs < stats.total_runs
                ? `${stats.scored_runs} scored / ${stats.total_runs}`
                : stats.total_runs
            }
            accent="bg-primary/60"
          />
          <StatCard label="Resources Processed" value={stats.total_resources.toLocaleString()} accent="bg-emerald-500" />
          <StatCard
            label="Avg Score"
            value={stats.avg_composite != null ? `${Math.round(stats.avg_composite)}%` : 'N/A'}
            accent="bg-amber-500"
          />
          <StatCard label="Endpoints" value={Object.keys(stats.runs_by_endpoint).length} accent="bg-blue-500" />
        </div>
      )}

      {/* Endpoint breakdown */}
      {endpointCounts.length > 0 && (
        <Collapsible open={endpointOpen} onOpenChange={setEndpointOpen}>
          <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-medium transition-colors hover:bg-muted/50">
            <ChevronDown className={cn('size-4 shrink-0 transition-transform', endpointOpen && 'rotate-180')} />
            Runs by Endpoint
          </CollapsibleTrigger>
          <CollapsibleContent className="pt-2">
            <div className="rounded-xl border divide-y overflow-hidden">
              {endpointCounts.map(([ep, count]) => (
                <div key={ep} className="flex items-center justify-between px-4 py-2 text-sm">
                  <span className="font-mono text-xs">{ep}</span>
                  <span className="tabular-nums font-semibold">{count}</span>
                </div>
              ))}
            </div>
          </CollapsibleContent>
        </Collapsible>
      )}

      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <span className="text-sm text-muted-foreground">
          {total} run{total !== 1 ? 's' : ''}
        </span>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => setConfirmPurge(true)} disabled={purging}>
            <Trash2 className="size-3.5" />
            Purge
          </Button>
          <Button variant="outline" size="sm" onClick={() => refresh(page)} disabled={loading}>
            <RefreshCw className={cn('size-3.5', loading && 'animate-spin')} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Table */}
      {loading && runs.length === 0 ? (
        <div className="flex items-center justify-center h-64">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : unavailable ? null : runs.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No processing runs recorded yet. Enable scoring with <code className="rounded bg-muted px-1 py-0.5 text-xs">MEDANON_SCORING_ENABLED=true</code>.
        </p>
      ) : (
        <div className="rounded-xl border overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="min-w-[140px]">Time</TableHead>
                <TableHead className="min-w-[160px]">Endpoint</TableHead>
                <TableHead className="min-w-[120px]">Profile</TableHead>
                <TableHead className="text-right min-w-[100px]">Resources</TableHead>
                <TableHead className="text-right min-w-[60px]">Errors</TableHead>
                <TableHead className="text-right min-w-[80px]">Duration</TableHead>
                <TableHead className="text-center min-w-[70px]">Score</TableHead>
                <TableHead className="w-8" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <React.Fragment key={run.id}>
                  <TableRow
                    className="cursor-pointer hover:bg-muted/30"
                    onClick={() => setExpandedId(expandedId === run.id ? null : run.id)}
                  >
                    <TableCell className="text-xs tabular-nums whitespace-nowrap">{fmtDate(run.created_at)}</TableCell>
                    <TableCell className="max-w-[180px]">
                      <span className="block truncate font-mono text-xs" title={run.endpoint}>{run.endpoint}</span>
                    </TableCell>
                    <TableCell className="max-w-[120px]">
                      <span className="block truncate text-xs" title={run.config_profile}>{run.config_profile}</span>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{run.resource_count.toLocaleString()}</TableCell>
                    <TableCell className={cn('text-right tabular-nums', run.error_count > 0 && 'text-red-600 font-semibold')}>
                      {run.error_count}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">{fmtDuration(run.duration_ms)}</TableCell>
                    <TableCell className="text-center">
                      <div className="flex flex-col items-center gap-1">
                        <ScoreBadge score={run.score} />
                        <PassportChip passport={run.trust_passport} />
                      </div>
                    </TableCell>
                    <TableCell>
                      <ChevronDown className={cn('size-3.5 text-muted-foreground transition-transform', expandedId === run.id && 'rotate-180')} />
                    </TableCell>
                  </TableRow>
                  {expandedId === run.id && (
                    <TableRow>
                      <TableCell colSpan={8} className="p-0 border-0">
                        <RunDetailPanel run={run} />
                      </TableCell>
                    </TableRow>
                  )}
                </React.Fragment>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <ConfirmDialog
        open={confirmPurge}
        onOpenChange={setConfirmPurge}
        title="Purge old runs?"
        description="All processing runs older than 30 days will be permanently deleted. This cannot be undone."
        confirmLabel="Purge"
        loading={purging}
        onConfirm={handlePurge}
      />

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-3">
          <Button
            variant="outline" size="sm"
            disabled={page === 0}
            onClick={() => setPage((p) => Math.max(0, p - 1))}
          >
            <ChevronLeft className="size-3.5" /> Previous
          </Button>
          <span className="text-sm tabular-nums text-muted-foreground">
            Page {page + 1} of {totalPages}
          </span>
          <Button
            variant="outline" size="sm"
            disabled={page >= totalPages - 1}
            onClick={() => setPage((p) => p + 1)}
          >
            Next <ChevronRight className="size-3.5" />
          </Button>
        </div>
      )}
    </div>
  );
}