import { useMemo } from 'react';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend, ReferenceLine,
} from 'recharts';
import type { ProcessingRun } from '@/api/processingRuns';

export function ScoreTrendChart({ runs, threshold }: { runs: ProcessingRun[]; threshold?: number }) {
  const data = useMemo(() => {
    return [...runs]
      .filter((r) => r.score != null)
      .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime())
      .map((r) => {
        const s = r.score as { avg_composite?: number; avg_utility?: number; avg_quality?: number } | null;
        return {
          date: new Date(r.created_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
          // avg_composite is already 0–100; avg_utility / avg_quality are 0–1
          // module scores → ×100 to plot on the same percentage axis.
          Composite: s?.avg_composite != null ? +s.avg_composite.toFixed(1) : null,
          Utility:   s?.avg_utility   != null ? +(s.avg_utility * 100).toFixed(1) : null,
          Quality:   s?.avg_quality   != null ? +(s.avg_quality * 100).toFixed(1) : null,
        };
      });
  }, [runs]);

  if (!data.length) return (
    <div className="rounded-2xl border bg-card p-6 shadow-sm flex items-center justify-center min-h-[240px]">
      <p className="text-sm text-muted-foreground">No scored runs yet.</p>
    </div>
  );

  return (
    <div className="rounded-2xl border bg-card p-5 shadow-sm">
      <div className="mb-4">
        <h3 className="text-sm font-bold">Score Trend</h3>
        <p className="text-xs text-muted-foreground mt-0.5">Composite · Utility · Quality over time</p>
      </div>
      <ResponsiveContainer width="100%" height={240}>
        <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -16 }}>
          <defs>
            <linearGradient id="gradComposite" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor="#6366f1" stopOpacity={0.3} />
              <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
            </linearGradient>
            <linearGradient id="gradUtility" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%"  stopColor="#8b5cf6" stopOpacity={0.2} />
              <stop offset="95%" stopColor="#8b5cf6" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} axisLine={false} tickLine={false} />
          <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} axisLine={false} tickLine={false} unit="%" />
          <Tooltip
            formatter={(v, n) => [`${Number(v).toFixed(1)}%`, n]}
            contentStyle={{ fontSize: 12, borderRadius: 10, border: '1px solid hsl(var(--border))', background: 'hsl(var(--card))' }}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {threshold != null && (
            <ReferenceLine y={threshold} stroke="#ef4444" strokeDasharray="4 3"
              label={{ value: `Threshold ${threshold}%`, fontSize: 10, fill: '#ef4444', position: 'insideTopRight' }}
            />
          )}
          <Area type="monotone" dataKey="Composite" stroke="#6366f1" strokeWidth={2.5} fill="url(#gradComposite)" dot={false} connectNulls activeDot={{ r: 4 }} />
          <Area type="monotone" dataKey="Utility"   stroke="#8b5cf6" strokeWidth={1.5} fill="url(#gradUtility)"  dot={false} connectNulls activeDot={{ r: 3 }} />
          <Area type="monotone" dataKey="Quality"   stroke="#f59e0b" strokeWidth={1.5} fill="none"              dot={false} connectNulls activeDot={{ r: 3 }} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
