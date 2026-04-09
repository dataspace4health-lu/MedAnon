import { useState, useCallback } from 'react';
import { toast } from 'sonner';
import {
  Loader2,
  Play,
  FileCode,
  ChevronDown,
  Info,
} from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { FileUploader } from '@/components/shared/FileUploader';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { analyseRisk } from '@/api/medanon';
import { MAX_UPLOAD_BYTES } from '@/config/constants';
import type { RiskReport } from '@/api/types';
import {
  ACCEPTED_EXTENSIONS,
  FORMAT_CT,
  FORMAT_OPTIONS,
  EXAMPLE_NDJSON,
  EXT_CT,
  formatBytes,
} from './risk-assessment/RiskHelpers';
import { RiskResultsPanel } from './risk-assessment/RiskResultsPanel';

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function RiskAssessmentPage() {
  // Input state
  const [uploadedFile, setUploadedFile] = useState<{
    name: string;
    size: number;
    content: string;
    contentType: string;
  } | null>(null);
  const [pasteText, setPasteText] = useState('');
  const [pasteFormat, setPasteFormat] = useState('NDJSON');

  // Result state
  const [report, setReport] = useState<RiskReport | null>(null);
  const [isAnalysing, setIsAnalysing] = useState(false);

  // Collapsible state
  const [howItWorksOpen, setHowItWorksOpen] = useState(false);

  // -- handlers -------------------------------------------------------------

  const handleFile = useCallback((_file: File, content: Uint8Array) => {
    const ext = _file.name
      .substring(_file.name.lastIndexOf('.'))
      .toLowerCase();
    const decoded = new TextDecoder().decode(content);
    const ct = EXT_CT[ext] ?? 'application/json';
    setUploadedFile({
      name: _file.name,
      size: _file.size,
      content: decoded,
      contentType: ct,
    });
    setReport(null);
  }, []);

  const handleFileError = useCallback((message: string) => {
    toast.error('File upload failed', { description: message });
  }, []);

  const handleAnalyseFile = useCallback(async () => {
    if (!uploadedFile) return;

    setIsAnalysing(true);
    setReport(null);
    try {
      const result = await analyseRisk(
        uploadedFile.content,
        uploadedFile.contentType,
      );
      setReport(result);
      toast.success('Risk analysis complete.');
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error('Risk analysis failed.', { description: message });
    } finally {
      setIsAnalysing(false);
    }
  }, [uploadedFile]);

  const handleAnalysePaste = useCallback(async () => {
    const trimmed = pasteText.trim();
    if (!trimmed) {
      toast.error('Please enter FHIR resource content before analysing.');
      return;
    }

    const contentType = FORMAT_CT[pasteFormat] ?? 'application/x-ndjson';

    setIsAnalysing(true);
    setReport(null);
    try {
      const result = await analyseRisk(trimmed, contentType);
      setReport(result);
      toast.success('Risk analysis complete.');
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      toast.error('Risk analysis failed.', { description: message });
    } finally {
      setIsAnalysing(false);
    }
  }, [pasteText, pasteFormat]);

  const handleLoadExample = useCallback(() => {
    setPasteText(EXAMPLE_NDJSON);
    setPasteFormat('NDJSON');
    setReport(null);
  }, []);

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="Risk Assessment"
        description="Upload de-identified FHIR resources to compute re-identification risk metrics including k-anonymity and l-diversity"
      />

      {/* ------------------------------------------------------------------ */}
      {/* How this works                                                      */}
      {/* ------------------------------------------------------------------ */}
      <Collapsible
        open={howItWorksOpen}
        onOpenChange={setHowItWorksOpen}
        className="mb-6"
      >
        <Card>
          <CardHeader>
            <CollapsibleTrigger className="flex w-full items-center justify-between">
              <CardTitle className="flex items-center gap-2">
                <Info className="h-4 w-4" />
                How this works
              </CardTitle>
              <ChevronDown
                className={`h-4 w-4 text-muted-foreground transition-transform ${
                  howItWorksOpen ? 'rotate-180' : ''
                }`}
              />
            </CollapsibleTrigger>
          </CardHeader>
          <CollapsibleContent>
            <CardContent className="flex flex-col gap-4 text-sm text-muted-foreground">
              <div>
                <p className="font-medium text-foreground">
                  Why risk assessment matters
                </p>
                <p className="mt-1">
                  Even after de-identification, combinations of quasi-identifiers
                  (gender, birth year, postal code prefix) can make individuals
                  re-identifiable. This tool measures how well your de-identified
                  dataset resists common re-identification attacks by computing
                  k-anonymity and l-diversity metrics.
                </p>
              </div>
              <div>
                <p className="font-medium text-foreground">
                  Accepted input formats
                </p>
                <ul className="mt-1 list-inside list-disc space-y-1">
                  <li>
                    <strong>NDJSON</strong> -- one FHIR resource per line
                    (Patient and optionally Condition resources)
                  </li>
                  <li>
                    <strong>JSON</strong> -- a single resource or FHIR Bundle
                  </li>
                  <li>
                    <strong>XML</strong> -- FHIR XML format
                  </li>
                </ul>
              </div>
              <div>
                <p className="font-medium text-foreground">Metrics</p>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Metric</TableHead>
                      <TableHead>Description</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    <TableRow>
                      <TableCell className="font-medium">k-Anonymity</TableCell>
                      <TableCell>
                        Minimum group size -- every individual is
                        indistinguishable from at least k-1 others based on
                        quasi-identifiers.
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">
                        Prosecutor risk
                      </TableCell>
                      <TableCell>
                        Maximum re-identification probability assuming the
                        attacker knows the target is in the dataset (1/min_k).
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">
                        Journalist risk
                      </TableCell>
                      <TableCell>
                        Maximum re-identification probability without prior
                        knowledge of dataset membership.
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">
                        Marketer risk
                      </TableCell>
                      <TableCell>
                        Average re-identification probability across all records
                        in the dataset.
                      </TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">l-Diversity</TableCell>
                      <TableCell>
                        Measures whether each equivalence class has at least l
                        distinct sensitive values (Condition codes), protecting
                        against attribute inference.
                      </TableCell>
                    </TableRow>
                  </TableBody>
                </Table>
              </div>
              <div>
                <p className="font-medium text-foreground">Risk levels</p>
                <ul className="mt-1 list-inside list-disc space-y-1">
                  <li>
                    <strong className="text-emerald-600 dark:text-emerald-400">
                      Low
                    </strong>{' '}
                    -- k &ge; 5
                  </li>
                  <li>
                    <strong className="text-amber-600 dark:text-amber-400">
                      Medium
                    </strong>{' '}
                    -- k &ge; 3
                  </li>
                  <li>
                    <strong className="text-destructive">High</strong> -- k &ge;
                    2
                  </li>
                  <li>
                    <strong className="text-destructive">Critical</strong> -- k =
                    1 (singleton groups exist)
                  </li>
                </ul>
              </div>
            </CardContent>
          </CollapsibleContent>
        </Card>
      </Collapsible>

      {/* ------------------------------------------------------------------ */}
      {/* Input section                                                       */}
      {/* ------------------------------------------------------------------ */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Input</CardTitle>
        </CardHeader>
        <CardContent>
          <Tabs defaultValue="upload">
            <TabsList>
              <TabsTrigger value="upload">Upload file</TabsTrigger>
              <TabsTrigger value="paste">Paste / type</TabsTrigger>
            </TabsList>

            {/* -- Upload tab -- */}
            <TabsContent value="upload">
              <div className="flex flex-col gap-4">
                <FileUploader
                  accept={ACCEPTED_EXTENSIONS}
                  maxSize={MAX_UPLOAD_BYTES}
                  onFile={handleFile}
                  onError={handleFileError}
                />
                {uploadedFile && (
                  <p className="text-sm text-muted-foreground">
                    {uploadedFile.name} ({formatBytes(uploadedFile.size)})
                  </p>
                )}
                <div>
                  <Button
                    onClick={handleAnalyseFile}
                    disabled={!uploadedFile || isAnalysing}
                  >
                    {isAnalysing ? (
                      <Loader2
                        data-icon="inline-start"
                        className="h-4 w-4 animate-spin"
                      />
                    ) : (
                      <Play data-icon="inline-start" className="h-4 w-4" />
                    )}
                    Analyse Risk
                  </Button>
                </div>
              </div>
            </TabsContent>

            {/* -- Paste tab -- */}
            <TabsContent value="paste">
              <div className="flex flex-col gap-4">
                <div className="flex items-center gap-3">
                  <Select
                    value={pasteFormat}
                    onValueChange={(val) => setPasteFormat(val ?? 'NDJSON')}
                  >
                    <SelectTrigger>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {FORMAT_OPTIONS.map((fmt) => (
                        <SelectItem key={fmt} value={fmt}>
                          {fmt}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>

                  <Button variant="outline" onClick={handleLoadExample}>
                    <FileCode
                      data-icon="inline-start"
                      className="h-4 w-4"
                    />
                    Load example
                  </Button>
                </div>

                <Textarea
                  className="h-[250px] resize-none font-mono text-xs"
                  placeholder="Paste de-identified FHIR resources here (NDJSON, JSON Bundle, or XML)..."
                  value={pasteText}
                  onChange={(e) => setPasteText(e.target.value)}
                />

                <div>
                  <Button
                    onClick={handleAnalysePaste}
                    disabled={!pasteText.trim() || isAnalysing}
                  >
                    {isAnalysing ? (
                      <Loader2
                        data-icon="inline-start"
                        className="h-4 w-4 animate-spin"
                      />
                    ) : (
                      <Play data-icon="inline-start" className="h-4 w-4" />
                    )}
                    Analyse Risk
                  </Button>
                </div>
              </div>
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      {/* ------------------------------------------------------------------ */}
      {/* Results section                                                     */}
      {/* ------------------------------------------------------------------ */}
      {report && <RiskResultsPanel report={report} />}
    </div>
  );
}
