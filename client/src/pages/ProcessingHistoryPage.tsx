import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import {
  listProcessingRuns,
  getProcessingRunStats,
  purgeProcessingRuns,
} from '@/api/processingRuns';
import type { ProcessingRun, ProcessingRunStats } from '@/api/processingRuns';
import { ApiError } from '@/api/types';
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
  const [purging, setPurging] = useState(false);

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
    }
    if (statsRes.status === 'fulfilled') {
      setStats(statsRes.value);
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
    if (!confirm('Delete processing runs older than 30 days?')) return;
    setPurging(true);
    try {
      await purgeProcessingRuns(30);
      await refresh(0);
      setPage(0);
    } catch {
      /* ignore */
    }
    setPurging(false);
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
          <StatCard label="Total Runs" value={stats.total_runs} accent="bg-primary/60" />
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
        <Collapsible>
          <CollapsibleTrigger className="flex w-full items-center gap-2 rounded-lg border px-4 py-2.5 text-sm font-medium transition-colors hover:bg-muted/50">
            <ChevronDown className="size-4 shrink-0 transition-transform [[data-panel-open]_&]:rotate-180" />
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
          <Button variant="outline" size="sm" onClick={handlePurge} disabled={purging}>
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
                <TableHead className="w-40">Time</TableHead>
                <TableHead>Endpoint</TableHead>
                <TableHead>Profile</TableHead>
                <TableHead className="text-right">Resources</TableHead>
                <TableHead className="text-right">Errors</TableHead>
                <TableHead className="text-right">Duration</TableHead>
                <TableHead className="text-center">Score</TableHead>
                <TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <React.Fragment key={run.id}>
                  <TableRow
                    className="cursor-pointer hover:bg-muted/30"
                    onClick={() => setExpandedId(expandedId === run.id ? null : run.id)}
                  >
                    <TableCell className="text-xs tabular-nums">{fmtDate(run.created_at)}</TableCell>
                    <TableCell className="font-mono text-xs">{run.endpoint}</TableCell>
                    <TableCell className="text-xs">{run.config_profile}</TableCell>
                    <TableCell className="text-right tabular-nums">{run.resource_count}</TableCell>
                    <TableCell className={cn('text-right tabular-nums', run.error_count > 0 && 'text-red-600 font-semibold')}>
                      {run.error_count}
                    </TableCell>
                    <TableCell className="text-right text-xs tabular-nums">{fmtDuration(run.duration_ms)}</TableCell>
                    <TableCell className="text-center"><ScoreBadge score={run.score} /></TableCell>
                    <TableCell>
                      <ChevronDown className={cn('size-3.5 text-muted-foreground transition-transform', expandedId === run.id && 'rotate-180')} />
                    </TableCell>
                  </TableRow>
                  {expandedId === run.id && (
                    <TableRow>
                      <TableCell colSpan={8} className="bg-muted/20 p-4">
                        <div className="grid gap-4 sm:grid-cols-2">
                          {run.summary && (
                            <div>
                              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Summary</p>
                              <pre className="rounded border bg-background p-3 text-xs overflow-auto max-h-48">
                                {JSON.stringify(run.summary, null, 2)}
                              </pre>
                            </div>
                          )}
                          {run.score && (
                            <div>
                              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Score Details</p>
                              <pre className="rounded border bg-background p-3 text-xs overflow-auto max-h-48">
                                {JSON.stringify(run.score, null, 2)}
                              </pre>
                            </div>
                          )}
                        </div>
                        <p className="mt-2 font-mono text-[10px] text-muted-foreground">
                          ID: {run.id} | Type: {run.input_type}
                        </p>
                      </TableCell>
                    </TableRow>
                  )}
                </React.Fragment>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

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
