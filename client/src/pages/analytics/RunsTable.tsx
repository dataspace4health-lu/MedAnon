import { useState, useMemo } from 'react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ChevronDown, ChevronUp, ChevronsUpDown } from 'lucide-react';
import type { ProcessingRun } from '@/api/processingRuns';

type SortKey = 'created_at' | 'resource_count' | 'duration_ms' | 'score';
type SortDir = 'asc' | 'desc';

interface RunScore {
  avg_composite?: number;
}

function getScore(run: ProcessingRun): number | null {
  return (run.score as RunScore | null)?.avg_composite ?? null;
}

function ScoreBadge({ value }: { value: number | null }) {
  if (value == null) return <span className="text-xs text-muted-foreground"></span>;
  const color =
    value >= 80 ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-400' :
    value >= 60 ? 'bg-amber-100 text-amber-700 dark:bg-amber-950/40 dark:text-amber-400' :
    'bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-400';
  return <Badge className={`text-[10px] border-0 ${color}`}>{value.toFixed(0)}%</Badge>;
}

function SortIcon({ field, sort }: { field: SortKey; sort: { key: SortKey; dir: SortDir } }) {
  if (sort.key !== field) return <ChevronsUpDown className="size-3 opacity-40" />;
  return sort.dir === 'asc' ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />;
}

export function RunsTable({ runs }: { runs: ProcessingRun[] }) {
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: 'created_at', dir: 'desc' });
  const [page, setPage] = useState(0);
  const PER_PAGE = 20;

  const sorted = useMemo(() => {
    return [...runs].sort((a, b) => {
      let aVal: number, bVal: number;
      if (sort.key === 'created_at') {
        aVal = new Date(a.created_at).getTime();
        bVal = new Date(b.created_at).getTime();
      } else if (sort.key === 'score') {
        aVal = getScore(a) ?? -1;
        bVal = getScore(b) ?? -1;
      } else {
        aVal = (a[sort.key] as number) ?? 0;
        bVal = (b[sort.key] as number) ?? 0;
      }
      return sort.dir === 'asc' ? aVal - bVal : bVal - aVal;
    });
  }, [runs, sort]);

  const page_data = sorted.slice(page * PER_PAGE, (page + 1) * PER_PAGE);
  const totalPages = Math.ceil(sorted.length / PER_PAGE);

  const toggleSort = (key: SortKey) => {
    setSort((prev) => prev.key === key
      ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
      : { key, dir: 'desc' });
    setPage(0);
  };

  function Th({ field, label }: { field: SortKey; label: string }) {
    return (
      <th
        className="px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground cursor-pointer select-none hover:text-foreground transition-colors"
        onClick={() => toggleSort(field)}
      >
        <div className="flex items-center gap-1">
          {label}
          <SortIcon field={field} sort={sort} />
        </div>
      </th>
    );
  }

  return (
    <div className="rounded-xl border bg-card shadow-sm overflow-x-auto">
      <table className="w-full text-sm min-w-[640px]">
          <thead className="border-b bg-muted/30">
            <tr>
              <Th field="created_at" label="Date" />
              <th className="px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">Endpoint</th>
              <th className="px-4 py-2.5 text-left text-xs font-semibold text-muted-foreground">Profile</th>
              <Th field="resource_count" label="Resources" />
              <Th field="duration_ms" label="Duration" />
              <Th field="score" label="Score" />
            </tr>
          </thead>
          <tbody>
            {page_data.length === 0 ? (
              <tr>
                <td colSpan={6} className="px-4 py-12 text-center text-muted-foreground text-sm">
                  No runs yet. Enable scoring with <code className="font-mono text-xs bg-muted px-1 rounded">MEDANON_SCORING_ENABLED=true</code>.
                </td>
              </tr>
            ) : (
              page_data.map((run) => (
                <tr key={run.id} className="border-b hover:bg-muted/30 transition-colors">
                  <td className="px-4 py-3 text-xs text-muted-foreground tabular-nums whitespace-nowrap">
                    {new Date(run.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 max-w-[200px]">
                    <span
                      className="block truncate text-xs font-mono bg-muted px-1.5 py-0.5 rounded"
                      title={run.endpoint}
                    >
                      {run.endpoint}
                    </span>
                  </td>
                  <td className="px-4 py-3 max-w-[120px]">
                    <span className="block truncate text-xs text-muted-foreground" title={run.config_profile}>
                      {run.config_profile}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs tabular-nums">{run.resource_count.toLocaleString()}</td>
                  <td className="px-4 py-3 text-xs tabular-nums text-muted-foreground">
                    {run.duration_ms != null ? `${(run.duration_ms / 1000).toFixed(1)}s` : ''}
                  </td>
                  <td className="px-4 py-3">
                    <ScoreBadge value={getScore(run)} />
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t px-4 py-2">
          <span className="text-xs text-muted-foreground">
            {sorted.length} runs · page {page + 1} of {totalPages}
          </span>
          <div className="flex gap-1">
            <Button size="sm" variant="outline" className="h-7 text-xs" disabled={page === 0} onClick={() => setPage((p) => p - 1)}>
              Prev
            </Button>
            <Button size="sm" variant="outline" className="h-7 text-xs" disabled={page >= totalPages - 1} onClick={() => setPage((p) => p + 1)}>
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
