import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { toast } from 'sonner';
import { Play, ChevronDown, X, FileText, Loader2, Server, Upload, CheckCircle2 } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { FileUploader } from '@/components/shared/FileUploader';
import { StreamProgress } from '@/components/shared/StreamProgress';
import { ResourceTypeSummary } from '@/components/shared/ResourceTypeSummary';
import { DownloadButton } from '@/components/shared/DownloadButton';
import { FhirCodeViewer } from '@/components/shared/FhirCodeViewer';
import { Button } from '@/components/ui/button';
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
import { buildPiiDetectionMap, buildPiiFromDeidentifiedOnly, buildFieldSummary, stripManifestTag } from '@/lib/piiDetection';
import { extractFieldsDeep } from '@/lib/fhirFields';
import {
  processBatch,
  uploadToTarget,
} from '@/api/medanon';
import { AsyncExportPanel } from './batch/AsyncExportPanel.tsx';
import {
  MAX_SIZE,
  ACCEPTED_EXTENSIONS,
  MAX_VISIBLE_ERRORS,
  CT_MAP,
  formatBytes,
  sanitizeFilename,
  detectFormat,
  countResources,
  getPreview,
} from './batch/batchHelpers.ts';
import type { FileInfo } from './batch/batchHelpers.ts';

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
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<{ uploaded: number; errors: number } | null>(null);

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
    setUploadResult(null);
  }, [isStreaming, abort]);

  const handleUploadToTarget = useCallback(async () => {
    if (uploading || lines.length === 0) return;
    setUploading(true);
    setUploadResult(null);
    try {
      const cleanLines = lines.map(stripManifestTag);
      const result = await uploadToTarget(cleanLines);
      setUploadResult({ uploaded: result.uploaded, errors: result.errors });
      toast.success(`Uploaded ${result.uploaded} resource${result.uploaded !== 1 ? 's' : ''} to target server`);
    } catch (err) {
      setUploadResult({ uploaded: 0, errors: -1 });
      toast.error('Upload to target failed', {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setUploading(false);
    }
  }, [uploading, lines]);

  // -- derived values -------------------------------------------------------

  // Parse original resources from the uploaded file so we can do an accurate
  // field diff (buildPiiDetectionMap) instead of relying on heuristics only.
  const originalResources = useMemo((): Record<string, unknown>[] => {
    if (!fileInfo || !hasProcessed || fileInfo.ext === '.xml') return [];
    try {
      if (fileInfo.ext === '.ndjson') {
        return fileInfo.content
          .split('\n')
          .filter((l) => l.trim())
          .map((l) => JSON.parse(l) as Record<string, unknown>);
      }
      const parsed = JSON.parse(fileInfo.content) as Record<string, unknown>;
      if (parsed.resourceType === 'Bundle' && Array.isArray(parsed.entry)) {
        return (parsed.entry as Array<{ resource?: Record<string, unknown> }>)
          .map((e) => e.resource)
          .filter((r): r is Record<string, unknown> => !!r);
      }
      return [parsed];
    } catch {
      return [];
    }
  }, [fileInfo, hasProcessed]);

  // Strip manifest tags before download so the output file stays clean
  const resultNdjson = useMemo(() => {
    if (lines.length === 0) return '';
    return lines.map((line) => JSON.stringify(stripManifestTag(line))).join('\n');
  }, [lines]);

  const piiData = useMemo(() => {
    if (lines.length === 0) return undefined;
    // Use accurate diff when originals are available and counts align with no errors
    if (originalResources.length > 0 && originalResources.length === lines.length && errors.length === 0) {
      return buildPiiDetectionMap(originalResources, lines);
    }
    return buildPiiFromDeidentifiedOnly(lines);
  }, [lines, originalResources, errors]);

  const fieldSummary = useMemo(() => {
    if (lines.length === 0 || !piiData) return undefined;
    const allFieldCounts: Record<string, Record<string, number>> = {};
    for (const resource of lines) {
      const type = String(resource.resourceType ?? 'Unknown');
      if (!allFieldCounts[type]) allFieldCounts[type] = {};
      for (const { field } of extractFieldsDeep(resource)) {
        allFieldCounts[type][field] = (allFieldCounts[type][field] ?? 0) + 1;
      }
    }
    return buildFieldSummary(allFieldCounts, piiData);
  }, [lines, piiData]);

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
                        <ResourceTypeSummary
                          counts={resourceCounts}
                          piiData={piiData}
                          fieldSummary={fieldSummary}
                        />
                      </CardContent>
                    </Card>
                  )}

                  {/* Download + Send to Target -- shown once streaming finishes */}
                  {!isStreaming && lines.length > 0 && (
                    <Card>
                      <CardContent className="flex flex-col gap-3">
                        <div className="flex items-center gap-2">
                          <DownloadButton
                            data={resultNdjson}
                            filename={downloadFilename}
                            mime="application/x-ndjson"
                            label="Download NDJSON result"
                          />
                          <Button
                            variant="outline"
                            onClick={handleUploadToTarget}
                            disabled={uploading}
                          >
                            {uploading ? (
                              <Loader2 className="size-4 animate-spin" />
                            ) : (
                              <Upload className="size-4" />
                            )}
                            {uploading ? 'Sending…' : 'Send to Target'}
                          </Button>
                        </div>
                        {uploadResult && uploadResult.errors !== -1 && (
                          <div className="flex items-center gap-2 text-sm">
                            <CheckCircle2 className="size-4 text-green-600" />
                            <span>
                              Uploaded {uploadResult.uploaded} resource{uploadResult.uploaded !== 1 ? 's' : ''} to target server
                              {uploadResult.errors > 0 && (
                                <span className="text-destructive"> ({uploadResult.errors} error{uploadResult.errors !== 1 ? 's' : ''})</span>
                              )}
                            </span>
                          </div>
                        )}
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
