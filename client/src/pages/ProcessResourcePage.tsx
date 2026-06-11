import { useState, useCallback, useMemo } from 'react';
import { toast } from 'sonner';
import { Loader2, Play, FileCode, GitCompare, FileText, Maximize2, Minimize2, Wand2, Trash2, ShieldAlert } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { FhirCodeViewer } from '@/components/shared/FhirCodeViewer';
import { JsonDiffViewer } from '@/components/shared/JsonDiffViewer';
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
import { processRaw } from '@/api/processing';
import type { PiiLeakInfo } from '@/api/processing';

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

type ViewMode = 'output' | 'diff';

// Normalize an input string for diffing. JSON is pretty-printed so the diff
// aligns nicely against the (also pretty-printed) backend JSON output. NDJSON
// and XML are passed through verbatim — line-based diff still works.
function normalizeForDiff(text: string, format: OutputFormat): string {
  const trimmed = text.trim();
  if (!trimmed) return '';
  if (format === 'json') {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      return trimmed;
    }
  }
  if (format === 'ndjson') {
    // Re-pretty-print each NDJSON line so the structure can be diffed.
    return trimmed
      .split(/\r?\n/)
      .map((line) => {
        const l = line.trim();
        if (!l) return '';
        try {
          return JSON.stringify(JSON.parse(l), null, 2);
        } catch {
          return l;
        }
      })
      .filter(Boolean)
      .join('\n');
  }
  return trimmed;
}

