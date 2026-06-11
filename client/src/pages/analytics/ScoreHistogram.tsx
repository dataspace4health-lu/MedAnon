import { useMemo } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts';
import type { ProcessingRun } from '@/api/processingRuns';

const BINS = ['0–20', '20–40', '40–60', '60–80', '80–100'];
const BIN_COLORS = ['#ef4444', '#f97316', '#eab308', '#84cc16', '#10b981'];

export function ScoreHistogram({ runs }: { runs: ProcessingRun[] }) {
  const data = useMemo(() => {
    const buckets = [0, 0, 0, 0, 0];
    runs.forEach((r) => {
      const score = (r.score as { avg_composite?: number } | null)?.avg_composite;
      if (score == null) return;
      const idx = Math.min(4, Math.floor(score / 20));
      buckets[idx]++;
    });
    return BINS.map((label, i) => ({ label, count: buckets[i], color: BIN_COLORS[i] }));
  }, [runs]);

  const total = data.reduce((s, d) => s + d.count, 0);

  if (total === 0) {
    return (
      <div className="rounded-xl border bg-card p-6 shadow-sm flex items-center justify-center min-h-[220px]">
        <p className="text-sm text-muted-foreground">No scored runs yet.</p>
      </div>
    );
  }

  return (
    <div className="rounded-xl border bg-card p-5 shadow-sm">
      <h3 className="text-sm font-semibold mb-4">Score Distribution</h3>
      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 4, left: -8 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
          <XAxis dataKey="label" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} allowDecimals={false} />
          <Tooltip
            formatter={(v) => [`${v} runs`, 'Count']}
            contentStyle={{ fontSize: 12, borderRadius: 8 }}
          />
          <Bar dataKey="count" radius={[4, 4, 0, 0]}>
            {data.map((d, i) => (
              <Cell key={i} fill={d.color} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
