import { useState, useCallback, useMemo } from 'react';
import { toast } from 'sonner';
import {
  Loader2,
  Sparkles,
  FileText,
  Info,
  ChevronDown,
  CheckCircle2,
} from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { FileUploader } from '@/components/shared/FileUploader';
import { FhirCodeViewer } from '@/components/shared/FhirCodeViewer';
import { DownloadButton } from '@/components/shared/DownloadButton';
import { Button } from '@/components/ui/button';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Slider } from '@/components/ui/slider';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { generateSynthetic } from '@/api/medanon';
import { MAX_UPLOAD_BYTES } from '@/config/constants';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ACCEPTED_EXTENSIONS = ['.ndjson', '.json', '.xml'];

const CT_MAP: Record<string, string> = {
  '.ndjson': 'application/x-ndjson',
  '.json': 'application/json',
  '.xml': 'application/fhir+xml',
};

const MAX_SEED = 2147483647;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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

function countPatients(content: string, ext: string): string {
  switch (ext) {
    case '.ndjson': {
      const lines = content.split('\n').filter((l) => l.trim().length > 0);
      const patientLines = lines.filter((l) =>
        l.includes('"resourceType":"Patient"') ||
        l.includes('"resourceType": "Patient"'),
      );
      return patientLines.length > 0
        ? `${patientLines.length} Patient resource(s)`
        : `${lines.length} resource(s)`;
    }
    case '.json': {
      try {
        const parsed = JSON.parse(content);
        if (parsed.resourceType === 'Bundle' && Array.isArray(parsed.entry)) {
          const patients = parsed.entry.filter(
            (e: { resource?: { resourceType?: string } }) =>
              e.resource?.resourceType === 'Patient',
          );
          return patients.length > 0
            ? `${patients.length} Patient resource(s) in Bundle`
            : `${parsed.entry.length} resource(s) in Bundle`;
        }
        if (parsed.resourceType === 'Patient') {
          return '1 Patient resource';
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

export default function SyntheticDataPage() {
  // File state
  const [fileInfo, setFileInfo] = useState<FileInfo | null>(null);

  // Generation parameters
  const [count, setCount] = useState(100);
  const [useSeed, setUseSeed] = useState(false);
  const [seed, setSeed] = useState(42);

  // Processing state
  const [generating, setGenerating] = useState(false);

  // Result state
  const [resultBlob, setResultBlob] = useState<Blob | null>(null);
  const [resultText, setResultText] = useState<string | null>(null);

  // UI state
  const [howItWorksOpen, setHowItWorksOpen] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);

  // -- derived values -------------------------------------------------------

  const contentType = fileInfo ? (CT_MAP[fileInfo.ext] ?? 'application/json') : '';

  const previewRecords = useMemo(() => {
    if (!resultText) return [];
    return resultText
      .split('\n')
      .filter((l) => l.trim())
      .slice(0, 3)
      .map((line) => {
        try {
          return JSON.stringify(JSON.parse(line), null, 2);
        } catch {
          return line;
        }
      });
  }, [resultText]);

  const resultLineCount = useMemo(() => {
    if (!resultText) return 0;
    return resultText.split('\n').filter((l) => l.trim()).length;
  }, [resultText]);

  // -- handlers -------------------------------------------------------------

  const handleFile = useCallback(
    (file: File, content: Uint8Array) => {
      const ext = file.name
        .substring(file.name.lastIndexOf('.'))
        .toLowerCase();
      const decoded = new TextDecoder().decode(content);

      setFileInfo({ name: file.name, size: file.size, ext, content: decoded });

      // Clear previous results when a new file is uploaded
      setResultBlob(null);
      setResultText(null);
      setPreviewOpen(false);
    },
    [],
  );

  const handleFileError = useCallback((message: string) => {
    toast.error('File upload failed', { description: message });
  }, []);

  const handleGenerate = async () => {
    if (!fileInfo) return;

    setGenerating(true);
    setResultBlob(null);
    setResultText(null);
    setPreviewOpen(false);

    try {
      const blob = await generateSynthetic(
        fileInfo.content,
        count,
        contentType,
        useSeed ? seed : undefined,
      );
      setResultBlob(blob);
      const text = await blob.text();
      setResultText(text);
      const lines = text.split('\n').filter((l) => l.trim());
      toast.success(`Generated ${lines.length} synthetic patient(s)`);
    } catch (e) {
      toast.error(
        `Generation failed: ${e instanceof Error ? e.message : 'Unknown error'}`,
      );
    } finally {
      setGenerating(false);
    }
  };

  const handleSeedChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const val = parseInt(e.target.value, 10);
      if (!isNaN(val) && val >= 0 && val <= MAX_SEED) {
        setSeed(val);
      } else if (e.target.value === '') {
        setSeed(0);
      }
    },
    [],
  );

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Synthetic Data Generation"
        description="Generate synthetic FHIR Patient resources from de-identified input data."
      />

      {/* Info banner */}
      <Card className="mb-6 border-blue-200 bg-blue-50 dark:border-blue-900 dark:bg-blue-950/30">
        <CardContent className="flex gap-3">
          <Info className="mt-0.5 h-5 w-5 shrink-0 text-blue-600 dark:text-blue-400" />
          <p className="text-sm text-blue-800 dark:text-blue-300">
            Upload de-identified FHIR Patient resources as seed data. The engine
            learns statistical patterns (gender distribution, age ranges, postal
            code frequencies) and generates new synthetic patients that preserve
            population characteristics without any real patient data.
          </p>
        </CardContent>
      </Card>

      {/* How this works collapsible */}
      <Collapsible
        open={howItWorksOpen}
        onOpenChange={setHowItWorksOpen}
        className="mb-6"
      >
        <CollapsibleTrigger className="flex items-center gap-1 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
          <ChevronDown
            className={`h-4 w-4 transition-transform ${
              howItWorksOpen ? 'rotate-180' : ''
            }`}
          />
          How this works
        </CollapsibleTrigger>
        <CollapsibleContent>
          <Card className="mt-3">
            <CardContent className="flex flex-col gap-3 text-sm">
              <ol className="flex flex-col gap-2 pl-5" style={{ listStyleType: 'decimal' }}>
                <li>Upload a de-identified FHIR file containing Patient resources</li>
                <li>Set the number of synthetic patients to generate</li>
                <li>Optionally fix a random seed for reproducible output</li>
                <li>Download the generated synthetic patients as NDJSON</li>
              </ol>
              <p className="text-muted-foreground">
                All synthetic records are automatically tagged with{' '}
                <code className="rounded bg-muted px-1.5 py-0.5 text-xs">
                  meta.tag[code=SYN]
                </code>{' '}
                to clearly identify them as synthetic data.
              </p>
            </CardContent>
          </Card>
        </CollapsibleContent>
      </Collapsible>

      {/* File upload section */}
      {!fileInfo && (
        <FileUploader
          accept={ACCEPTED_EXTENSIONS}
          maxSize={MAX_UPLOAD_BYTES}
          onFile={handleFile}
          onError={handleFileError}
        />
      )}

      {/* File metadata + parameters + results */}
      {fileInfo && (
        <div className="flex flex-col gap-6">
          {/* File metadata card */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <FileText className="h-4 w-4" />
                Uploaded File
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-sm sm:grid-cols-4">
                <div>
                  <span className="text-muted-foreground">Filename</span>
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
                  <span className="text-muted-foreground">Content</span>
                  <p className="font-medium">
                    {countPatients(fileInfo.content, fileInfo.ext)}
                  </p>
                </div>
              </div>

              {/* Change file link */}
              <button
                type="button"
                className="mt-3 text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
                onClick={() => {
                  setFileInfo(null);
                  setResultBlob(null);
                  setResultText(null);
                  setPreviewOpen(false);
                }}
                disabled={generating}
              >
                Upload a different file
              </button>
            </CardContent>
          </Card>

          {/* Generation parameters */}
          <Card>
            <CardHeader>
              <CardTitle>Generation Parameters</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-6">
              {/* Count slider */}
              <div className="flex flex-col gap-3">
                <div className="flex items-center justify-between">
                  <label className="text-sm font-medium">
                    Number of synthetic patients
                  </label>
                  <span className="text-sm font-semibold tabular-nums">
                    {count.toLocaleString()}
                  </span>
                </div>
                <Slider
                  min={1}
                  max={10000}
                  value={[count]}
                  onValueChange={(value) => setCount(Array.isArray(value) ? value[0] : value)}
                />
                <div className="flex justify-between text-xs text-muted-foreground">
                  <span>1</span>
                  <span>10,000</span>
                </div>
              </div>

              {/* Seed checkbox + input */}
              <div className="flex flex-col gap-3">
                <div className="flex items-center gap-2">
                  <Checkbox
                    checked={useSeed}
                    onCheckedChange={(checked: boolean) => setUseSeed(checked)}
                  />
                  <label className="text-sm font-medium">
                    Fix random seed for reproducibility
                  </label>
                </div>
                {useSeed && (
                  <Input
                    type="number"
                    min={0}
                    max={MAX_SEED}
                    value={seed}
                    onChange={handleSeedChange}
                    className="max-w-xs"
                    placeholder="Seed value (0 - 2147483647)"
                  />
                )}
              </div>
            </CardContent>
          </Card>

          {/* Generate button */}
          <Button
            className="w-full"
            size="lg"
            onClick={handleGenerate}
            disabled={!fileInfo || generating}
          >
            {generating ? (
              <Loader2
                data-icon="inline-start"
                className="h-4 w-4 animate-spin"
              />
            ) : (
              <Sparkles data-icon="inline-start" className="h-4 w-4" />
            )}
            {generating ? 'Generating...' : 'Generate Synthetic Data'}
          </Button>

          {/* Results section */}
          {resultBlob && resultText && (
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 text-green-600" />
                  Generation Complete
                </CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                {/* Success summary */}
                <div className="rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-800 dark:border-green-900 dark:bg-green-950/30 dark:text-green-300">
                  Successfully generated{' '}
                  <span className="font-semibold">
                    {resultLineCount.toLocaleString()}
                  </span>{' '}
                  synthetic patient record(s) ({formatBytes(resultBlob.size)})
                </div>

                {/* Download button */}
                <DownloadButton
                  data={resultText}
                  filename="synthetic_patients.ndjson"
                  mime="application/x-ndjson"
                  label="Download NDJSON"
                />

                {/* Preview collapsible */}
                {previewRecords.length > 0 && (
                  <Collapsible
                    open={previewOpen}
                    onOpenChange={setPreviewOpen}
                  >
                    <CollapsibleTrigger className="flex items-center gap-1 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
                      <ChevronDown
                        className={`h-4 w-4 transition-transform ${
                          previewOpen ? 'rotate-180' : ''
                        }`}
                      />
                      Preview first {previewRecords.length} record(s)
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <div className="mt-3 flex flex-col gap-3">
                        {previewRecords.map((record, i) => (
                          <div key={i}>
                            <p className="mb-1 text-xs font-medium text-muted-foreground">
                              Record {i + 1}
                            </p>
                            <FhirCodeViewer
                              code={record}
                              language="json"
                              maxHeight="300px"
                            />
                          </div>
                        ))}
                      </div>
                    </CollapsibleContent>
                  </Collapsible>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