export default function ProcessResourcePage() {
  const { configProfile } = useConfig();

  const [input, setInput] = useState('');
  const [output, setOutput] = useState('');
  const [outputFormat, setOutputFormat] = useState<OutputFormat>('json');
  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [piiLeak, setPiiLeak] = useState<PiiLeakInfo | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>('diff');
  const [fullView, setFullView] = useState(false);

  // -- handlers -------------------------------------------------------------

  const handleLoadExample = useCallback(() => {
    setInput(EXAMPLE_PATIENT);
    setOutput('');
    setError(null);
  }, []);

  const handleFormatInput = useCallback(() => {
    const trimmed = input.trim();
    if (!trimmed) {
      toast.error('Nothing to format.');
      return;
    }
    if (outputFormat === 'json') {
      try {
        setInput(JSON.stringify(JSON.parse(trimmed), null, 2));
        toast.success('Formatted JSON.');
      } catch (e) {
        toast.error('Invalid JSON', {
          description: e instanceof Error ? e.message : String(e),
        });
      }
      return;
    }
    if (outputFormat === 'ndjson') {
      const lines = trimmed.split(/\r?\n/);
      const out: string[] = [];
      let bad = 0;
      for (const line of lines) {
        const l = line.trim();
        if (!l) continue;
        try {
          out.push(JSON.stringify(JSON.parse(l)));
        } catch {
          bad++;
          out.push(l);
        }
      }
      setInput(out.join('\n'));
      if (bad > 0) {
        toast.warning(`Formatted ${out.length - bad} of ${out.length} NDJSON lines (${bad} invalid).`);
      } else {
        toast.success('Formatted NDJSON.');
      }
      return;
    }
    toast.info('Formatting not supported for XML.');
  }, [input, outputFormat]);

  const handleClearInput = useCallback(() => {
    setInput('');
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
    setPiiLeak(null);

    try {
      const { text, piiLeak: leak } = await processRaw(trimmed, outputFormat, configProfile);

      // Pretty-print JSON output when the format is JSON and not already formatted
      let formatted = text;
      if (outputFormat === 'json' && !leak) {
        try {
          formatted = JSON.stringify(JSON.parse(text), null, 2);
        } catch {
          // Non-JSON response — use raw string
        }
      }

      setOutput(formatted);
      setPiiLeak(leak);

      if (leak) {
        toast.error('Output blocked — PII leak detected', {
          description: `${leak.resources_affected} resource(s) contain uncovered HIPAA-sensitive fields. Output was not released.`,
          duration: 10000,
        });
      } else {
        toast.success('Resource de-identified successfully.');
      }
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

  const normalizedInput = useMemo(
    () => normalizeForDiff(input, outputFormat),
    [input, outputFormat],
  );
  const normalizedOutput = useMemo(
    () => normalizeForDiff(output, outputFormat),
    [output, outputFormat],
  );

  const canDiff = Boolean(output) && outputFormat !== 'xml';

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Process Resource"
        description="Submit FHIR resources for de-identification and compare input vs. output"
      />

      {/* ------------------------------------------------------------------ */}
      {/* Top: Input editor + run controls                                   */}
      {/* ------------------------------------------------------------------ */}
      <Card className="mb-5 flex flex-col">
        <CardHeader className="flex-row items-center justify-between pb-3">
          <CardTitle className="text-base">Input</CardTitle>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              onClick={handleFormatInput}
              disabled={!input.trim() || outputFormat === 'xml'}
              className="text-xs"
              title="Pretty-print the input"
            >
              <Wand2 className="mr-1.5 h-3.5 w-3.5" />
              Format
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={handleClearInput}
              disabled={!input}
              className="text-xs"
              title="Clear input and output"
            >
              <Trash2 className="mr-1.5 h-3.5 w-3.5" />
              Clear
            </Button>
            <Button variant="ghost" size="sm" onClick={handleLoadExample} className="text-xs">
              <FileCode className="mr-1.5 h-3.5 w-3.5" />
              Load example
            </Button>
          </div>
        </CardHeader>
        <CardContent className="flex flex-1 flex-col gap-4">
          <Textarea
            className="h-[260px] resize-y font-mono text-xs"
            placeholder="Paste FHIR resource JSON, NDJSON, or XML here…  (Cmd/Ctrl+Enter to run)"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                e.preventDefault();
                if (!isProcessing && input.trim()) handleDeidentify();
              }
            }}
          />
          <div className="flex flex-wrap items-center gap-3 border-t pt-3">
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
              className="flex-1 min-w-[180px]"
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

      {/* ------------------------------------------------------------------ */}
      {/* PII Leak banner — shown when scoring detects uncovered fields      */}
      {/* ------------------------------------------------------------------ */}
      {piiLeak && (
        <div className="mb-5 rounded-xl border-2 border-destructive bg-destructive/5">
          {/* Header */}
          <div className="flex items-center gap-3 px-5 py-4 border-b border-destructive/20">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-destructive text-white">
              <ShieldAlert className="size-5" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="font-black text-destructive text-base uppercase tracking-wide">
                Output Blocked — PII Leak Detected
              </p>
              <p className="text-sm text-destructive/80 mt-0.5">
                The de-identified output was <strong>not released</strong>.
                Privacy score set to <strong>0%</strong>.
              </p>
            </div>
          </div>

          {/* Detail */}
          <div className="px-5 py-4 space-y-3">
            <p className="text-sm text-destructive/90">{piiLeak.message}</p>

            <div className="grid grid-cols-2 gap-3">
              {piiLeak.identifier_risk_hits > 0 && (
                <div className="rounded-lg bg-destructive/10 border border-destructive/30 px-4 py-3">
                  <p className="text-xs font-bold text-destructive uppercase tracking-wide">Identifier fields exposed</p>
                  <p className="text-3xl font-black tabular-nums text-destructive mt-1">{piiLeak.identifier_risk_hits}</p>
                  <p className="text-[11px] text-destructive/70 mt-0.5">Patient.name · identifier · birthDate · address uncovered</p>
                </div>
              )}
              {piiLeak.text_risk_hits > 0 && (
                <div className="rounded-lg bg-amber-500/10 border border-amber-500/30 px-4 py-3">
                  <p className="text-xs font-bold text-amber-700 dark:text-amber-400 uppercase tracking-wide">Free-text PII found</p>
                  <p className="text-3xl font-black tabular-nums text-amber-700 dark:text-amber-400 mt-1">{piiLeak.text_risk_hits}</p>
                  <p className="text-[11px] text-amber-600/70 mt-0.5">NLP scrubbing rules missing for narrative fields</p>
                </div>
              )}
              {/* Privacy score zero indicator */}
              <div className="rounded-lg bg-destructive/10 border border-destructive/30 px-4 py-3">
                <p className="text-xs font-bold text-destructive uppercase tracking-wide">Privacy Score</p>
                <p className="text-3xl font-black tabular-nums text-destructive mt-1">0%</p>
                <p className="text-[11px] text-destructive/70 mt-0.5">Forced to zero — any leak = automatic failure</p>
              </div>
            </div>

            <div className="rounded-lg bg-muted/60 border px-4 py-3">
              <p className="text-xs font-semibold text-muted-foreground uppercase tracking-wide mb-1">How to fix</p>
              <p className="text-sm text-foreground">{piiLeak.remediation}</p>
            </div>
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Bottom: Result panel — hidden when output is blocked by PII gate  */}
      {/* ------------------------------------------------------------------ */}
      {!piiLeak && <Card className="flex flex-col">
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 pb-3">
          <CardTitle className="text-base">Result</CardTitle>
          <div className="flex flex-wrap items-center gap-2">
            {/* View toggle (Diff / Output) */}
            {output && (
              <div className="inline-flex items-center rounded-md border bg-background p-0.5 text-xs">
                <button
                  type="button"
                  onClick={() => setViewMode('diff')}
                  disabled={!canDiff}
                  className={`inline-flex items-center gap-1.5 rounded px-2.5 py-1 transition disabled:cursor-not-allowed disabled:opacity-50 ${
                    viewMode === 'diff'
                      ? 'bg-primary text-primary-foreground'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                  title={canDiff ? 'Side-by-side diff' : 'Diff not available for XML output'}
                >
                  <GitCompare className="h-3.5 w-3.5" />
                  Diff
                </button>
                <button
                  type="button"
                  onClick={() => setViewMode('output')}
                  className={`inline-flex items-center gap-1.5 rounded px-2.5 py-1 transition ${
                    viewMode === 'output'
                      ? 'bg-primary text-primary-foreground'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                >
                  <FileText className="h-3.5 w-3.5" />
                  Output
                </button>
              </div>
            )}

            {/* Full-view toggle (only meaningful for diff) */}
            {output && viewMode === 'diff' && canDiff && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => setFullView((v) => !v)}
                className="shrink-0"
              >
                {fullView ? (
                  <>
                    <Minimize2 className="mr-1.5 h-3.5 w-3.5" />
                    Compact
                  </>
                ) : (
                  <>
                    <Maximize2 className="mr-1.5 h-3.5 w-3.5" />
                    Full view
                  </>
                )}
              </Button>
            )}

            {output && (
              <DownloadButton
                data={output}
                filename={filename}
                mime={MIME_MAP[outputFormat]}
                label="Download"
              />
            )}
          </div>
        </CardHeader>
        <CardContent className="flex flex-1 flex-col gap-4">
          {error ? (
            <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
              <p className="text-sm font-medium text-destructive">Error</p>
              <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
            </div>
          ) : !output ? (
            <div className="flex h-[420px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed">
              <Play className="size-8 text-muted-foreground/30" />
              <p className="text-sm text-muted-foreground">
                De-identified output will appear here
              </p>
              <p className="text-xs text-muted-foreground/70">
                Submit a resource above to see a side-by-side comparison of input vs. output.
              </p>
            </div>
          ) : viewMode === 'diff' && canDiff ? (
            <JsonDiffViewer
              original={normalizedInput}
              modified={normalizedOutput}
              maxHeight={fullView ? 'none' : '560px'}
              context={fullView ? 20 : 4}
              disableGapCompression={fullView}
              fullHeight={fullView}
            />
          ) : (
            <FhirCodeViewer code={output} language={language} maxHeight="560px" />
          )}
        </CardContent>
      </Card>}
    </div>
  );
}
