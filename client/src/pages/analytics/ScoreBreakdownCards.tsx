import { useMemo } from 'react';
import { RadialBarChart, RadialBar, ResponsiveContainer } from 'recharts';
import type { ProcessingRun } from '@/api/processingRuns';

interface RunScore {
  avg_composite?: number; avg_utility?: number; avg_quality?: number;
  batch_privacy?: { passed: boolean; risk_score: number } | null;
}

function parse(r: ProcessingRun): RunScore | null {
  return r.score as RunScore | null;
}

/* NTT DATA blue palette, no purple */
const GAUGES = [
  {
    key: 'composite',
    label: 'Composite',
    sub: 'overall',
    color: '#0072bc',
    track: '#0072bc20',
    hint: 'Weighted average of privacy, utility and quality scores across all runs.',
  },
  {
    key: 'privacyPass',
    label: 'Privacy',
    sub: 'pass rate',
    color: '#10b981',
    track: '#10b98120',
    hint: 'Percentage of runs where the privacy gate passed (re-identification risk below threshold).',
  },
  {
    key: 'utility',
    label: 'Utility',
    sub: 'data usability',
    color: '#0099d8',
    track: '#0099d820',
    hint: 'How much useful clinical information is preserved after de-identification. Low values are expected when aggressive redaction is applied.',
  },
  {
    key: 'quality',
    label: 'Quality',
    sub: 'structure',
    color: '#004d80',
    track: '#004d8020',
    hint: 'FHIR structural integrity of the output. Low values indicate many fields were removed or transformed.',
  },
];

function Gauge({
  label, sub, value, color, track, hint,
}: {
  label: string; sub: string; value: number | null;
  color: string; track: string; hint: string;
}) {
  const pct = value != null ? Math.min(100, Math.max(0, Math.round(value))) : null;
  const isEmpty = pct == null;

  const scoreColor = isEmpty
    ? undefined
    : pct >= 80 ? color
    : pct >= 60 ? '#d97706'
    : '#dc2626';

  return (
    <div className="flex flex-col items-center gap-2 rounded-2xl border bg-card p-5 shadow-sm hover-card" title={hint}>
      <div className="relative w-32 h-20">
        <ResponsiveContainer width="100%" height="100%">
          <RadialBarChart
            innerRadius="68%" outerRadius="100%"
            data={[{ value: pct ?? 0 }]}
            startAngle={180} endAngle={0} barSize={10}
          >
            <RadialBar dataKey="value" fill={track} cornerRadius={5} background={{ fill: 'hsl(var(--muted))' }} />
            <RadialBar dataKey="value" fill={isEmpty ? 'hsl(var(--muted-foreground)/30)' : color} cornerRadius={5} />
          </RadialBarChart>
        </ResponsiveContainer>
        <div className="absolute inset-0 flex flex-col items-center justify-end pb-0">
          <span className="text-2xl font-black tabular-nums leading-none" style={{ color: scoreColor }}>
            {isEmpty ? '' : `${pct}%`}
          </span>
        </div>
      </div>
      <div className="text-center">
        <p className="text-sm font-bold text-foreground">{label}</p>
        <p className="text-xs text-muted-foreground">{sub}</p>
      </div>
    </div>
  );
}

export function ScoreBreakdownCards({ runs }: { runs: ProcessingRun[] }) {
  const vals = useMemo(() => {
    const scored = runs.map(parse).filter(Boolean) as RunScore[];
    if (!scored.length) return { composite: null, utility: null, quality: null, privacyPass: null };
    const avg = (arr: number[]) => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;
    const composites = scored.map((s) => s.avg_composite).filter((v): v is number => v != null);
    // Backend contract: avg_composite is already 0–100 (pre-scaled), but
    // avg_utility / avg_quality are 0–1 module scores, multiply by 100 for
    // the percentage gauges (matching AuditReport's *100).  This is the fix for
    // utility/quality always rendering as "1%".
    const utilities  = scored.map((s) => s.avg_utility).filter((v): v is number => v != null);
    const qualities  = scored.map((s) => s.avg_quality).filter((v): v is number => v != null);
    const privacies  = scored.map((s) => s.batch_privacy).filter((p): p is { passed: boolean; risk_score: number } => p != null);
    const utilityAvg = avg(utilities);
    const qualityAvg = avg(qualities);
    return {
      composite:   avg(composites),
      utility:     utilityAvg != null ? utilityAvg * 100 : null,
      quality:     qualityAvg != null ? qualityAvg * 100 : null,
      privacyPass: privacies.length ? (privacies.filter((p) => p.passed).length / privacies.length) * 100 : null,
    };
  }, [runs]);

  const hasLowScore = (vals.utility != null && vals.utility < 20) || (vals.quality != null && vals.quality < 20);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {GAUGES.map((g) => (
          <Gauge
            key={g.key}
            label={g.label}
            sub={g.sub}
            value={vals[g.key as keyof typeof vals]}
            color={g.color}
            track={g.track}
            hint={g.hint}
          />
        ))}
      </div>
      {hasLowScore && (
        <p className="text-xs text-muted-foreground bg-muted/50 rounded-lg px-3 py-2">
          <strong>Low utility/quality scores are expected</strong> when aggressive redaction profiles (e.g. value-masking) are used, they remove many fields by design. Hover each gauge for details.
        </p>
      )}
    </div>
  );
}
