import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { toast } from 'sonner';
import { Play, ChevronDown, X, FileText, Loader2, Server, Download } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { FileUploader } from '@/components/shared/FileUploader';
import { StreamProgress } from '@/components/shared/StreamProgress';
import { ResourceTypeSummary } from '@/components/shared/ResourceTypeSummary';
import { DownloadButton } from '@/components/shared/DownloadButton';
import { FhirCodeViewer } from '@/components/shared/FhirCodeViewer';
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
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useStreamingProcess } from '@/hooks/useStreamingProcess';
import { useConfig } from '@/context/ConfigContext';
import {
  processBatch,
  submitBulkExportJob,
  getJobStatus,
  getJobResult,
  type JobResponse,
} from '@/api/medanon';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const MAX_SIZE = 10 * 1024 * 1024; // 10 MB
const ACCEPTED_EXTENSIONS = ['.ndjson', '.json', '.xml'];
const MAX_VISIBLE_ERRORS = 10;

const CT_MAP: Record<string, string> = {
  '.ndjson': 'application/x-ndjson',
  '.json': 'application/json',
  '.xml': 'application/fhir+xml',
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / Math.pow(1024, i);
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function sanitizeFilename(name: string): string {
  const sanitized = name.replace(/[^a-zA-Z0-9\-_.]/g, '_');
  return `deid_${sanitized}`;
}

function detectFormat(ext: string): string {
  switch (ext) {
    case '.ndjson':
      return 'NDJSON';
    case '.json':
      return 'JSON';
    case '.xml':
      return 'XML';
    default:
      return 'Unknown';
  }
}

function countResources(content: string, ext: string): string {
  switch (ext) {
    case '.ndjson': {
      const lines = content.split('\n').filter((l) => l.trim().length > 0);
      return `${lines.length} resource(s)`;
    }
    case '.json': {
      try {
        const parsed = JSON.parse(content);
        if (parsed.resourceType === 'Bundle' && Array.isArray(parsed.entry)) {
          return `${parsed.entry.length} resource(s) in Bundle`;
        }
        return '1 resource';
      } catch {
        return 'Invalid JSON';
      }
    }
    case '.xml':
      return formatBytes(new TextEncoder().encode(content).byteLength);
    default:
      return 'Unknown';
  }
}

function getPreview(content: string, ext: string): string {
  switch (ext) {
    case '.ndjson': {
      const lines = content.split('\n').filter((l) => l.trim().length > 0);
      return lines
        .slice(0, 3)
        .map((line) => {
          try {
            return JSON.stringify(JSON.parse(line), null, 2);
          } catch {
            return line;
          }
        })
        .join('\n---\n');
    }
    case '.json': {
      try {
        const parsed = JSON.parse(content);
        return JSON.stringify(parsed, null, 2).slice(0, 2000);
      } catch {
        return content.slice(0, 2000);
      }
    }
    case '.xml':
      return content.slice(0, 800);
    default:
      return content.slice(0, 800);
  }
}

// Poll interval for job status (ms)
const JOB_POLL_INTERVAL = 3000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface FileInfo {
  name: string;
  size: number;
  ext: string;
  content: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// AsyncExportPanel — submit a server-side bulk export job
// ---------------------------------------------------------------------------

function AsyncExportPanel({ configProfile }: { configProfile: string }) {
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

// ---------------------------------------------------------------------------
// Main BatchPage component
// ---------------------------------------------------------------------------

export default function BatchPage() {
  const { configProfile } = useConfig();
  const { lines, errors, resourceCounts, isStreaming, progress, start, abort } =
    useStreamingProcess();

  const [fileInfo, setFileInfo] = useState<FileInfo | null>(null);
  const [hasProcessed, setHasProcessed] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);

  // Keep abort ref current so the unmount cleanup always calls the latest abort
  const abortRef = useRef(abort);
  useEffect(() => {
    abortRef.current = abort;
  }, [abort]);

  // Abort any in-flight stream when the component unmounts
  useEffect(() => {
    return () => {
      abortRef.current();
    };
  }, []);

  // -- handlers -------------------------------------------------------------

  const handleFile = useCallback((file: File, content: Uint8Array) => {
    const ext = file.name
      .substring(file.name.lastIndexOf('.'))
      .toLowerCase();
    const decoded = new TextDecoder().decode(content);

    setFileInfo({ name: file.name, size: file.size, ext, content: decoded });
    setHasProcessed(false);
    setPreviewOpen(false);
  }, []);

  const handleFileError = useCallback((message: string) => {
    toast.error('File upload failed', { description: message });
  }, []);

  const handleProcess = useCallback(async () => {
    if (!fileInfo) return;

    const contentType = CT_MAP[fileInfo.ext] ?? 'application/json';
    setHasProcessed(true);

    try {
      const generator = processBatch(
        fileInfo.content,
        contentType,
        configProfile,
      );
      await start(
        generator as AsyncGenerator<Record<string, unknown>>,
      );
      toast.success('Batch processing complete.');
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error('Batch processing failed.', { description: message });
    }
  }, [fileInfo, configProfile, start]);

  const handleClear = useCallback(() => {
    if (isStreaming) {
      abort();
    }
    setFileInfo(null);
    setHasProcessed(false);
    setPreviewOpen(false);
  }, [isStreaming, abort]);

  // -- derived values -------------------------------------------------------

  const resultNdjson = useMemo(() => {
    if (lines.length === 0) return '';
    return lines.map((line) => JSON.stringify(line)).join('\n');
  }, [lines]);

  const downloadFilename = fileInfo
    ? sanitizeFilename(fileInfo.name)
    : 'deid_result.ndjson';

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Batch Processing"
        description="Process multiple FHIR resources in bulk"
      />

      <Tabs defaultValue="file" className="mt-2">
        <TabsList>
          <TabsTrigger value="file">
            <FileText className="mr-1.5 h-4 w-4" />
            File Upload
          </TabsTrigger>
          <TabsTrigger value="server">
            <Server className="mr-1.5 h-4 w-4" />
            Server Export
          </TabsTrigger>
        </TabsList>

        {/* ── Tab 1: file upload ── */}
        <TabsContent value="file" className="mt-6">
          {/* File upload area -- shown only when no file is loaded */}
          {!fileInfo && (
            <FileUploader
              accept={ACCEPTED_EXTENSIONS}
              maxSize={MAX_SIZE}
              onFile={handleFile}
              onError={handleFileError}
            />
          )}

          {/* File info + processing results */}
          {fileInfo && (
            <div className="flex flex-col gap-6">
              {/* ---- File details card ---- */}
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center justify-between">
                    <span className="flex items-center gap-2">
                      <FileText className="h-4 w-4" />
                      File Details
                    </span>
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      onClick={handleClear}
                      disabled={isStreaming}
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </CardTitle>
                </CardHeader>

                <CardContent>
                  {/* Metadata grid */}
                  <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-4">
                    <div>
                      <span className="text-muted-foreground">Name</span>
                      <p className="truncate font-medium" title={fileInfo.name}>
                        {fileInfo.name}
                      </p>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Size</span>
                      <p className="font-medium">{formatBytes(fileInfo.size)}</p>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Format</span>
                      <p className="font-medium">
                        <Badge variant="secondary">
                          {detectFormat(fileInfo.ext)}
                        </Badge>
                      </p>
                    </div>
                    <div>
                      <span className="text-muted-foreground">Resources</span>
                      <p className="font-medium">
                        {countResources(fileInfo.content, fileInfo.ext)}
                      </p>
                    </div>
                  </div>

                  {/* Collapsible preview */}
                  <Collapsible
                    open={previewOpen}
                    onOpenChange={setPreviewOpen}
                    className="mt-4"
                  >
                    <CollapsibleTrigger className="flex items-center gap-1 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
                      <ChevronDown
                        className={`h-4 w-4 transition-transform ${
                          previewOpen ? 'rotate-180' : ''
                        }`}
                      />
                      Preview
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <div className="mt-2">
                        <FhirCodeViewer
                          code={getPreview(fileInfo.content, fileInfo.ext)}
                          language={fileInfo.ext === '.xml' ? 'xml' : 'json'}
                          maxHeight="240px"
                        />
                      </div>
                    </CollapsibleContent>
                  </Collapsible>

                  {/* Action buttons */}
                  {!hasProcessed && (
                    <div className="mt-4">
                      <Button onClick={handleProcess}>
                        <Play data-icon="inline-start" className="h-4 w-4" />
                        Process
                      </Button>
                    </div>
                  )}

                  {isStreaming && (
                    <div className="mt-4">
                      <Button variant="destructive" onClick={abort}>
                        <Loader2
                          data-icon="inline-start"
                          className="h-4 w-4 animate-spin"
                        />
                        Abort
                      </Button>
                    </div>
                  )}
                </CardContent>
              </Card>

              {/* ---- Processing results ---- */}
              {hasProcessed && (
                <div className="flex flex-col gap-6">
                  {/* Stream progress */}
                  <Card>
                    <CardContent>
                      <StreamProgress
                        count={progress}
                        isStreaming={isStreaming}
                        errorCount={errors.length}
                      />
                    </CardContent>
                  </Card>

                  {/* Errors list */}
                  {errors.length > 0 && (
                    <Card>
                      <CardHeader>
                        <CardTitle className="text-destructive">
                          Errors ({errors.length})
                        </CardTitle>
                      </CardHeader>
                      <CardContent>
                        <ul className="flex flex-col gap-2 text-sm">
                          {errors.slice(0, MAX_VISIBLE_ERRORS).map((err, i) => (
                            <li
                              key={i}
                              className="rounded border border-destructive/30 bg-destructive/5 px-3 py-2 text-destructive"
                            >
                              {err}
                            </li>
                          ))}
                        </ul>
                        {errors.length > MAX_VISIBLE_ERRORS && (
                          <p className="mt-2 text-sm text-muted-foreground">
                            ...and {errors.length - MAX_VISIBLE_ERRORS} more
                            error(s)
                          </p>
                        )}
                      </CardContent>
                    </Card>
                  )}

                  {/* Resource type breakdown */}
                  {Object.keys(resourceCounts).length > 0 && (
                    <Card>
                      <CardHeader>
                        <CardTitle>Resource Summary</CardTitle>
                      </CardHeader>
                      <CardContent>
                        <ResourceTypeSummary counts={resourceCounts} />
                      </CardContent>
                    </Card>
                  )}

                  {/* Download button -- shown once streaming finishes */}
                  {!isStreaming && lines.length > 0 && (
                    <Card>
                      <CardContent>
                        <DownloadButton
                          data={resultNdjson}
                          filename={downloadFilename}
                          mime="application/x-ndjson"
                          label="Download NDJSON result"
                        />
                      </CardContent>
                    </Card>
                  )}
                </div>
              )}
            </div>
          )}
        </TabsContent>

        {/* ── Tab 2: async server export ── */}
        <TabsContent value="server" className="mt-6">
          <AsyncExportPanel configProfile={configProfile} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
