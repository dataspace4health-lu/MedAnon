import { useState, useEffect, useCallback, useRef } from 'react';
import { toast } from 'sonner';
import { Play, Loader2, Server, Download, Upload, CheckCircle2, AlertCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Progress } from '@/components/ui/progress';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { PermitPicker } from '@/components/shared/PermitPicker';
import { SourcePicker, DestinationPicker } from '@/components/shared/ConnectorPickers';
import { useSettings } from '@/context/SettingsContext';
import { activeSourceJobParams, activeTargetJobParams } from '@/api/fhirRoute';
import {
  submitBulkExportJob,
  submitBulkImport,
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
  const [permitId, setPermitId] = useState<string | null>(null);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [destinationId, setDestinationId] = useState<string | null>(null);

  // Prefill the source from the app-wide Active Source (Settings). A saved
  // backend source -> source_id; a custom/override server -> server_url.
  const { activeSourceId, activeTargetId, connections } = useSettings();
  useEffect(() => {
    const src = activeSourceJobParams(activeSourceId, connections);
    if (src.source_id) setSourceId(src.source_id);
    else if (src.server_url) setServerUrl(src.server_url);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSourceId]);
  const [job, setJob] = useState<JobResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [importJob, setImportJob] = useState<JobResponse | null>(null);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const importPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => () => stopPolling(), [stopPolling]);
  useEffect(() => () => {
    if (importPollRef.current !== null) clearInterval(importPollRef.current);
  }, []);

  // Recover job from previous session on mount
  useEffect(() => {
    const savedJobId = sessionStorage.getItem("asyncExportJobId");
    if (!savedJobId || job) return;
    let cancelled = false;
    (async () => {
      try {
        const recovered = await getJobStatus(savedJobId);
        if (cancelled) return;
        setJob(recovered);
        if (recovered.status === "pending" || recovered.status === "running") {
          startPolling(recovered.job_id);
        }
      } catch {
        sessionStorage.removeItem("asyncExportJobId");
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
              toast.success('Export job complete, ready to download.');
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
    setSubmitting(true);
    try {
      const submitted = await submitBulkExportJob({
        server_url: serverUrl.trim() || undefined,
        resource_type: resourceType.trim() || undefined,
        config_profile: configProfile,
        permit_id: permitId,
        source_id: sourceId,
        destination_id: destinationId,
      });
      setJob(submitted);
      sessionStorage.setItem("asyncExportJobId", submitted.job_id);
      toast.success('Job queued.', { description: `ID: ${submitted.job_id}` });
      startPolling(submitted.job_id);
    } catch (err) {
      toast.error('Failed to submit job.', {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  }, [serverUrl, resourceType, configProfile, permitId, sourceId, destinationId, startPolling]);

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

  const handleStartUpload = useCallback(async () => {
    if (!job || importJob?.status === "running" || importJob?.status === "pending") return;
    setUploadError(null);
    setImportJob(null);
    try {
      const tgt = activeTargetJobParams(activeTargetId, connections);
      const imp = await submitBulkImport({ job_id: job.job_id, ...tgt });
      setImportJob(imp);
      if (importPollRef.current !== null) clearInterval(importPollRef.current);
      importPollRef.current = setInterval(async () => {
        try {
          const updated = await getJobStatus(imp.job_id);
          setImportJob(updated);
          if (updated.status === 'done' || updated.status === 'error' || updated.status === 'cancelled') {
            clearInterval(importPollRef.current!);
            importPollRef.current = null;
            if (updated.status === 'done') {
              toast.success('Upload complete, resources sent to target server.');
            } else if (updated.status === 'error') {
              toast.error('Upload failed.', { description: updated.error ?? undefined });
            }
          }
        } catch {
          clearInterval(importPollRef.current!);
          importPollRef.current = null;
        }
      }, 2000);
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : 'Failed to start upload');
    }
  }, [job, importJob, activeTargetId, connections]);

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
                FHIR Server URL{' '}
                <span className="font-normal text-muted-foreground">
                  (optional, uses server default if blank)
                </span>
              </label>
              <Input
                placeholder="Leave blank for default (FHIR_SOURCE_URL)"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">
                Resource Type{' '}
                <span className="font-normal text-muted-foreground">
                  (optional, leave blank for system-level export)
                </span>
              </label>
              <Input
                placeholder="Patient"
                value={resourceType}
                onChange={(e) => setResourceType(e.target.value)}
                disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
              />
            </div>
            <SourcePicker
              value={sourceId}
              onChange={setSourceId}
              disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
            />
            <DestinationPicker
              value={destinationId}
              onChange={setDestinationId}
              disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
            />
            <PermitPicker
              value={permitId}
              onChange={setPermitId}
              disabled={submitting || job?.status === 'running' || job?.status === 'pending'}
            />
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
              <div className="flex flex-col gap-3">
                <Button onClick={handleDownload} variant="outline">
                  <Download data-icon="inline-start" className="h-4 w-4" />
                  Download NDJSON result
                </Button>
                <div className="border-t pt-3 flex flex-col gap-2">
                  <Button
                    variant="outline"
                    onClick={handleStartUpload}
                    disabled={importJob?.status === 'running' || importJob?.status === 'pending'}
                  >
                    {(importJob?.status === 'running' || importJob?.status === 'pending') ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <Upload className="h-4 w-4" />
                    )}
                    {(importJob?.status === 'running' || importJob?.status === 'pending')
                      ? 'Uploading…'
                      : importJob?.status === 'done'
                        ? 'Re-upload to Target'
                        : 'Send to Target'}
                  </Button>
                  {/* Progress */}
                  {(importJob?.status === 'running' || importJob?.status === 'pending') && (
                    <div className="flex flex-col gap-1">
                      {(importJob.staged_count ?? 0) > 0 ? (
                        <>
                          <Progress
                            value={Math.round((importJob.processed / importJob.staged_count!) * 100)}
                            className="h-1.5"
                          />
                          <p className="text-[10px] text-muted-foreground tabular-nums text-right">
                            {importJob.processed.toLocaleString()} / {importJob.staged_count!.toLocaleString()} uploaded
                            {' '}({Math.round((importJob.processed / importJob.staged_count!) * 100)}%)
                          </p>
                        </>
                      ) : (
                        <>
                          <Progress value={100} className="h-1.5 [&>div]:animate-pulse" />
                          <p className="text-[10px] text-muted-foreground">Loading resources…</p>
                        </>
                      )}
                    </div>
                  )}
                  {/* Success */}
                  {importJob?.status === 'done' && (
                    <div className="flex items-center gap-2 text-sm">
                      <CheckCircle2 className="h-4 w-4 text-green-600" />
                      <span>
                        Uploaded {importJob.processed.toLocaleString()} of{' '}
                        {(importJob.staged_count ?? importJob.processed).toLocaleString()} resources to target
                      </span>
                    </div>
                  )}
                  {/* Error */}
                  {(importJob?.status === 'error' || uploadError) && (
                    <div className="flex items-center gap-2 text-sm text-destructive">
                      <AlertCircle className="h-4 w-4 shrink-0" />
                      {importJob?.error ?? uploadError ?? 'Upload failed'}
                    </div>
                  )}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
