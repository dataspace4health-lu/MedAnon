import { useState } from 'react';
import { useProcessingRuns } from '@/hooks/useProcessingRuns';
import { getRunScoreReport } from '@/api/jobsExtra';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { FileText, ChevronDown, ChevronUp, Download, Loader2, ShieldAlert, CheckCircle2, AlertTriangle } from 'lucide-react';
import { toast } from 'sonner';
import type { ProcessingRun } from '@/api/processingRuns';
import { MarkdownReport } from '@/components/shared/MarkdownReport';

interface RunScore {
  avg_composite?: number;
  batch_privacy?: { passed: boolean; risk_score: number; threshold: number } | null;
  avg_utility?: number;
  avg_quality?: number;
  error_count?: number;
  total_scored?: number;
}

function parseScore(r: ProcessingRun): RunScore | null {
  return r.score as RunScore | null;
}

function ScoreBar({ label, value, color }: { label: string; value: number; color: string }) {
  const pct = Math.min(100, Math.max(0, Math.round(value)));
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-semibold tabular-nums">{pct}%</span>
      </div>
      <div className="h-1.5 rounded-full bg-muted overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function RunAuditRow({ run }: { run: ProcessingRun }) {
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<string | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const score = parseScore(run);
  const composite = score?.avg_composite;
  const privacy = score?.batch_privacy;

  const loadReport = async () => {
    if (report || reportError) { setOpen((p) => !p); return; }
    setLoading(true);
    setOpen(true);
    setReportError(null);
    try {
      const r = await getRunScoreReport(run.id);
      setReport(r);
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Audit report unavailable.';
      setReportError(msg);
    } finally {
      setLoading(false);
    }
  };

  const downloadReport = () => {
    if (!report) return;
    const blob = new Blob([report], { type: 'text/markdown' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `audit-report-${run.id.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(url);
    toast.success('Report downloaded');
  };

  const statusColor =
    composite == null              ? 'text-muted-foreground'   :
    composite >= 80                ? 'text-emerald-600 dark:text-emerald-400' :
    composite >= 60                ? 'text-amber-600 dark:text-amber-400'   :
                                     'text-red-600 dark:text-red-400';

  return (
    <div className="rounded-xl border bg-card overflow-hidden shadow-sm hover-card transition-all">
      {/* Header row */}
      <div className="flex items-center gap-3 px-4 py-3">
        {/* Status icon */}
        <div className={`flex size-8 shrink-0 items-center justify-center rounded-lg ${
          privacy?.passed === false ? 'bg-red-100 dark:bg-red-950/30' :
          composite != null && composite >= 80 ? 'bg-emerald-100 dark:bg-emerald-950/30' :
          'bg-muted'
        }`}>
          {privacy?.passed === false
            ? <ShieldAlert className="size-4 text-destructive" />
            : composite != null && composite >= 80
            ? <CheckCircle2 className="size-4 text-emerald-500" />
            : <AlertTriangle className="size-4 text-amber-500" />
          }
        </div>

        {/* Info */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium truncate max-w-[180px]" title={run.endpoint}>{run.endpoint}</span>
            <Badge variant="secondary" className="text-[10px] shrink-0">{run.config_profile}</Badge>
            {privacy?.passed === false && (
              <Badge variant="destructive" className="text-[10px] shrink-0">Privacy FAIL</Badge>
            )}
          </div>
          <div className="flex items-center gap-3 mt-0.5 text-xs text-muted-foreground">
            <span>{new Date(run.created_at).toLocaleString()}</span>
            <span className="text-border">|</span>
            <span>{run.resource_count.toLocaleString()} resources</span>
            {run.error_count > 0 && (
              <>
                <span className="text-border">|</span>
                <span className="text-destructive">{run.error_count} errors</span>
              </>
            )}
          </div>
        </div>

        {/* Score */}
        <div className="text-right shrink-0">
          <p className={`text-xl font-black tabular-nums ${statusColor}`}>
            {composite != null ? `${Math.round(composite)}%` : ''}
          </p>
          <p className="text-[10px] text-muted-foreground">composite</p>
        </div>

        {/* Actions */}
        <div className="flex items-center gap-1.5 shrink-0">
          {report && (
            <Button size="icon" variant="ghost" className="size-7" title="Download report" onClick={downloadReport}>
              <Download className="size-3.5" />
            </Button>
          )}
          {score && (
            <Button size="sm" variant="outline" className="h-7 gap-1 text-xs" onClick={loadReport} disabled={loading}>
              {loading
                ? <Loader2 className="size-3 animate-spin" />
                : open ? <ChevronUp className="size-3" /> : <ChevronDown className="size-3" />
              }
              {open ? 'Hide' : 'Audit'}
            </Button>
          )}
        </div>
      </div>

      {/* Score breakdown bars */}
      {score && (
        <div className="px-4 pb-3 grid grid-cols-3 gap-4">
          {score.avg_composite != null && (
            <ScoreBar label="Composite" value={score.avg_composite} color="bg-primary" />
          )}
          {score.avg_utility != null && (
            <ScoreBar label="Utility" value={(score.avg_utility as number) * 100} color="bg-sky-600" />
          )}
          {score.avg_quality != null && (
            <ScoreBar label="Quality" value={(score.avg_quality as number) * 100} color="bg-amber-500" />
          )}
        </div>
      )}

      {/* Privacy detail */}
      {privacy && (
        <div className={`mx-4 mb-3 rounded-lg border px-3 py-2 text-xs flex items-center gap-4 ${
          privacy.passed
            ? 'border-emerald-200 bg-emerald-50 dark:border-emerald-800/30 dark:bg-emerald-950/20'
            : 'border-red-200 bg-red-50 dark:border-red-800/30 dark:bg-red-950/20'
        }`}>
          <span className="font-semibold">{privacy.passed ? 'Privacy gate PASSED' : 'Privacy gate FAILED'}</span>
          <span className="text-muted-foreground">Risk: <span className="font-mono font-semibold">{privacy.risk_score.toFixed(3)}</span></span>
          <span className="text-muted-foreground">Threshold: <span className="font-mono">{privacy.threshold}</span></span>
        </div>
      )}

      {/* Markdown audit report */}
      {open && !loading && (
        <div className="border-t mx-0">
          <div className="px-4 py-3">
            <div className="flex items-center gap-2 mb-2">
              <FileText className="size-3.5 text-muted-foreground" />
              <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">Audit Report</span>
            </div>
            {reportError ? (
              <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">
                {reportError.includes('404') || reportError.includes('unavailable')
                  ? 'No audit report stored for this run. Re-process with MEDANON_SCORING_ENABLED=true to generate one.'
                  : reportError}
              </div>
            ) : report ? (
              <div className="overflow-auto max-h-80 rounded-lg border bg-muted/50 p-3">
                <MarkdownReport markdown={report} />
              </div>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}

export function AuditReport() {
  const { data, isLoading } = useProcessingRuns({ limit: 100 });
  const runs = data?.runs ?? [];
  const scoredRuns = runs.filter((r) => r.score != null && r.resource_count > 0);
  const failedRuns = scoredRuns.filter((r) => {
    const s = r.score as RunScore | null;
    return s?.batch_privacy?.passed === false ||
           (s?.avg_composite != null && s.avg_composite < 60);
  });

  return (
    <div className="space-y-4">
      {/* Summary bar */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: 'Total Runs', value: runs.length, color: 'border-l-primary' },
          { label: 'Scored',     value: scoredRuns.length, color: 'border-l-emerald-500' },
          { label: 'Need Review',value: failedRuns.length, color: failedRuns.length > 0 ? 'border-l-destructive' : 'border-l-muted' },
        ].map((s) => (
          <div key={s.label} className={`rounded-xl border-l-4 ${s.color} bg-card border border-border px-4 py-3 shadow-sm`}>
            <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">{s.label}</p>
            <p className="text-2xl font-black tabular-nums mt-1">{s.value}</p>
          </div>
        ))}
      </div>

      {/* Runs list */}
      {isLoading ? (
        <div className="space-y-3">
          {[1,2,3].map((i) => <div key={i} className="skeleton h-24 rounded-xl" />)}
        </div>
      ) : scoredRuns.length === 0 ? (
        <div className="rounded-xl border bg-card p-10 text-center">
          <FileText className="size-10 text-muted-foreground/30 mx-auto mb-3" />
          <p className="text-sm font-medium text-muted-foreground">No scored runs yet</p>
          <p className="text-xs text-muted-foreground/60 mt-1">
            Enable scoring with <code className="font-mono bg-muted px-1 rounded">MEDANON_SCORING_ENABLED=true</code>
          </p>
        </div>
      ) : (
        <div className="space-y-3">
          {scoredRuns
            .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
            .map((run) => <RunAuditRow key={run.id} run={run} />)
          }
        </div>
      )}
    </div>
  );
}
