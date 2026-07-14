import { useState } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { useProcessingRuns, useProcessingRunStats } from '@/hooks/useProcessingRuns';
import { ScoreBreakdownCards } from './analytics/ScoreBreakdownCards';
import { ScoreTrendChart } from './analytics/ScoreTrendChart';
import { ScoreHistogram } from './analytics/ScoreHistogram';
import { ThresholdAlertPanel } from './analytics/ThresholdAlertPanel';
import { RunsTable } from './analytics/RunsTable';
import { Button } from '@/components/ui/button';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { RefreshCw } from 'lucide-react';

function KpiCard({
  label, value, sub, valueClass,
}: {
  label: string; value: string | number; sub?: string; valueClass?: string;
}) {
  return (
    <div className="rounded-xl border bg-card px-4 py-3 shadow-sm">
      <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={`text-2xl font-black tabular-nums mt-1 ${valueClass ?? ''}`}>{value}</p>
      {sub && <p className="text-[11px] text-muted-foreground mt-0.5">{sub}</p>}
    </div>
  );
}

function fmt(n: number): string {
  return n >= 1_000_000
    ? `${(n / 1_000_000).toFixed(1)}M`
    : n >= 1_000
    ? `${(n / 1_000).toFixed(1)}k`
    : String(n);
}

export default function AnalyticsPage() {
  const [profileFilter, setProfileFilter] = useState('all');

  const profileParam = profileFilter !== 'all' ? profileFilter : undefined;

  const { data: runsData, isLoading, refetch, isFetching } = useProcessingRuns({
    config_profile: profileParam,
    limit: 500,
  });
  const { data: stats } = useProcessingRunStats({ config_profile: profileParam });
  // The filtered stats only ever report the selected profile, so the dropdown
  // has to source its options from an unfiltered query, otherwise picking a
  // profile collapses the list to that one profile and you cannot switch away.
  const { data: allStats } = useProcessingRunStats();

  const runs = runsData?.runs ?? [];

  const profiles = Object.keys(allStats?.runs_by_profile ?? {}).sort();
  const avgComposite = stats?.avg_composite;

  const compositeClass =
    avgComposite == null ? '' :
    avgComposite >= 80 ? 'text-emerald-600 dark:text-emerald-400' :
    avgComposite >= 60 ? 'text-amber-600 dark:text-amber-400' :
    'text-red-600 dark:text-red-400';

  return (
    <div className="space-y-6">
      <PageHeader
        title="Analytics Dashboard"
        description="Privacy, utility, and quality scoring trends across all de-identification runs."
        actions={
          <Button size="sm" variant="outline" className="gap-1.5" onClick={() => refetch()} disabled={isFetching}>
            <RefreshCw className={`size-3.5 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        }
      />

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-7">
        <KpiCard
          label="Total Runs"
          value={(stats?.total_runs ?? runsData?.total ?? 0).toLocaleString()}
        />
        <KpiCard
          label="Scored"
          value={(stats?.scored_runs ?? 0).toLocaleString()}
          sub={`of ${(stats?.total_runs ?? 0).toLocaleString()} total`}
        />
        <KpiCard
          label="Blocked"
          value={(stats?.blocked_runs ?? 0).toLocaleString()}
          valueClass={(stats?.blocked_runs ?? 0) > 0 ? 'text-red-600 dark:text-red-400' : ''}
          sub="output gate rejections"
        />
        <KpiCard
          label="Resources"
          value={stats?.total_resources != null ? fmt(stats.total_resources) : ''}
        />
        <KpiCard
          label="Avg Composite"
          value={avgComposite != null ? `${Math.round(avgComposite)}%` : ''}
          valueClass={compositeClass}
          sub="overall score"
        />
        <KpiCard
          label="Avg Privacy"
          sub="residual privacy (1 - risk)"
          value={stats?.avg_privacy != null ? `${Math.round(stats.avg_privacy)}%` : ''}
          valueClass={stats?.avg_privacy != null
            ? stats.avg_privacy >= 80 ? 'text-emerald-600 dark:text-emerald-400'
              : stats.avg_privacy >= 60 ? 'text-amber-600 dark:text-amber-400'
              : 'text-red-600 dark:text-red-400'
            : ''}
        />
        <KpiCard
          label="Avg Utility"
          value={stats?.avg_utility != null ? `${Math.round(stats.avg_utility)}%` : ''}
          valueClass={stats?.avg_utility != null
            ? stats.avg_utility >= 80 ? 'text-emerald-600 dark:text-emerald-400'
              : stats.avg_utility >= 60 ? 'text-amber-600 dark:text-amber-400'
              : 'text-red-600 dark:text-red-400'
            : ''}
        />
      </div>

      {/* Profile filter */}
      <div className="flex items-center gap-3">
        <Select value={profileFilter} onValueChange={(v) => setProfileFilter(v ?? 'all')}>
          <SelectTrigger className="w-48 h-8 text-xs">
            <SelectValue placeholder="All profiles" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All profiles</SelectItem>
            {profiles.map((p) => (
              <SelectItem key={p} value={p}>{p}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">
          {isLoading
            ? 'Loading…'
            : `${runs.length} run${runs.length !== 1 ? 's' : ''}${profileFilter !== 'all' ? ` · ${profileFilter}` : ''}`}
        </span>
      </div>

      {/* Gauges */}
      <ScoreBreakdownCards runs={runs} />

      {/* Trend + alerts */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <ScoreTrendChart runs={runs} threshold={60} />
        </div>
        <ThresholdAlertPanel runs={runs} />
      </div>

      {/* Histogram */}
      <ScoreHistogram runs={runs} />

      {/* Run table */}
      <div>
        <h2 className="mb-3 text-xs font-bold uppercase tracking-widest text-muted-foreground">
          Run History
        </h2>
        <RunsTable runs={runs} />
      </div>
    </div>
  );
}
