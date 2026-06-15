import { useState, useCallback, useRef, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import {
  Loader2,
  Play,
  FileCode,
  Trash2,
  Upload,
  Download,
  FileText,
  ShieldCheck,
  Table2,
  Save,
} from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
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
import { createConfig, listConfigs, type ConfigMeta } from '@/api/configs';
import {
  processHl7v2,
  processCda,
  processDicom,
  processTabular,
  inspectTabular,
  submitTabularBatch,
  type TabularFormat,
  type TabularPreview,
  type ColumnRule,
} from '@/api/formats';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

type Format = 'hl7v2' | 'cda' | 'dicom' | 'tabular';

const FORMAT_TABS: { value: Format; label: string; note: string }[] = [
  { value: 'hl7v2', label: 'HL7 v2', note: 'Pipe-delimited messages (PID, NK1, PV1…)' },
  { value: 'cda', label: 'CDA / CCDA', note: 'HL7 v3 clinical documents (recordTarget)' },
  { value: 'dicom', label: 'DICOM', note: 'Imaging objects — patient/study tags' },
  { value: 'tabular', label: 'Tabular', note: 'CSV / Excel / Parquet — by column rule' },
];

const TABULAR_FORMATS: { value: TabularFormat; label: string; accept: string }[] = [
  { value: 'csv', label: 'CSV', accept: '.csv,text/csv' },
  { value: 'xlsx', label: 'Excel (.xlsx)', accept: '.xlsx' },
  { value: 'parquet', label: 'Parquet', accept: '.parquet' },
];

// Per-column action choices for the tabular column-mapper.  Each maps to an
// action + params understood by the backend column: rule dispatcher.
const COLUMN_ACTIONS: { value: string; label: string; toRule: (col: string) => ColumnRule | null }[] = [
  { value: 'none', label: 'Keep', toRule: () => null },
  { value: 'redact', label: 'Redact', toRule: (col) => ({ column: col, action: 'redact' }) },
  {
    value: 'generalize_year',
    label: 'Generalize → year',
    toRule: (col) => ({ column: col, action: 'generalize', params: { strategy: 'date_year' } }),
  },
  {
    value: 'pseudonymize_gpas',
    label: 'Pseudonymize',
    toRule: (col) => ({ column: col, action: 'gpas_pseudonymize' }),
  },
  {
    value: 'tokenize',
    label: 'Tokenize (consistent)',
    toRule: (col) => ({ column: col, action: 'tokenize' }),
  },
  { value: 'cryptohash', label: 'Hash (pseudonym)', toRule: (col) => ({ column: col, action: 'cryptohash' }) },
  { value: 'nlp_scrub', label: 'NLP scrub (free text)', toRule: (col) => ({ column: col, action: 'nlp_scrub' }) },
];

// Short human label for a recommendation value (used in the "Recommended" badge).
const ACTION_LABEL: Record<string, string> = Object.fromEntries(
  COLUMN_ACTIONS.map((a) => [a.value, a.label]),
);

const EXAMPLE_HL7 = [
  'MSH|^~\\&|SENDING|FAC|REC|FAC|20240101||ADT^A01|MSG1|P|2.5',
  'PID|1||MRN12345^^^FAC^MR||DOE^JOHN^A||19800101|M|||123 MAIN ST^^BOSTON^MA^02101||5551234567|||||SSN999||',
].join('\n');

const EXAMPLE_CDA = [
  '<?xml version="1.0"?>',
  '<ClinicalDocument xmlns="urn:hl7-org:v3">',
  '  <recordTarget><patientRole>',
  '    <id root="2.16.840.1.113883.19.5" extension="MRN-12345"/>',
  '    <addr><streetAddressLine>123 Main St</streetAddressLine><city>Boston</city><postalCode>02101</postalCode></addr>',
  '    <patient>',
  '      <name><given>John</given><family>Doe</family></name>',
  '      <birthTime value="19800101"/>',
  '    </patient>',
  '  </patientRole></recordTarget>',
  '  <component><structuredBody/></component>',
  '</ClinicalDocument>',
].join('\n');

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function FormatProcessPage() {
  const { configProfile } = useConfig();
  const [format, setFormat] = useState<Format>('hl7v2');

  // Text-format state (shared by HL7 v2 and CDA — both are text → text)
  const [textInput, setTextInput] = useState('');
  const [textOutput, setTextOutput] = useState('');

  // DICOM state
  const [dicomFile, setDicomFile] = useState<File | null>(null);
  const [dicomResultUrl, setDicomResultUrl] = useState<string | null>(null);
  const [dicomResultName, setDicomResultName] = useState<string>('');
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Tabular state
  const [tabularMode, setTabularMode] = useState<'single' | 'batch'>('single');
  const [tabularFormat, setTabularFormat] = useState<TabularFormat>('csv');
  const [tabularFile, setTabularFile] = useState<File | null>(null);
  const [tabularPreview, setTabularPreview] = useState<TabularPreview | null>(null);
  const [columnActions, setColumnActions] = useState<Record<string, string>>({});
  const [tabularResultUrl, setTabularResultUrl] = useState<string | null>(null);
  const [tabularResultName, setTabularResultName] = useState<string>('');
  const tabularInputRef = useRef<HTMLInputElement | null>(null);

  // Batch mode state
  const [batchFiles, setBatchFiles] = useState<File[]>([]);
  const [batchProfile, setBatchProfile] = useState<string>('');
  const [profiles, setProfiles] = useState<ConfigMeta[]>([]);
  const [submittedJobId, setSubmittedJobId] = useState<string | null>(null);
  const batchInputRef = useRef<HTMLInputElement | null>(null);

  // Load saved profiles for the batch profile picker.
  useEffect(() => {
    if (format === 'tabular' && tabularMode === 'batch' && profiles.length === 0) {
      listConfigs().then(setProfiles).catch(() => {});
    }
  }, [format, tabularMode, profiles.length]);

  const [isProcessing, setIsProcessing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isText = format === 'hl7v2' || format === 'cda';

  // -- text-format (HL7 v2 / CDA) handlers ----------------------------------

  const handleTextDeidentify = useCallback(async () => {
    const trimmed = textInput.trim();
    if (!trimmed) {
      toast.error(`Please paste a ${format === 'cda' ? 'CDA document' : 'HL7 v2 message'} first.`);
      return;
    }
    setIsProcessing(true);
    setError(null);
    setTextOutput('');
    try {
      // Pass the active config profile so the request runs through the full
      // rule engine (the adapter path) rather than the legacy scrubber.
      const { text } =
        format === 'cda'
          ? await processCda(trimmed, configProfile)
          : await processHl7v2(trimmed, configProfile);
      setTextOutput(text);
      toast.success(`${format === 'cda' ? 'CDA document' : 'HL7 v2 message'} de-identified.`);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error('De-identification failed.', { description: message });
    } finally {
      setIsProcessing(false);
    }
  }, [textInput, format, configProfile]);

  // -- DICOM handlers -------------------------------------------------------

  const resetDicomResult = useCallback(() => {
    setDicomResultUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return null;
    });
    setDicomResultName('');
  }, []);

  const handleDicomDeidentify = useCallback(async () => {
    if (!dicomFile) {
      toast.error('Please choose a DICOM file first.');
      return;
    }
    setIsProcessing(true);
    setError(null);
    resetDicomResult();
    try {
      const { blob, filename } = await processDicom(dicomFile, configProfile);
      const url = URL.createObjectURL(blob);
      setDicomResultUrl(url);
      setDicomResultName(filename);
      toast.success('DICOM file de-identified — ready to download.');
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error('DICOM de-identification failed.', { description: message });
    } finally {
      setIsProcessing(false);
    }
  }, [dicomFile, resetDicomResult, configProfile]);

  // -- Tabular handlers -----------------------------------------------------

  const resetTabularResult = useCallback(() => {
    setTabularResultUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return null;
    });
    setTabularResultName('');
  }, []);

  const resetTabular = useCallback(() => {
    setTabularPreview(null);
    setColumnActions({});
    resetTabularResult();
    setError(null);
  }, [resetTabularResult]);

  // Step 1 — inspect the chosen file to preview its columns.
  const handleTabularInspect = useCallback(async () => {
    if (!tabularFile) {
      toast.error('Please choose a file first.');
      return;
    }
    setIsProcessing(true);
    setError(null);
    resetTabularResult();
    try {
      const preview = await inspectTabular(tabularFile, tabularFormat);
      setTabularPreview(preview);
      // Pre-select the backend's recommended action per column (falling back to
      // "Keep").  The user can override any pick before running.
      setColumnActions(
        Object.fromEntries(
          preview.columns.map((c) => [c.name, c.recommended_action ?? 'none']),
        ),
      );
      const recommended = preview.columns.filter(
        (c) => c.recommended_action && c.recommended_action !== 'none',
      ).length;
      toast.success(
        `Found ${preview.columns.length} columns, ${preview.row_count} rows` +
          (recommended ? ` — ${recommended} pre-mapped (review below).` : '.'),
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error('Could not read the file.', { description: message });
    } finally {
      setIsProcessing(false);
    }
  }, [tabularFile, tabularFormat, resetTabularResult]);

  // Build column rules from the current per-column action picks.
  const buildColumnRules = useCallback((): ColumnRule[] => {
    const rules: ColumnRule[] = [];
    for (const [col, choice] of Object.entries(columnActions)) {
      const opt = COLUMN_ACTIONS.find((a) => a.value === choice);
      const rule = opt?.toRule(col);
      if (rule) rules.push(rule);
    }
    return rules;
  }, [columnActions]);

  // Save the current column mapping as a reusable config profile so a batch
  // job (or future runs) can reference it by name.  Column rules are valid
  // config rules (match: "column:<name>"), so this uses the standard
  // POST /v1/configs endpoint.
  const handleSaveProfile = useCallback(async () => {
    const columnRules = buildColumnRules();
    if (columnRules.length === 0) {
      toast.error('Assign an action to at least one column before saving.');
      return;
    }
    const name = window.prompt(
      'Save this column mapping as a profile. Name (letters, digits, - and _ only):',
      'my-csv-map',
    );
    if (!name) return;
    try {
      await createConfig({
        name,
        description: 'Tabular column-mapping profile (created from the column-mapper)',
        rules: columnRules.map((r) => ({
          match: `column:${r.column}`,
          action: r.action,
          ...(r.params ? { params: r.params } : {}),
        })),
      });
      toast.success(`Saved profile "${name}". You can reuse it for batch jobs.`);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error('Could not save profile.', { description: message });
    }
  }, [buildColumnRules]);

  // Step 2 — build inline column rules from the picker and run de-identification.
  const handleTabularDeidentify = useCallback(async () => {
    if (!tabularFile) return;
    const columnRules = buildColumnRules();
    if (columnRules.length === 0) {
      toast.error('Assign an action to at least one column.');
      return;
    }
    setIsProcessing(true);
    setError(null);
    resetTabularResult();
    try {
      const { blob, filename } = await processTabular(tabularFile, tabularFormat, { columnRules });
      const url = URL.createObjectURL(blob);
      setTabularResultUrl(url);
      setTabularResultName(filename);
      toast.success(`De-identified ${columnRules.length} column(s) — ready to download.`);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error('Tabular de-identification failed.', { description: message });
    } finally {
      setIsProcessing(false);
    }
  }, [tabularFile, tabularFormat, buildColumnRules, resetTabularResult]);

  // -- Tabular BATCH handler (many files → async job) -----------------------

  const handleBatchSubmit = useCallback(async () => {
    if (batchFiles.length === 0) {
      toast.error('Choose one or more files first.');
      return;
    }
    if (!batchProfile) {
      toast.error('Choose a saved profile (with column: rules) for the batch.');
      return;
    }
    setIsProcessing(true);
    setError(null);
    setSubmittedJobId(null);
    try {
      const { job_id } = await submitTabularBatch(batchFiles, tabularFormat, batchProfile);
      setSubmittedJobId(job_id);
      toast.success(`Batch job submitted (${batchFiles.length} files). Track it on the Jobs page.`);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      toast.error('Batch submit failed.', { description: message });
    } finally {
      setIsProcessing(false);
    }
  }, [batchFiles, batchProfile, tabularFormat]);

  // -- format switch --------------------------------------------------------

  const switchFormat = useCallback((next: Format) => {
    setFormat(next);
    setError(null);
    setTextOutput('');
  }, []);

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Multi-Format De-identification"
        description="De-identify HL7 v2, CDA, DICOM, and tabular files (CSV / Excel / Parquet) through the same rule engine as FHIR. Output is returned to you — never uploaded to the FHIR target server."
      />

      {/* Format selector */}
      <div className="mb-5 inline-flex items-center rounded-lg border bg-background p-1">
        {FORMAT_TABS.map((tab) => (
          <button
            key={tab.value}
            type="button"
            onClick={() => switchFormat(tab.value)}
            className={`rounded-md px-4 py-1.5 text-sm transition ${
              format === tab.value
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:text-foreground'
            }`}
            title={tab.note}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Invariant notice */}
      <div className="mb-5 flex items-start gap-2 rounded-lg border border-primary/30 bg-primary/5 px-4 py-3">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-primary" />
        <p className="text-sm text-muted-foreground">
          {format === 'hl7v2'
            ? `HL7 v2 PID demographics are mapped to the FHIR engine and de-identified with the "${configProfile}" profile, then written back into the message.`
            : format === 'cda'
              ? `CDA recordTarget demographics are mapped to the FHIR engine and de-identified with the "${configProfile}" profile, then written back into the document.`
              : format === 'dicom'
                ? `DICOM patient/study tags are de-identified with the "${configProfile}" profile; PS3.15 §E.3.1 markers are stamped on the result.`
                : `Tabular files are de-identified by column rule. The "${configProfile}" profile must contain column:<name> rules (e.g. column:patient_name → redact) to transform columns.`}
        </p>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Text-format panel (HL7 v2 / CDA)                                   */}
      {/* ------------------------------------------------------------------ */}
      {isText && (
        <>
          <Card className="mb-5">
            <CardHeader className="flex-row items-center justify-between pb-3">
              <CardTitle className="text-base">
                {format === 'cda' ? 'CDA Document' : 'HL7 v2 Message'}
              </CardTitle>
              <div className="flex items-center gap-1">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setTextInput('');
                    setTextOutput('');
                    setError(null);
                  }}
                  disabled={!textInput}
                  className="text-xs"
                >
                  <Trash2 className="mr-1.5 h-3.5 w-3.5" />
                  Clear
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setTextInput(format === 'cda' ? EXAMPLE_CDA : EXAMPLE_HL7);
                    setTextOutput('');
                    setError(null);
                  }}
                  className="text-xs"
                >
                  <FileCode className="mr-1.5 h-3.5 w-3.5" />
                  Load example
                </Button>
              </div>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <Textarea
                className="h-[200px] resize-y font-mono text-xs"
                placeholder={
                  format === 'cda'
                    ? 'Paste a CDA / CCDA XML document here…'
                    : 'Paste an HL7 v2 message here…  (segments separated by newlines)'
                }
                value={textInput}
                onChange={(e) => setTextInput(e.target.value)}
              />
              <Button
                onClick={handleTextDeidentify}
                disabled={isProcessing || !textInput.trim()}
                className="w-full"
              >
                {isProcessing ? (
                  <Loader2 data-icon="inline-start" className="h-4 w-4 animate-spin" />
                ) : (
                  <Play data-icon="inline-start" className="h-4 w-4" />
                )}
                {isProcessing ? 'Processing…' : 'De-identify'}
              </Button>
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between pb-3">
              <CardTitle className="text-base">De-identified Output</CardTitle>
              {textOutput && (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    const blob = new Blob([textOutput], {
                      type: format === 'cda' ? 'application/xml' : 'text/plain',
                    });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = format === 'cda' ? 'deidentified.cda.xml' : 'deidentified.hl7';
                    a.click();
                    URL.revokeObjectURL(url);
                  }}
                >
                  <Download className="mr-1.5 h-3.5 w-3.5" />
                  Download
                </Button>
              )}
            </CardHeader>
            <CardContent>
              {error ? (
                <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
                  <p className="text-sm font-medium text-destructive">Error</p>
                  <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
                </div>
              ) : textOutput ? (
                <pre className="max-h-[420px] overflow-auto rounded-lg border bg-muted/40 p-4 font-mono text-xs whitespace-pre-wrap">
                  {textOutput}
                </pre>
              ) : (
                <div className="flex h-[240px] flex-col items-center justify-center gap-2 rounded-lg border border-dashed">
                  <FileText className="size-8 text-muted-foreground/30" />
                  <p className="text-sm text-muted-foreground">
                    De-identified {format === 'cda' ? 'CDA' : 'HL7 v2'} output will appear here
                  </p>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* DICOM panel                                                        */}
      {/* ------------------------------------------------------------------ */}
      {format === 'dicom' && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">DICOM File</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <input
              ref={fileInputRef}
              type="file"
              accept=".dcm,application/dicom"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0] ?? null;
                setDicomFile(f);
                resetDicomResult();
                setError(null);
              }}
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              className="flex h-[180px] w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed transition hover:border-primary/60 hover:bg-primary/5"
            >
              <Upload className="size-8 text-muted-foreground/40" />
              <p className="text-sm text-foreground">
                {dicomFile ? dicomFile.name : 'Click to choose a .dcm file'}
              </p>
              {dicomFile && (
                <p className="text-xs text-muted-foreground">
                  {(dicomFile.size / 1024).toFixed(1)} KB
                </p>
              )}
            </button>

            <Button
              onClick={handleDicomDeidentify}
              disabled={isProcessing || !dicomFile}
              className="w-full"
            >
              {isProcessing ? (
                <Loader2 data-icon="inline-start" className="h-4 w-4 animate-spin" />
              ) : (
                <Play data-icon="inline-start" className="h-4 w-4" />
              )}
              {isProcessing ? 'Processing…' : 'De-identify'}
            </Button>

            {error && (
              <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
                <p className="text-sm font-medium text-destructive">Error</p>
                <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
              </div>
            )}

            {dicomResultUrl && (
              <div className="flex items-center justify-between rounded-lg border border-primary/30 bg-primary/5 px-4 py-3">
                <div className="flex items-center gap-2">
                  <ShieldCheck className="size-4 text-primary" />
                  <p className="text-sm">
                    De-identified DICOM ready — <span className="font-medium">{dicomResultName}</span>
                  </p>
                </div>
                <a href={dicomResultUrl} download={dicomResultName}>
                  <Button size="sm">
                    <Download className="mr-1.5 h-3.5 w-3.5" />
                    Download
                  </Button>
                </a>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* ------------------------------------------------------------------ */}
      {/* Tabular panel (CSV / Excel / Parquet) — column-mapping workflow    */}
      {/* ------------------------------------------------------------------ */}
      {format === 'tabular' && (
        <div className="space-y-5">
          {/* Single vs. batch mode toggle */}
          <div className="inline-flex w-fit items-center rounded-lg border bg-background p-1">
            <button
              type="button"
              onClick={() => { setTabularMode('single'); setError(null); }}
              className={`rounded-md px-3 py-1 text-xs transition ${
                tabularMode === 'single' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Single file (map columns)
            </button>
            <button
              type="button"
              onClick={() => { setTabularMode('batch'); setError(null); }}
              className={`rounded-md px-3 py-1 text-xs transition ${
                tabularMode === 'batch' ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              Many files (batch job)
            </button>
          </div>

          {tabularMode === 'single' && (
          <div className="space-y-5">
          {/* Step 1 — choose file + format, then inspect */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">1. Choose a file</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <div className="inline-flex w-fit items-center rounded-lg border bg-background p-1">
                {TABULAR_FORMATS.map((f) => (
                  <button
                    key={f.value}
                    type="button"
                    onClick={() => {
                      setTabularFormat(f.value);
                      setTabularFile(null);
                      resetTabular();
                    }}
                    className={`rounded-md px-3 py-1 text-xs transition ${
                      tabularFormat === f.value
                        ? 'bg-primary text-primary-foreground'
                        : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {f.label}
                  </button>
                ))}
              </div>

              <input
                ref={tabularInputRef}
                type="file"
                accept={TABULAR_FORMATS.find((f) => f.value === tabularFormat)?.accept}
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0] ?? null;
                  setTabularFile(f);
                  resetTabular();
                }}
              />
              <button
                type="button"
                onClick={() => tabularInputRef.current?.click()}
                className="flex h-[160px] w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed transition hover:border-primary/60 hover:bg-primary/5"
              >
                <Upload className="size-8 text-muted-foreground/40" />
                <p className="text-sm text-foreground">
                  {tabularFile ? tabularFile.name : `Click to choose a ${tabularFormat.toUpperCase()} file`}
                </p>
                {tabularFile && (
                  <p className="text-xs text-muted-foreground">
                    {(tabularFile.size / 1024).toFixed(1)} KB
                  </p>
                )}
              </button>

              <Button
                variant="outline"
                onClick={handleTabularInspect}
                disabled={isProcessing || !tabularFile}
                className="w-full"
              >
                {isProcessing && !tabularPreview ? (
                  <Loader2 data-icon="inline-start" className="h-4 w-4 animate-spin" />
                ) : (
                  <Table2 data-icon="inline-start" className="h-4 w-4" />
                )}
                {tabularPreview ? 'Re-read columns' : 'Read columns'}
              </Button>

              {error && !tabularPreview && (
                <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
                  <p className="text-sm font-medium text-destructive">Error</p>
                  <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Step 2 — per-column action mapping */}
          {tabularPreview && (
            <Card>
              <CardHeader className="flex-row items-center justify-between pb-3">
                <CardTitle className="text-base">2. Map columns to actions</CardTitle>
                <span className="text-xs text-muted-foreground">
                  {tabularPreview.columns.length} columns · {tabularPreview.row_count} rows
                </span>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                <div className="overflow-hidden rounded-lg border">
                  <table className="w-full text-sm">
                    <thead className="bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground">
                      <tr>
                        <th className="px-4 py-2 text-left font-medium">Column</th>
                        <th className="px-4 py-2 text-left font-medium">Sample values</th>
                        <th className="px-4 py-2 text-left font-medium">Action</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y">
                      {tabularPreview.columns.map((col) => (
                        <tr key={col.name} className="align-top">
                          <td className="px-4 py-2.5 font-medium text-foreground">{col.name}</td>
                          <td className="px-4 py-2.5">
                            <div className="flex flex-wrap gap-1">
                              {col.samples.length === 0 ? (
                                <span className="text-xs text-muted-foreground/60">—</span>
                              ) : (
                                col.samples.slice(0, 3).map((s, i) => (
                                  <span
                                    key={i}
                                    className="truncate rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground"
                                    style={{ maxWidth: '14rem' }}
                                  >
                                    {s}
                                  </span>
                                ))
                              )}
                            </div>
                          </td>
                          <td className="px-4 py-2 w-52">
                            <Select
                              value={columnActions[col.name] ?? 'none'}
                              onValueChange={(v) =>
                                setColumnActions((prev) => ({ ...prev, [col.name]: v ?? 'none' }))
                              }
                            >
                              <SelectTrigger className="h-8 w-full text-xs">
                                <SelectValue />
                              </SelectTrigger>
                              <SelectContent>
                                {COLUMN_ACTIONS.map((a) => (
                                  <SelectItem key={a.value} value={a.value}>
                                    {a.label}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                            {col.recommended_action && col.recommended_action !== 'none' && (
                              columnActions[col.name] === col.recommended_action ? (
                                <span className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium text-primary">
                                  <ShieldCheck className="size-3" /> Recommended
                                </span>
                              ) : (
                                <button
                                  type="button"
                                  onClick={() =>
                                    setColumnActions((prev) => ({
                                      ...prev,
                                      [col.name]: col.recommended_action as string,
                                    }))
                                  }
                                  className="mt-1 text-[11px] text-muted-foreground underline-offset-2 hover:text-primary hover:underline"
                                  title="Apply the recommended action"
                                >
                                  Suggested: {ACTION_LABEL[col.recommended_action] ?? col.recommended_action}
                                </button>
                              )
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="flex flex-col gap-2 sm:flex-row">
                  <Button
                    onClick={handleTabularDeidentify}
                    disabled={isProcessing}
                    className="flex-1"
                  >
                    {isProcessing && tabularPreview ? (
                      <Loader2 data-icon="inline-start" className="h-4 w-4 animate-spin" />
                    ) : (
                      <Play data-icon="inline-start" className="h-4 w-4" />
                    )}
                    {isProcessing ? 'Processing…' : 'De-identify this file'}
                  </Button>
                  <Button
                    variant="outline"
                    onClick={handleSaveProfile}
                    disabled={isProcessing}
                    title="Save this column mapping as a reusable profile for batch jobs"
                  >
                    <Save data-icon="inline-start" className="h-4 w-4" />
                    Save as profile
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">
                  Save this mapping as a profile to reuse it for many files at once (Bulk Jobs).
                </p>

                {error && (
                  <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
                    <p className="text-sm font-medium text-destructive">Error</p>
                    <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
                  </div>
                )}

                {tabularResultUrl && (
                  <div className="flex items-center justify-between rounded-lg border border-primary/30 bg-primary/5 px-4 py-3">
                    <div className="flex items-center gap-2">
                      <ShieldCheck className="size-4 text-primary" />
                      <p className="text-sm">
                        De-identified file ready — <span className="font-medium">{tabularResultName}</span>
                      </p>
                    </div>
                    <a href={tabularResultUrl} download={tabularResultName}>
                      <Button size="sm">
                        <Download className="mr-1.5 h-3.5 w-3.5" />
                        Download
                      </Button>
                    </a>
                  </div>
                )}
              </CardContent>
            </Card>
          )}
          </div>
          )}

          {/* ---- Batch mode: many files → async job ---- */}
          {tabularMode === 'batch' && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">De-identify many files</CardTitle>
              </CardHeader>
              <CardContent className="flex flex-col gap-4">
                {/* Format selector */}
                <div className="inline-flex w-fit items-center rounded-lg border bg-background p-1">
                  {TABULAR_FORMATS.map((f) => (
                    <button
                      key={f.value}
                      type="button"
                      onClick={() => { setTabularFormat(f.value); setBatchFiles([]); setSubmittedJobId(null); }}
                      className={`rounded-md px-3 py-1 text-xs transition ${
                        tabularFormat === f.value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
                      }`}
                    >
                      {f.label}
                    </button>
                  ))}
                </div>

                {/* Multi-file picker */}
                <input
                  ref={batchInputRef}
                  type="file"
                  multiple
                  accept={TABULAR_FORMATS.find((f) => f.value === tabularFormat)?.accept}
                  className="hidden"
                  onChange={(e) => {
                    setBatchFiles(Array.from(e.target.files ?? []));
                    setSubmittedJobId(null);
                    setError(null);
                  }}
                />
                <button
                  type="button"
                  onClick={() => batchInputRef.current?.click()}
                  className="flex h-[140px] w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed transition hover:border-primary/60 hover:bg-primary/5"
                >
                  <Upload className="size-8 text-muted-foreground/40" />
                  <p className="text-sm text-foreground">
                    {batchFiles.length > 0
                      ? `${batchFiles.length} ${tabularFormat.toUpperCase()} file(s) selected`
                      : `Click to choose multiple ${tabularFormat.toUpperCase()} files`}
                  </p>
                </button>

                {/* Saved-profile picker */}
                <div>
                  <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
                    Profile (must contain <span className="font-mono">column:</span> rules)
                  </label>
                  <Select value={batchProfile} onValueChange={(v) => setBatchProfile(v ?? '')}>
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="Choose a saved profile…" />
                    </SelectTrigger>
                    <SelectContent>
                      {profiles.map((p) => (
                        <SelectItem key={p.name} value={p.name}>
                          {p.name}{!p.is_system && ' *'}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="mt-1.5 text-xs text-muted-foreground">
                    Tip: build a mapping in <strong>Single file</strong> mode, then{' '}
                    <strong>Save as profile</strong> — it appears here.
                  </p>
                </div>

                <Button
                  onClick={handleBatchSubmit}
                  disabled={isProcessing || batchFiles.length === 0 || !batchProfile}
                  className="w-full"
                >
                  {isProcessing ? (
                    <Loader2 data-icon="inline-start" className="h-4 w-4 animate-spin" />
                  ) : (
                    <Play data-icon="inline-start" className="h-4 w-4" />
                  )}
                  {isProcessing ? 'Submitting…' : 'Submit batch job'}
                </Button>

                {error && (
                  <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
                    <p className="text-sm font-medium text-destructive">Error</p>
                    <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
                  </div>
                )}

                {submittedJobId && (
                  <div className="flex items-center justify-between rounded-lg border border-primary/30 bg-primary/5 px-4 py-3">
                    <div className="flex items-center gap-2">
                      <ShieldCheck className="size-4 text-primary" />
                      <p className="text-sm">
                        Job <span className="font-mono">{submittedJobId.slice(0, 8)}</span> submitted —
                        track progress and download the result ZIP on the Jobs page.
                      </p>
                    </div>
                    <Link to="/jobs">
                      <Button size="sm" variant="outline">Open Jobs</Button>
                    </Link>
                  </div>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </div>
  );
}
