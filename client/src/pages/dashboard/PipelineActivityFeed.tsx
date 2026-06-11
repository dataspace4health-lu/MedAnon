import { Link } from 'react-router-dom';
import { useDashboardSummary } from '@/hooks/useDashboardSummary';
import { useJobs } from '@/hooks/useJobs';
import type { JobSummary } from '@/api/dashboard';
import type { JobResponse } from '@/api/jobs';
import { CheckCircle2, XCircle, Clock, Loader2, AlertCircle, ArrowRight, Activity } from 'lucide-react';

function relativeTime(iso?: string | null): string {
  if (!iso) return '';
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h`;
}

const STATUS_CONFIG: Record<string, { icon: React.ReactNode; dot: string; label: string }> = {
  done:      { icon: <CheckCircle2 className="size-3.5 text-emerald-500" />, dot: 'bg-emerald-400', label: 'text-emerald-600 dark:text-emerald-400' },
  error:     { icon: <XCircle      className="size-3.5 text-rose-500" />,    dot: 'bg-rose-400',    label: 'text-rose-600 dark:text-rose-400' },
  running:   { icon: <Loader2      className="size-3.5 text-blue-500 animate-spin" />, dot: 'bg-blue-400 animate-pulse', label: 'text-blue-600 dark:text-blue-400' },
  pending:   { icon: <Clock        className="size-3.5 text-amber-500" />,   dot: 'bg-amber-400',   label: 'text-amber-600 dark:text-amber-400' },
  cancelled: { icon: <AlertCircle  className="size-3.5 text-slate-400" />,   dot: 'bg-slate-400',   label: 'text-slate-500' },
};

type FeedItem = { id: string; type: string; status: string; time?: string | null; processed?: number; profile?: string };

function FeedRow({ item, isLast }: { item: FeedItem; isLast: boolean }) {
  const cfg = STATUS_CONFIG[item.status] ?? STATUS_CONFIG.cancelled;
  return (
    <div className={`flex items-center gap-3 py-2.5 px-1 rounded-lg hover:bg-muted/50 transition-colors ${!isLast ? 'border-b border-border/40' : ''}`}>
      <div className="relative flex size-7 shrink-0 items-center justify-center rounded-lg bg-muted/60">
        {cfg.icon}
      </div>
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-foreground truncate">
          {item.type.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
        </p>
        <div className="flex items-center gap-1.5 mt-0.5">
          <span className={`text-[10px] font-bold uppercase ${cfg.label}`}>{item.status}</span>
          {item.profile && item.profile !== 'auto' && (
            <>
              <span className="text-muted-foreground/40">·</span>
              <span className="text-[10px] text-muted-foreground truncate">{item.profile}</span>
            </>
          )}
          {item.processed != null && item.processed > 0 && (
            <>
              <span className="text-muted-foreground/40">·</span>
              <span className="text-[10px] text-muted-foreground">{item.processed.toLocaleString()} res</span>
            </>
          )}
        </div>
      </div>
      {item.time && (
        <span className="text-xs text-muted-foreground tabular-nums shrink-0">{relativeTime(item.time)} ago</span>
      )}
    </div>
  );
}

export function PipelineActivityFeed() {
  const { data: summary, isLoading: sLoading } = useDashboardSummary(15);
  const { data: liveJobs, isLoading: jLoading } = useJobs({ limit: 20 });

  const items: FeedItem[] = (() => {
    const m = new Map<string, FeedItem>();
    (liveJobs ?? []).forEach((j: JobResponse) => m.set(j.job_id, { id: j.job_id, type: j.type, status: j.status, time: j.updated_at, processed: j.processed, profile: j.config_profile }));
    (summary?.recent_jobs ?? []).forEach((j: JobSummary) => { if (!m.has(j.id)) m.set(j.id, { id: j.id, type: j.type, status: j.status, time: j.updated_at ?? j.created_at }); });
    return Array.from(m.values()).sort((a, b) => new Date(b.time ?? 0).getTime() - new Date(a.time ?? 0).getTime()).slice(0, 12);
  })();

  return (
    <div className="rounded-2xl border bg-card shadow-sm overflow-hidden">
      <div className="flex items-center justify-between border-b px-5 py-3.5">
        <div>
          <h3 className="text-sm font-bold text-foreground">Pipeline Activity</h3>
          <p className="text-xs text-muted-foreground mt-0.5">Live job feed · refreshes every 5s</p>
        </div>
        <Link to="/jobs" className="flex items-center gap-1 text-xs font-semibold text-blue-600 hover:text-blue-700 transition-colors">
          View all <ArrowRight className="size-3.5" />
        </Link>
      </div>
      <div className="px-4 py-2 max-h-[380px] overflow-y-auto scrollbar-hidden">
        {(sLoading && jLoading) ? (
          <div className="space-y-2 py-2">
            {Array.from({ length: 6 }).map((_, i) => <div key={i} className="skeleton h-12 rounded-lg" />)}
          </div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center py-10 text-center">
            <div className="size-10 rounded-full bg-muted flex items-center justify-center mb-3">
              <Activity className="size-5 text-muted-foreground" />
            </div>
            <p className="text-sm font-medium text-muted-foreground">No activity yet</p>
            <p className="text-xs text-muted-foreground/60 mt-1">Jobs will appear here when started</p>
          </div>
        ) : (
          items.map((item, i) => <FeedRow key={item.id} item={item} isLast={i === items.length - 1} />)
        )}
      </div>
    </div>
  );
}

