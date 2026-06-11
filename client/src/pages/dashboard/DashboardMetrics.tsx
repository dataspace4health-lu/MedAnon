import { useDashboardSummary } from '@/hooks/useDashboardSummary';
import { useJobs } from '@/hooks/useJobs';
import { Activity, CheckCircle2, XCircle, Clock, Database, TrendingUp } from 'lucide-react';

interface KpiProps {
  label: string;
  value: string | number;
  sub?: string;
  icon: React.ReactNode;
  gradient: string;
  loading?: boolean;
}

function KpiCard({ label, value, sub, icon, gradient, loading }: KpiProps) {
  return (
    <div className={`relative overflow-hidden rounded-2xl p-5 shadow-sm border border-white/60 dark:border-white/5 bg-card hover-lift ${loading ? 'animate-pulse' : ''}`}>
      {/* gradient blob */}
      <div className={`absolute -right-4 -top-4 size-20 rounded-full opacity-10 blur-2xl ${gradient}`} />
      <div className="flex items-start justify-between relative">
        <div>
          <p className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">{label}</p>
          <p className="mt-2 text-3xl font-black tabular-nums leading-none text-foreground">
            {loading ? <span className="skeleton rounded inline-block w-12 h-8" /> : value}
          </p>
          {sub && <p className="mt-1 text-[11px] text-muted-foreground">{sub}</p>}
        </div>
        <div className={`flex size-10 shrink-0 items-center justify-center rounded-xl ${gradient} shadow-sm`}>
          {icon}
        </div>
      </div>
    </div>
  );
}

export function DashboardMetrics() {
  const { data: summary, isLoading: sLoading } = useDashboardSummary();
  const { data: jobs, isLoading: jLoading } = useJobs({ limit: 200 });

  const running  = jobs?.filter((j) => j.status === 'running').length  ?? 0;
  const queued   = jobs?.filter((j) => j.status === 'pending').length  ?? 0;
  const failed   = jobs?.filter((j) => j.status === 'error').length    ?? 0;

  const stats   = summary?.processing_runs;
  const totalResources = stats?.total_resources ?? 0;
  const totalRuns      = stats?.total_runs      ?? 0;
  const avgMs          = stats?.avg_duration_ms;
  const avgScore       = (stats as Record<string, unknown> | undefined)?.avg_composite as number | undefined;

  const loading = sLoading || jLoading;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
      <KpiCard label="Running"   value={running}
        icon={<Activity  className="size-5 text-white" />}
        gradient="bg-blue-500"   loading={loading}
        sub={running > 0 ? 'active now' : 'idle'}
      />
      <KpiCard label="Queued"    value={queued}
        icon={<Clock     className="size-5 text-white" />}
        gradient="bg-amber-500"  loading={loading}
        sub={queued > 0 ? 'waiting' : 'queue empty'}
      />
      <KpiCard label="Failed"    value={failed}
        icon={<XCircle   className="size-5 text-white" />}
        gradient={failed > 0 ? 'bg-rose-500' : 'bg-slate-400'}  loading={loading}
      />
      <KpiCard label="Total Runs" value={totalRuns.toLocaleString()}
        icon={<CheckCircle2 className="size-5 text-white" />}
        gradient="bg-emerald-500" loading={sLoading}
      />
      <KpiCard label="Resources" value={totalResources >= 1000 ? `${(totalResources / 1000).toFixed(1)}k` : totalResources.toLocaleString()}
        sub={avgMs != null ? `${(avgMs / 1000).toFixed(1)}s avg` : undefined}
        icon={<Database  className="size-5 text-white" />}
        gradient="bg-sky-600" loading={sLoading}
      />
      <KpiCard label="Avg Score" value={avgScore != null ? `${Math.round(avgScore)}%` : '—'}
        icon={<TrendingUp className="size-5 text-white" />}
        gradient="bg-blue-700" loading={sLoading}
        sub="composite"
      />
    </div>
  );
}
