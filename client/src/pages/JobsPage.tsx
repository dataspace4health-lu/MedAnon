import { useState } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { useJobs, useDeadJobs, useJobMutations } from '@/hooks/useJobs';
import { JobDetailDrawer } from './jobs/JobDetailDrawer';
import { PipelineDag } from './jobs/PipelineDag';
import { ConfirmDialog } from '@/components/shared/ConfirmDialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import {
  CheckCircle2, XCircle, Clock, Loader2, AlertCircle,
  StopCircle, RotateCcw, RefreshCw, Skull, ChevronRight,
} from 'lucide-react';
import { toast } from 'sonner';
import type { JobResponse } from '@/api/jobs';

// ── helpers ──────────────────────────────────────────────────────────────────

function rel(iso: string) {
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  return `${Math.floor(m / 60)}h ago`;
}

const STATUS_ICON: Record<string, React.ReactNode> = {
  done:      <CheckCircle2 className="size-4 text-emerald-500 shrink-0" />,
  error:     <XCircle      className="size-4 text-destructive shrink-0" />,
  running:   <Loader2      className="size-4 text-blue-500 shrink-0 animate-spin" />,
  pending:   <Clock        className="size-4 text-amber-500 shrink-0" />,
  cancelled: <AlertCircle  className="size-4 text-muted-foreground shrink-0" />,
};

const STATUS_BADGE: Record<string, string> = {
  done:      'bg-emerald-100 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-400',
  error:     'bg-red-100 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-400',
  running:   'bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-950/40 dark:text-blue-400',
  pending:   'bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-400',
  cancelled: 'bg-muted text-muted-foreground border-border',
};

// ── Job row ───────────────────────────────────────────────────────────────────

function JobRow({ job, onSelect }: { job: JobResponse; onSelect: () => void }) {
  const { cancel, reprocess } = useJobMutations();
  const [confirmCancel, setConfirmCancel] = useState(false);

  const pct = job.staged_count
    ? Math.min(100, Math.round((job.processed / job.staged_count) * 100))
    : job.status === 'done' ? 100 : 0;

  return (
    <>
      <tr
        className="border-b hover:bg-muted/30 cursor-pointer transition-colors group"
        onClick={onSelect}
      >
        {/* Job info */}
        <td className="px-4 py-3.5 w-56">
          <div className="flex items-center gap-2.5 min-w-0">
            {STATUS_ICON[job.status] ?? <Clock className="size-4 text-muted-foreground shrink-0" />}
            <div className="min-w-0">
              <p className="text-sm font-semibold truncate">{job.type}</p>
              <p className="text-xs text-muted-foreground font-mono mt-0.5">{job.job_id.slice(0, 12)}…</p>
            </div>
          </div>
        </td>

        {/* Status */}
        <td className="px-4 py-3.5 w-28">
          <Badge className={`text-xs border whitespace-nowrap ${STATUS_BADGE[job.status] ?? STATUS_BADGE.cancelled}`}>
            {job.status}
          </Badge>
        </td>

        {/* Profile */}
        <td className="px-4 py-3.5 w-40 hidden md:table-cell">
          <p className="text-xs text-muted-foreground truncate max-w-[9rem]" title={job.config_profile}>
            {job.config_profile}
          </p>
        </td>

        {/* Progress */}
        <td className="px-4 py-3.5 w-40 hidden lg:table-cell">
          {job.status === 'running' ? (
            <div className="space-y-1.5">
              <Progress value={pct} className="h-1.5 w-32" />
              <p className="text-xs text-muted-foreground tabular-nums">
                {job.processed.toLocaleString()} / {(job.staged_count ?? '?').toLocaleString()}
              </p>
            </div>
          ) : job.summary?.total_resources != null ? (
            <span className="text-sm tabular-nums font-medium">
              {job.summary.total_resources.toLocaleString()}
              <span className="text-xs text-muted-foreground font-normal ml-1">resources</span>
            </span>
          ) : null}
        </td>

        {/* Pipeline DAG */}
        <td className="px-4 py-3.5 hidden xl:table-cell max-w-[240px]">
          <PipelineDag job={job} />
        </td>

        {/* Updated */}
        <td className="px-4 py-3.5 w-24 text-xs text-muted-foreground tabular-nums whitespace-nowrap">
          {rel(job.updated_at)}
        </td>

        {/* Actions */}
        <td className="px-4 py-3.5 w-20" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            {(job.status === 'pending' || job.status === 'running') && (
              <Button size="icon" variant="ghost" className="size-7" title="Cancel job"
                onClick={() => setConfirmCancel(true)}
              >
                <StopCircle className="size-3.5 text-destructive" />
              </Button>
            )}
            {job.status === 'done' && (
              <Button size="icon" variant="ghost" className="size-7" title="Reprocess"
                disabled={reprocess.isPending}
                onClick={() => {
                  reprocess.mutate({ id: job.job_id }, {
                    onSuccess: () => toast.success(`Reprocess queued for ${job.job_id.slice(0, 8)}`),
                    onError:   (e) => toast.error('Reprocess failed', { description: e.message }),
                  });
                }}
              >
                <RotateCcw className="size-3.5" />
              </Button>
            )}
            <Button size="icon" variant="ghost" className="size-7" title="View detail" onClick={onSelect}>
              <ChevronRight className="size-3.5" />
            </Button>
          </div>
        </td>
      </tr>

      <ConfirmDialog
        open={confirmCancel}
        onOpenChange={setConfirmCancel}
        title="Cancel job?"
        description={`Job ${job.job_id.slice(0, 8)}… (${job.type}) will be cancelled and cannot be resumed.`}
        confirmLabel="Cancel job"
        loading={cancel.isPending}
        onConfirm={() => {
          cancel.mutate(job.job_id, {
            onSuccess: () => { toast.success('Job cancelled'); setConfirmCancel(false); },
            onError:   (e) => { toast.error('Failed to cancel', { description: e.message }); setConfirmCancel(false); },
          });
        }}
      />
    </>
  );
}

