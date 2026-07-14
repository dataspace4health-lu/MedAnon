import { useState, useCallback, useMemo } from 'react';
import { toast } from 'sonner';
import {
  Loader2,
  Play,
  FileCode,
  FileText,
  GitCompare,
  ListTree,
  ChevronDown,
  Maximize2,
  Minimize2,
  Wand2,
  Trash2,
  ShieldAlert,
} from 'lucide-react';
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
import {
  stripManifestTag,
  extractManifest,
  type ResourceManifest,
} from '@/lib/piiDetection';

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
type ViewMode = 'diff' | 'output';

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
// Output splitting, separate the clinical data from the transformation manifest
// ---------------------------------------------------------------------------

interface SplitOutput {
  /** De-identified clinical data with the manifest meta.tag removed. */
  cleanData: string;
  /** Per-resource transformation manifest, extracted from meta.tag. */
  manifest: ResourceManifest[];
}

function prettyJson(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/**
 * Normalize a string for line-based diffing. JSON is pretty-printed so it aligns
 * against the (also pretty-printed) de-identified data; NDJSON is pretty-printed
 * per line; XML is passed through verbatim (diff is disabled for XML anyway).
 */
function normalizeForDiff(text: string, format: OutputFormat): string {
  const trimmed = text.trim();
  if (!trimmed) return '';
  if (format === 'ndjson') {
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
  if (format === 'xml') return trimmed;
  return prettyJson(trimmed);
}

/**
 * Split the de-identified output into two artifacts: the clinical data (with the
 * transformation manifest stripped) and the manifest itself. XML output is not
 * parsed client-side, so the manifest view is unavailable for that format.
 */
function splitOutput(output: string, format: OutputFormat): SplitOutput {
  const trimmed = output.trim();
  if (!trimmed || format === 'xml') return { cleanData: output, manifest: [] };
  try {
    if (format === 'ndjson') {
      const resources = trimmed
        .split(/\r?\n/)
        .map((l) => l.trim())
        .filter(Boolean)
        .map((l) => JSON.parse(l) as Record<string, unknown>);
      const clean = resources.map(stripManifestTag);
      return {
        cleanData: clean.map((r) => JSON.stringify(r)).join('\n'),
        manifest: extractManifest(resources),
      };
    }
    // JSON: a single resource or a Bundle.
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    const isBundle =
      parsed.resourceType === 'Bundle' && Array.isArray(parsed.entry);
    if (isBundle) {
      const entries = parsed.entry as Array<{ resource?: Record<string, unknown> }>;
      const resources = entries
        .map((e) => e.resource)
        .filter((r): r is Record<string, unknown> => !!r);
      const cleanEntries = entries.map((e) =>
        e.resource ? { ...e, resource: stripManifestTag(e.resource) } : e,
      );
      return {
        cleanData: JSON.stringify({ ...parsed, entry: cleanEntries }, null, 2),
        manifest: extractManifest(resources),
      };
    }
    return {
      cleanData: JSON.stringify(stripManifestTag(parsed), null, 2),
      manifest: extractManifest([parsed]),
    };
  } catch {
    return { cleanData: output, manifest: [] };
  }
}

// ---------------------------------------------------------------------------
// Manifest table
// ---------------------------------------------------------------------------

function ManifestTable({ manifest }: { manifest: ResourceManifest[] }) {
  const rows = manifest.flatMap((m) =>
    m.entries.map((e) => ({
      resource: m.id ? `${m.resourceType}/${m.id}` : m.resourceType,
      path: (e as { path?: string; match?: string }).path ??
        (e as { match?: string }).match ?? '—',
      action: e.action,
      rule: e.rule,
    })),
  );
  if (rows.length === 0) {
    return (
      <p className="px-4 py-6 text-center text-sm text-muted-foreground">
        No transformation rules fired for this resource
        {manifest.length ? '' : ' (manifest unavailable for XML output)'}.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b text-left text-muted-foreground">
            <th className="px-3 py-2 font-medium">Resource</th>
            <th className="px-3 py-2 font-medium">Path</th>
            <th className="px-3 py-2 font-medium">Action</th>
            <th className="px-3 py-2 font-medium">Rule</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-border/50 last:border-0">
              <td className="px-3 py-1.5 whitespace-nowrap">{r.resource}</td>
              <td className="px-3 py-1.5">{r.path}</td>
              <td className="px-3 py-1.5">
                <span className="rounded bg-primary/10 px-1.5 py-0.5 text-primary">
                  {r.action}
                </span>
              </td>
              <td className="px-3 py-1.5 font-sans text-muted-foreground">{r.rule}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

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
  const [piiLeak, setPiiLeak] = useState<PiiLeakInfo | null>(null);
  const [showManifest, setShowManifest] = useState(true);
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

      let formatted = text;
      if (outputFormat === 'json' && !leak) {
        formatted = prettyJson(text);
      }

      setOutput(formatted);
      setPiiLeak(leak);

      if (leak) {
        toast.error('Output blocked, PII leak detected', {
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

  // Separate the de-identified data from the transformation manifest, the two
  // are distinct artifacts and are shown (and downloaded) separately.
  const { cleanData, manifest } = useMemo(
    () => splitOutput(output, outputFormat),
    [output, outputFormat],
  );
  const manifestText = useMemo(() => JSON.stringify(manifest, null, 2), [manifest]);
  const manifestRuleCount = manifest.reduce((n, m) => n + m.entries.length, 0);

  // Diff inputs: original submission vs. the manifest-stripped de-identified data,
  // so the meta.tag manifest never appears as noise in the comparison.
  const normalizedInput = useMemo(
    () => normalizeForDiff(input, outputFormat),
    [input, outputFormat],
  );
  const normalizedClean = useMemo(
    () => normalizeForDiff(cleanData, outputFormat),
    [cleanData, outputFormat],
  );
  const canDiff = Boolean(output) && outputFormat !== 'xml';

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Process Resource"
        description="Submit FHIR resources for de-identification. Compare original vs. de-identified data as a line-numbered diff, with the transformation manifest shown as a separate artifact."
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
            className="h-[220px] resize-y font-mono text-xs"
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
      {/* PII Leak banner, shown when scoring detects uncovered fields      */}
      {/* ------------------------------------------------------------------ */}
      {piiLeak && (
        <div className="mb-5 rounded-xl border-2 border-destructive bg-destructive/5">
          <div className="flex items-center gap-3 px-5 py-4 border-b border-destructive/20">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-destructive text-white">
              <ShieldAlert className="size-5" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="font-black text-destructive text-base uppercase tracking-wide">
                Output Blocked, PII Leak Detected
              </p>
              <p className="text-sm text-destructive/80 mt-0.5">
                The de-identified output was <strong>not released</strong>.
                Privacy score set to <strong>0%</strong>.
              </p>
            </div>
          </div>
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
              <div className="rounded-lg bg-destructive/10 border border-destructive/30 px-4 py-3">
                <p className="text-xs font-bold text-destructive uppercase tracking-wide">Privacy Score</p>
                <p className="text-3xl font-black tabular-nums text-destructive mt-1">0%</p>
                <p className="text-[11px] text-destructive/70 mt-0.5">Forced to zero, any leak = automatic failure</p>
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
      {/* Result, three separated artifacts                                 */}
      {/* ------------------------------------------------------------------ */}
      {!piiLeak && !output && (
        <Card>
          <CardContent className="flex h-[360px] flex-col items-center justify-center gap-2">
            <Play className="size-8 text-muted-foreground/30" />
            <p className="text-sm text-muted-foreground">
              De-identified output will appear here
            </p>
            <p className="text-xs text-muted-foreground/70">
              Submit a resource above to see a line-numbered diff of input vs. output, plus the transformation manifest.
            </p>
          </CardContent>
        </Card>
      )}

      {error && (
        <Card className="border-destructive/50">
          <CardContent className="py-4">
            <p className="text-sm font-medium text-destructive">Error</p>
            <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
          </CardContent>
        </Card>
      )}

      {!piiLeak && output && (
        <div className="space-y-5">
          {/* Section 1: Original vs De-identified, line-numbered diff (manifest stripped) */}
          <Card className="flex flex-col">
            <CardHeader className="flex-row flex-wrap items-center justify-between gap-3 pb-3">
              <CardTitle className="text-base">De-identified data</CardTitle>
              <div className="flex flex-wrap items-center gap-2">
                {/* View toggle (Diff / Output) */}
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

                {/* Full-view toggle (only meaningful for diff) */}
                {viewMode === 'diff' && canDiff && (
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

                <DownloadButton
                  data={cleanData}
                  filename={`deid_data.${outputFormat}`}
                  mime={MIME_MAP[outputFormat]}
                  label="Download data"
                />
              </div>
            </CardHeader>
            <CardContent>
              {viewMode === 'diff' && canDiff ? (
                <JsonDiffViewer
                  original={normalizedInput}
                  modified={normalizedClean}
                  maxHeight={fullView ? 'none' : '560px'}
                  context={fullView ? 20 : 4}
                  disableGapCompression={fullView}
                  fullHeight={fullView}
                />
              ) : (
                <FhirCodeViewer code={cleanData} language={language} maxHeight="560px" />
              )}
            </CardContent>
          </Card>

          {/* Section 2: Transformation manifest (separate artifact, collapsible) */}
          <Card>
            <CardHeader className="flex-row items-center gap-2 pb-3">
              <button
                type="button"
                onClick={() => setShowManifest((v) => !v)}
                className="flex items-center gap-2"
                aria-expanded={showManifest}
              >
                <ChevronDown
                  className={`size-4 text-muted-foreground transition-transform ${showManifest ? '' : '-rotate-90'}`}
                />
                <ListTree className="size-4 text-muted-foreground" />
                <CardTitle className="text-base">Transformation manifest</CardTitle>
              </button>
              <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">
                {manifestRuleCount} rule{manifestRuleCount === 1 ? '' : 's'}
              </span>
              <div className="ml-auto">
                {manifestRuleCount > 0 && (
                  <DownloadButton
                    data={manifestText}
                    filename="deid_manifest.json"
                    mime="application/json"
                    label="Download manifest"
                  />
                )}
              </div>
            </CardHeader>
            {showManifest && (
              <CardContent>
                <div className="rounded-lg border">
                  <ManifestTable manifest={manifest} />
                </div>
                {outputFormat === 'xml' && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    The manifest view is available for JSON and NDJSON output.
                  </p>
                )}
              </CardContent>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
