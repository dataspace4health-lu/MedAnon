import { useState, useCallback } from 'react';
import { toast } from 'sonner';
import { Loader2, Play, FileCode } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { FhirCodeViewer } from '@/components/shared/FhirCodeViewer';
import { DownloadButton } from '@/components/shared/DownloadButton';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useConfig } from '@/context/ConfigContext';
import { processRaw } from '@/api/medanon';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const EXAMPLE_PATIENT = JSON.stringify(
  {
    resourceType: 'Patient',
    id: 'p-001',
    name: [{ family: 'Mustermann', given: ['Max'] }],
    birthDate: '1980-03-15',
    gender: 'male',
    telecom: [{ system: 'phone', value: '+49-30-12345678' }],
  },
  null,
  2,
);

type OutputFormat = 'json' | 'ndjson' | 'xml';

const FORMAT_OPTIONS: { value: OutputFormat; label: string }[] = [
  { value: 'json', label: 'JSON' },
  { value: 'ndjson', label: 'NDJSON' },
  { value: 'xml', label: 'XML' },
];

const MIME_MAP: Record<OutputFormat, string> = {
  json: 'application/json',
  ndjson: 'application/x-ndjson',
  xml: 'application/fhir+xml',
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function ProcessResourcePage() {
  const { configProfile } = useConfig();

  const [input, setInput] = useState('');
  const [output, setOutput] = useState('');
  const [outputFormat, setOutputFormat] = useState<OutputFormat>('json');
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // -- handlers -------------------------------------------------------------

  const handleLoadExample = useCallback(() => {
    setInput(EXAMPLE_PATIENT);
    setOutput('');
    setError(null);
  }, []);

  const handleDeidentify = useCallback(async () => {
    const trimmed = input.trim();
    if (!trimmed) {
      toast.error('Please enter FHIR resource content before processing.');
      return;
    }

    setIsProcessing(true);
    setError(null);
    setOutput('');

    try {
      const result = await processRaw(trimmed, outputFormat, configProfile);

      // Pretty-print JSON output when the format is JSON
      let formatted = result;
      if (outputFormat === 'json') {
        try {
          formatted = JSON.stringify(JSON.parse(result), null, 2);
        } catch {
          // If the response is not parseable JSON, use the raw string
        }
      }

      setOutput(formatted);
      toast.success('Resource de-identified successfully.');
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error('De-identification failed.', { description: message });
    } finally {
      setIsProcessing(false);
    }
  }, [input, outputFormat, configProfile]);

  // -- derived values -------------------------------------------------------

  const language = outputFormat === 'xml' ? 'xml' : 'json';
  const filename = `deid_result.${outputFormat}`;

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Process Resource"
        description="Submit FHIR resources for de-identification processing"
      />

      {/* Two-column layout */}
      <div className="grid grid-cols-1 gap-5 md:grid-cols-2">
        {/* ---- Left column: Input ---- */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between pb-3">
            <CardTitle className="text-base">Input</CardTitle>
            <Button variant="ghost" size="sm" onClick={handleLoadExample} className="text-xs">
              <FileCode className="mr-1.5 h-3.5 w-3.5" />
              Load example
            </Button>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-4">
            <Textarea
              className="flex-1 h-[520px] resize-none font-mono text-xs"
              placeholder="Paste FHIR resource JSON, NDJSON, or XML here…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
            />
            <div className="flex items-center gap-3 border-t pt-3">
              <Select
                value={outputFormat}
                onValueChange={(val) => setOutputFormat(val as OutputFormat)}
              >
                <SelectTrigger className="w-28">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {FORMAT_OPTIONS.map((opt) => (
                    <SelectItem key={opt.value} value={opt.value}>
                      {opt.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                onClick={handleDeidentify}
                disabled={isProcessing || !input.trim()}
                className="flex-1"
              >
                {isProcessing ? (
                  <Loader2 data-icon="inline-start" className="h-4 w-4 animate-spin" />
                ) : (
                  <Play data-icon="inline-start" className="h-4 w-4" />
                )}
                {isProcessing ? 'Processing…' : 'De-identify'}
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* ---- Right column: Output ---- */}
        <Card className="flex flex-col">
          <CardHeader className="flex-row items-center justify-between pb-3">
            <CardTitle className="text-base">Output</CardTitle>
            {output && (
              <DownloadButton
                data={output}
                filename={filename}
                mime={MIME_MAP[outputFormat]}
                label="Download"
              />
            )}
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-4">
            {error ? (
              <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
                <p className="text-sm font-medium text-destructive">Error</p>
                <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
              </div>
            ) : output ? (
              <FhirCodeViewer
                code={output}
                language={language}
                maxHeight="560px"
              />
            ) : (
              <div className="flex h-[560px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed">
                <Play className="size-8 text-muted-foreground/30" />
                <p className="text-sm text-muted-foreground">
                  De-identified output will appear here
                </p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
