import { useState } from 'react';
import { AlertTriangle, ShieldAlert } from 'lucide-react';
import { Slider } from '@/components/ui/slider';
import type { ProcessingRun } from '@/api/processingRuns';

const STORAGE_KEY = 'medanon:score_threshold';

function loadThreshold(): number {
  const v = localStorage.getItem(STORAGE_KEY);
  return v != null ? Number(v) : 60;
}

interface RunScore {
  avg_composite?: number;
  batch_privacy?: { passed: boolean } | null;
}

export function ThresholdAlertPanel({ runs }: { runs: ProcessingRun[] }) {
  const [threshold, setThreshold] = useState(loadThreshold);

  const handleChange = (val: number | readonly number[]) => {
    const v = Array.isArray(val) ? (val as number[])[0] : (val as number);
    setThreshold(v);
    localStorage.setItem(STORAGE_KEY, String(v));
  };

  const alerts = runs.filter((r) => {
    const s = r.score as RunScore | null;
    if (!s) return false;
    const compositeBelow = s.avg_composite != null && s.avg_composite < threshold;
    const privacyFailed = s.batch_privacy?.passed === false;
    return compositeBelow || privacyFailed;
  });

  return (
    <div className="rounded-xl border bg-card p-5 shadow-sm space-y-4">
      <div className="flex items-center gap-2">
        <ShieldAlert className="size-4 text-amber-500" />
        <h3 className="text-sm font-semibold">Threshold Alerts</h3>
      </div>

      <div>
        <div className="flex justify-between text-xs text-muted-foreground mb-2">
          <span>Composite score threshold</span>
          <span className="font-semibold tabular-nums">{threshold}%</span>
        </div>
        <Slider
          min={0}
          max={100}
          step={5}
          value={[threshold]}
          onValueChange={handleChange}
          className="w-full"
        />
      </div>

      {alerts.length === 0 ? (
        <p className="text-sm text-emerald-600 dark:text-emerald-400 flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-emerald-500 inline-block" />
          All runs meet the threshold.
        </p>
      ) : (
        <div className="space-y-2">
          <p className="text-xs font-semibold text-destructive flex items-center gap-1.5">
            <AlertTriangle className="size-3.5" />
            {alerts.length} run{alerts.length > 1 ? 's' : ''} below threshold
          </p>
          <div className="max-h-40 overflow-y-auto space-y-1.5">
            {alerts.map((r) => {
              const s = r.score as RunScore | null;
              const composite = s?.avg_composite;
              const privacyFailed = s?.batch_privacy?.passed === false;
              return (
                <div key={r.id} className="flex items-center justify-between rounded-lg bg-red-50 dark:bg-red-950/20 border border-red-200 dark:border-red-900 px-3 py-2 text-xs">
                  <div className="min-w-0">
                    <p className="font-medium truncate">{r.endpoint}</p>
                    <p className="text-muted-foreground">{new Date(r.created_at).toLocaleDateString()}</p>
                  </div>
                  <div className="text-right shrink-0 ml-2">
                    {composite != null && (
                      <p className="font-semibold text-destructive tabular-nums">{composite.toFixed(0)}%</p>
                    )}
                    {privacyFailed && (
                      <p className="text-destructive">Privacy failed</p>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
