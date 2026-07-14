import { useState } from 'react';
import { useJobDetail } from '@/hooks/useJobs';
import { useJobMutations } from '@/hooks/useJobs';
import { getJobResult } from '@/api/jobs';
import { PipelineDag } from './PipelineDag';
import { TransformationPassportPanel } from '@/components/shared/TransformationPassportPanel';
import { Progress } from '@/components/ui/progress';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { X, RotateCcw, StopCircle, Download } from 'lucide-react';
import { toast } from 'sonner';

// Job types whose result is a ZIP archive (one member per input file) rather
// than a single NDJSON stream. Keeps the downloaded filename's extension honest.
const ZIP_RESULT_TYPES = new Set(['tabular-batch', 'sql-export']);

const STATUS_COLORS: Record<string, string> = {
  done: 'bg-emerald-100 text-emerald-700 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-400',
  error: 'bg-red-100 text-red-700 border-red-200 dark:bg-red-950/40 dark:text-red-400',
  running: 'bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-950/40 dark:text-blue-400',
  pending: 'bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950/40 dark:text-amber-400',
  cancelled: 'bg-muted text-muted-foreground',
};

interface Props {
  jobId: string;
  onClose: () => void;
}

export function JobDetailDrawer({ jobId, onClose }: Props) {
  const { data: job, isLoading } = useJobDetail(jobId);
  const { cancel, reprocess } = useJobMutations();
  const [downloading, setDownloading] = useState(false);

  async function handleDownload(jid: string, jobType: string) {
    setDownloading(true);
    try {
      const blob = await getJobResult(jid);
      const ext = ZIP_RESULT_TYPES.has(jobType) ? 'zip' : 'ndjson';
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${jid}.${ext}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error('Download failed', {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setDownloading(false);
    }
  }

  if (isLoading || !job) {
    return (
      <div className="flex flex-col h-full">
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h3 className="font-semibold text-sm">Job Detail</h3>
          <button onClick={onClose}><X className="size-4" /></button>
        </div>
        <div className="flex-1 flex items-center justify-center">
          <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
        </div>
      </div>
    );
  }

  const pct = job.staged_count
    ? Math.min(100, Math.round((job.processed / job.staged_count) * 100))
    : job.status === 'done' ? 100 : 0;

  const resourceCounts = job.summary?.resource_type_counts ?? {};
  const duration = job.summary?.duration_sec
    ? `${job.summary.duration_sec.toFixed(1)}s`
    : null;

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-4 py-3 shrink-0">
        <div className="flex items-center gap-2 min-w-0">
          <h3 className="font-semibold text-sm truncate">{job.type}</h3>
          <Badge className={`text-[10px] border ${STATUS_COLORS[job.status] ?? STATUS_COLORS.cancelled}`}>
            {job.status}
          </Badge>
        </div>
        <button onClick={onClose} className="rounded p-1 hover:bg-muted ml-2 shrink-0">
          <X className="size-4" />
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-5 text-sm">
        {/* Pipeline DAG */}
        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Pipeline</p>
          <PipelineDag job={job} />
        </div>

        {/* Progress */}
        {(job.status === 'running' || (job.staged_count != null && job.staged_count > 0)) && (
          <div>
            <div className="flex justify-between mb-1.5 text-xs text-muted-foreground">
              <span>Progress</span>
              <span>{job.processed.toLocaleString()} / {(job.staged_count ?? '?').toLocaleString()}</span>
            </div>
            <Progress value={pct} className="h-2" />
          </div>
        )}

        {/* Meta */}
        <div className="grid grid-cols-2 gap-y-2 gap-x-4">
          <div>
            <p className="text-xs text-muted-foreground">Profile</p>
            <p className="font-medium">{job.config_profile}</p>
          </div>
          {duration && (
            <div>
              <p className="text-xs text-muted-foreground">Duration</p>
              <p className="font-medium">{duration}</p>
            </div>
          )}
          {job.summary?.total_resources != null && (
            <div>
              <p className="text-xs text-muted-foreground">Total Resources</p>
              <p className="font-medium">{job.summary.total_resources.toLocaleString()}</p>
            </div>
          )}
          {job.summary?.error_count != null && job.summary.error_count > 0 && (
            <div>
              <p className="text-xs text-muted-foreground">Errors</p>
              <p className="font-medium text-destructive">{job.summary.error_count}</p>
            </div>
          )}
        </div>

        {/* Resource type breakdown */}
        {Object.keys(resourceCounts).length > 0 && (
          <div>
            <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Resource Types</p>
            <div className="space-y-1.5">
              {Object.entries(resourceCounts)
                .sort(([, a], [, b]) => b - a)
                .map(([type, count]) => (
                  <div key={type} className="flex items-center justify-between rounded-md bg-muted/40 px-3 py-1.5">
                    <span className="font-medium">{type}</span>
                    <span className="tabular-nums text-muted-foreground">{count.toLocaleString()}</span>
                  </div>
                ))}
            </div>
          </div>
        )}

        {/* Privacy-gate block report, structured "what leaked / what to fix".
            Shown instead of the raw error text when the job was blocked by the
            score gate (output deleted, download blocked). */}
        {job.block_report ? (
          <div className="rounded-lg border border-destructive/30 bg-red-50 dark:bg-red-950/20 px-3 py-2.5 space-y-2.5">
            <div className="flex items-center justify-between">
              <p className="text-xs font-semibold text-destructive">
                {job.block_report.critical_pii
                  ? 'Output blocked, PII leak detected'
                  : 'Output blocked, quality gate failed'}
              </p>
              <span className="text-[10px] font-mono text-destructive/70">
                {job.block_report.score}% · Grade {job.block_report.grade}
              </span>
            </div>

            {job.block_report.leaked_fields.length > 0 && (
              <div>
                <p className="text-[11px] font-semibold text-destructive/90 mb-1">
                  Leaked fields (uncovered by any rule)
                </p>
                <ul className="space-y-0.5">
                  {job.block_report.leaked_fields.map((f) => (
                    <li
                      key={f.path}
                      className="flex items-center justify-between text-[11px] text-destructive/80"
                    >
                      <span className="font-mono">{f.path}</span>
                      <span className="tabular-nums text-destructive/60">
                        {f.resource_count} resource{f.resource_count === 1 ? '' : 's'}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {job.block_report.fixes.length > 0 && (
              <div>
                <p className="text-[11px] font-semibold text-destructive/90 mb-1">
                  What to fix
                </p>
                <ol className="list-decimal pl-4 space-y-0.5">
                  {job.block_report.fixes.map((fix, i) => (
                    <li key={i} className="text-[11px] text-destructive/80">{fix}</li>
                  ))}
                </ol>
              </div>
            )}

            <p className="text-[10px] text-destructive/60 pt-1 border-t border-destructive/20">
              The output file was deleted and the download is blocked. Resolve the
              issues above and re-run the job.
            </p>
          </div>
        ) : job.error ? (
          <div className="rounded-lg border border-destructive/30 bg-red-50 dark:bg-red-950/20 px-3 py-2.5">
            <p className="text-xs font-semibold text-destructive mb-1">Error</p>
            <p className="text-xs text-destructive/80 font-mono break-all">{job.error}</p>
          </div>
        ) : null}

        {/* Transformation Passport, the treated-data result (risk-driven export) */}
        {job.transformation_passport && (
          <TransformationPassportPanel passport={job.transformation_passport} />
        )}

        {/* IDs */}
        <div className="text-xs text-muted-foreground space-y-0.5">
          <p>ID: <span className="font-mono">{job.job_id}</span></p>
          <p>Created: {new Date(job.created_at).toLocaleString()}</p>
          <p>Updated: {new Date(job.updated_at).toLocaleString()}</p>
        </div>
      </div>

      {/* Actions */}
      <div className="border-t px-4 py-3 flex gap-2 shrink-0">
        {(job.status === 'pending' || job.status === 'running') && (
          <Button
            size="sm"
            variant="destructive"
            className="gap-1.5"
            disabled={cancel.isPending}
            onClick={() => cancel.mutate(job.job_id, {
              onSuccess: () => toast.success('Job cancelled'),
              onError: (e) => toast.error(e.message),
            })}
          >
            <StopCircle className="size-3.5" />
            Cancel
          </Button>
        )}
        {job.status === 'done' && (
          <Button
            size="sm"
            className="gap-1.5"
            disabled={downloading}
            onClick={() => handleDownload(job.job_id, job.type)}
          >
            <Download className="size-3.5" />
            {downloading ? 'Downloading…' : 'Download result'}
          </Button>
        )}
        {job.status === 'done' && (
          <Button
            size="sm"
            variant="outline"
            className="gap-1.5"
            disabled={reprocess.isPending}
            onClick={() => reprocess.mutate({ id: job.job_id }, {
              onSuccess: () => toast.success('Reprocess queued'),
              onError: (e) => toast.error(e.message),
            })}
          >
            <RotateCcw className="size-3.5" />
            Reprocess
          </Button>
        )}
      </div>
    </div>
  );
}