// ── Dead letter panel ─────────────────────────────────────────────────────────

function DeadLetterPanel() {
  const { data: dead, isLoading } = useDeadJobs();
  const { requeue } = useJobMutations();
  const [requeueId, setRequeueId] = useState<string | null>(null);

  if (!isLoading && (!dead || dead.length === 0)) return null;

  return (
    <div className="rounded-xl border border-destructive/30 bg-red-50/50 dark:bg-red-950/10 p-4">
      <div className="flex items-center gap-2 mb-3">
        <Skull className="size-4 text-destructive" />
        <h3 className="text-sm font-semibold text-destructive">Dead Letter Queue</h3>
        {dead && <Badge variant="destructive" className="text-[10px]">{dead.length}</Badge>}
        <p className="text-xs text-muted-foreground ml-1">Jobs that failed and exhausted all retries</p>
      </div>
      {isLoading ? (
        <div className="space-y-2">
          {[1,2].map((i) => <div key={i} className="skeleton h-10 rounded-lg" />)}
        </div>
      ) : (
        <div className="space-y-1.5">
          {dead!.map((job) => (
            <div key={job.job_id} className="flex items-center justify-between rounded-lg bg-background border px-3 py-2">
              <div className="flex items-center gap-2 min-w-0">
                <XCircle className="size-3.5 text-destructive shrink-0" />
                <span className="text-sm font-medium truncate">{job.type}</span>
                <span className="text-xs text-muted-foreground font-mono shrink-0">{job.job_id.slice(0, 8)}…</span>
                {job.error && (
                  <span className="text-xs text-muted-foreground truncate max-w-[200px]" title={job.error}>
                    {job.error}
                  </span>
                )}
              </div>
              <Button size="sm" variant="outline" className="h-7 gap-1 text-xs shrink-0 ml-2"
                onClick={() => setRequeueId(job.job_id)}
              >
                <RefreshCw className="size-3" /> Requeue
              </Button>
            </div>
          ))}
        </div>
      )}

      <ConfirmDialog
        open={!!requeueId}
        onOpenChange={(o) => { if (!o) setRequeueId(null); }}
        title="Requeue dead job?"
        description={`Job ${requeueId?.slice(0, 8)}… will be added back to the queue.`}
        confirmLabel="Requeue"
        variant="default"
        loading={requeue.isPending}
        onConfirm={() => {
          if (!requeueId) return;
          requeue.mutate(requeueId, {
            onSuccess: () => { toast.success('Job requeued'); setRequeueId(null); },
            onError:   (e) => { toast.error('Requeue failed', { description: e.message }); setRequeueId(null); },
          });
        }}
      />
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function JobsPage() {
  const [statusFilter, setStatusFilter] = useState('all');
  const [typeFilter,   setTypeFilter]   = useState('all');
  const [openJobId,    setOpenJobId]    = useState<string | null>(null);

  const { data: jobs, isLoading, isError, error, refetch } = useJobs({
    status: statusFilter === 'all' ? undefined : statusFilter,
    type:   typeFilter   === 'all' ? undefined : typeFilter,
    limit:  100,
  });

  // surface fetch errors via toast once
  if (isError) {
    toast.error('Failed to load jobs', { description: (error as Error).message, id: 'jobs-fetch-error' });
  }

  const jobTypes = Array.from(new Set((jobs ?? []).map((j) => j.type))).sort();
  const counts = (jobs ?? []).reduce<Record<string,number>>((acc, j) => {
    acc[j.status] = (acc[j.status] ?? 0) + 1; return acc;
  }, {});

  return (
    <div className="space-y-5">
      <PageHeader
        title="Jobs Monitor"
        description="Live server-side job queue — running, queued, failed and dead-letter."
        actions={
          <Button size="sm" variant="outline" className="gap-1.5" onClick={() => refetch()}>
            <RefreshCw className="size-3.5" /> Refresh
          </Button>
        }
      />

      {/* Status summary pills */}
      <div className="flex flex-wrap gap-2">
        {(['running','pending','done','error','cancelled'] as const).map((s) => (
          <button key={s} onClick={() => setStatusFilter(statusFilter === s ? 'all' : s)}
            className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold transition-colors ${
              statusFilter === s ? STATUS_BADGE[s] : 'bg-background text-muted-foreground hover:bg-muted border-border'
            }`}
          >
            {STATUS_ICON[s]}
            {s}
            {counts[s] != null && <span className="tabular-nums">({counts[s]})</span>}
          </button>
        ))}
      </div>

      <DeadLetterPanel />

      {/* Filter row */}
      <div className="flex flex-wrap gap-3">
        <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v ?? 'all')}>
          <SelectTrigger className="w-36 h-8 text-xs"><SelectValue placeholder="Status" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All statuses</SelectItem>
            {(['running','pending','done','error','cancelled']).map((s) => (
              <SelectItem key={s} value={s}>{s}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={typeFilter} onValueChange={(v) => setTypeFilter(v ?? 'all')}>
          <SelectTrigger className="w-44 h-8 text-xs"><SelectValue placeholder="Type" /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            {jobTypes.map((t) => <SelectItem key={t} value={t}>{t}</SelectItem>)}
          </SelectContent>
        </Select>
        {jobs && (
          <span className="flex items-center text-xs text-muted-foreground">
            {jobs.length} job{jobs.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {/* Table + detail split */}
      <div className="flex gap-4 min-h-0">
        {/* Table */}
        <div className={`flex-1 min-w-0 overflow-hidden rounded-xl border shadow-sm bg-card ${openJobId ? 'hidden lg:block' : ''}`}>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/30 sticky top-0">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground w-56">Job</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground w-28">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground w-40 hidden md:table-cell">Profile</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground w-40 hidden lg:table-cell">Progress</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground hidden xl:table-cell">Pipeline</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-muted-foreground w-24">Updated</th>
                  <th className="px-4 py-3 w-20" />
                </tr>
              </thead>
              <tbody>
                {isLoading
                  ? Array.from({ length: 5 }).map((_, i) => (
                      <tr key={i} className="border-b">
                        <td colSpan={7} className="px-4 py-3">
                          <div className="skeleton h-8 rounded" />
                        </td>
                      </tr>
                    ))
                  : (jobs ?? []).length === 0
                  ? (
                      <tr>
                        <td colSpan={7} className="px-4 py-16 text-center">
                          <p className="text-sm text-muted-foreground">No jobs found matching current filters.</p>
                        </td>
                      </tr>
                    )
                  : (jobs ?? []).map((job) => (
                      <JobRow key={job.job_id} job={job} onSelect={() => setOpenJobId(job.job_id)} />
                    ))
                }
              </tbody>
            </table>
          </div>
        </div>

        {/* Detail drawer */}
        {openJobId && (
          <div className="w-full lg:w-[380px] shrink-0 rounded-xl border shadow-sm bg-card overflow-hidden" style={{ maxHeight: '75vh' }}>
            <JobDetailDrawer jobId={openJobId} onClose={() => setOpenJobId(null)} />
          </div>
        )}
      </div>
    </div>
  );
}
