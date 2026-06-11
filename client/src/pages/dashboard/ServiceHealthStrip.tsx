import { useDashboardSummary } from '@/hooks/useDashboardSummary';
import { CheckCircle2, XCircle } from 'lucide-react';

const LABELS: Record<string, string> = {
  'app-db': 'App DB', redis: 'Redis', gpas: 'gPAS', nlp: 'NLP',
  fhir_source: 'FHIR Src', fhir_target: 'FHIR Dst', analytics: 'Analytics',
};

export function ServiceHealthStrip() {
  const { data, isLoading } = useDashboardSummary();

  if (isLoading) {
    return (
      <div className="flex gap-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="skeleton h-7 w-20 rounded-full" />
        ))}
      </div>
    );
  }

  const checks = Object.entries(data?.ready?.checks ?? {});
  if (!checks.length) return null;

  const allOk = checks.every(([, s]) => s === 'ok' || s === 'healthy');

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Overall status */}
      <div className={`flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${
        allOk
          ? 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800/40 dark:bg-emerald-950/30 dark:text-emerald-400'
          : 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800/40 dark:bg-rose-950/30 dark:text-rose-400'
      }`}>
        {allOk
          ? <CheckCircle2 className="size-3" />
          : <XCircle className="size-3" />
        }
        {allOk ? 'All systems operational' : 'Degraded'}
      </div>

      {/* Per-service chips */}
      {checks.map(([key, status]) => {
        const ok = status === 'ok' || status === 'healthy';
        return (
          <div
            key={key}
            title={`${LABELS[key] ?? key}: ${status}`}
            className={`flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-medium border transition-colors ${
              ok
                ? 'border-slate-200 bg-white/80 text-slate-600 dark:border-white/8 dark:bg-white/4 dark:text-slate-400'
                : 'border-rose-300 bg-rose-50 text-rose-600 dark:border-rose-800/50 dark:bg-rose-950/30 dark:text-rose-400'
            }`}
          >
            <span className={`size-1.5 rounded-full shrink-0 ${ok ? 'bg-emerald-400' : 'bg-rose-400'}`} />
            {LABELS[key] ?? key}
          </div>
        );
      })}
    </div>
  );
}
