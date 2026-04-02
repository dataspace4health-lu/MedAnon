import { useState, useEffect, useCallback, useRef } from 'react';
import { toast } from 'sonner';
import { Play, Loader2, Server, Download } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  submitBulkExportJob,
  getJobStatus,
  getJobResult,
} from '@/api/medanon';
import type { JobResponse } from '@/api/medanon';
import { JOB_POLL_INTERVAL } from './batchHelpers.ts';

// ---------------------------------------------------------------------------
// AsyncExportPanel -- submit a server-side bulk export job
// ---------------------------------------------------------------------------

export function AsyncExportPanel({ configProfile }: { configProfile: string }) {
  const [serverUrl, setServerUrl] = useState('');
  const [resourceType, setResourceType] = useState('');
  const [job, setJob] = useState<JobResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);

  const startPolling = useCallback(
    (jobId: string) => {
      stopPolling();
      pollRef.current = setInterval(async () => {
        try {
          const updated = await getJobStatus(jobId);
          setJob(updated);
          if (updated.status === 'done' || updated.status === 'error') {
            stopPolling();
            if (updated.status === 'done') {
              toast.success('Export job complete — ready to download.');
            } else {
              toast.error('Export job failed.', {
                description: updated.error ?? undefined,
              });
            }
          }
        } catch {
          stopPolling();
        }
      }, JOB_POLL_INTERVAL);
    },
    [stopPolling],
  );

  const handleSubmit = useCallback(async () => {
    if (!serverUrl.trim()) {
      toast.error('Server URL is required.');
      return;
    }
    setSubmitting(true);
    try {
      const submitted = await submitBulkExportJob({
        server_url: serverUrl.trim(),
        resource_type: resourceType.trim() || undefined,
        config_profile: configProfile,
      });
      setJob(submitted);
      toast.success('Job queued.', { description: `ID: ${submitted.job_id}` });
      startPolling(submitted.job_id);
    } catch (err) {
      toast.error('Failed to submit job.', {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  }, [serverUrl, resourceType, configProfile, startPolling]);

  const handleDownload = useCallback(async () => {
    if (!job) return;
    try {
      const blob = await getJobResult(job.job_id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `job_${job.job_id}.ndjson`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast.error('Download failed.', {
        description: err instanceof Error ? err.message : String(err),
      });
    }
  }, [job]);

  const statusColor: Record<string, string> = {
    pending: 'secondary',
    running: 'default',
    done: 'default',
    error: 'destructive',
  };

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Server className="h-4 w-4" />
            Server Export (Async)
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">
            Queue a background job that fetches resources from a FHIR server,
            de-identifies them, and writes the result to a downloadable NDJSON
            file. Large exports run without blocking your browser.
          </p>
          <div className="flex flex-col gap-3">
            <div>
              <label className="mb-1 block text-sm font-medium">
                FHIR Server URL
              </label>
              <Input
                placeholder="http://fhir-server:8080/fhir"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">
                Resource Type{' '}
                <span className="font-normal text-muted-foreground">
                  (optional — leave blank for system-level export)
                </span>
              </label>
              <Input
                placeholder="Patient"
                value={resourceType}
                onChange={(e) => setResourceType(e.target.value)}
                disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
              />
            </div>
          </div>
          <Button
            onClick={handleSubmit}
            disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
          >
            {submitting ? (
              <Loader2 data-icon="inline-start" className="h-4 w-4 animate-spin" />
            ) : (
              <Play data-icon="inline-start" className="h-4 w-4" />
            )}
            {submitting ? 'Submitting…' : 'Start Export'}
          </Button>
        </CardContent>
      </Card>

      {job && (
        <Card>
          <CardHeader>
            <CardTitle>Job Status</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-3">
              <div>
                <span className="text-muted-foreground">Job ID</span>
                <p className="truncate font-mono text-xs">{job.job_id}</p>
              </div>
              <div>
                <span className="text-muted-foreground">Status</span>
                <p>
                  <Badge variant={statusColor[job.status] as 'default' | 'secondary' | 'destructive'}>
                    {job.status}
                    {job.status === 'running' && (
                      <Loader2 className="ml-1 h-3 w-3 animate-spin" />
                    )}
                  </Badge>
                </p>
              </div>
              <div>
                <span className="text-muted-foreground">Updated</span>
                <p className="text-xs">
                  {new Date(job.updated_at).toLocaleTimeString()}
                </p>
              </div>
            </div>
            {job.error && (
              <p className="rounded border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                {job.error}
              </p>
            )}
            {job.status === 'done' && (
              <Button onClick={handleDownload} variant="outline">
                <Download data-icon="inline-start" className="h-4 w-4" />
                Download NDJSON result
              </Button>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
