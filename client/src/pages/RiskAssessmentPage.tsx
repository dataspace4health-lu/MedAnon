import { useState, useCallback, useMemo } from 'react';
import { toast } from 'sonner';
import {
  Loader2,
  Play,
  FileCode,
  ChevronDown,
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  Info,
  Lightbulb,
} from 'lucide-react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import { PageHeader } from '@/components/layout/PageHeader';
import { FileUploader } from '@/components/shared/FileUploader';
import { MetricCard } from '@/components/shared/MetricCard';
import { DownloadButton } from '@/components/shared/DownloadButton';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Textarea } from '@/components/ui/textarea';
import { Separator } from '@/components/ui/separator';
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

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const ACCEPTED_EXTENSIONS = ['.ndjson', '.json', '.xml'];

const FORMAT_CT: Record<string, string> = {
  NDJSON: 'application/x-ndjson',
  'JSON / Bundle': 'application/json',
  XML: 'application/fhir+xml',
};

const FORMAT_OPTIONS = Object.keys(FORMAT_CT);

const EXAMPLE_NDJSON = [
  '{"resourceType":"Patient","id":"p1","gender":"male","birthDate":"1982-01-01","address":[{"postalCode":"10115"}]}',
  '{"resourceType":"Patient","id":"p2","gender":"female","birthDate":"1975-01-01","address":[{"postalCode":"10115"}]}',
  '{"resourceType":"Patient","id":"p3","gender":"male","birthDate":"1982-01-01","address":[{"postalCode":"10115"}]}',
  '{"resourceType":"Patient","id":"p4","gender":"female","birthDate":"1990-01-01","address":[{"postalCode":"20148"}]}',
  '{"resourceType":"Patient","id":"p5","gender":"male","birthDate":"1990-01-01","address":[{"postalCode":"20148"}]}',
  '{"resourceType":"Patient","id":"p6","gender":"female","birthDate":"1975-01-01","address":[{"postalCode":"80331"}]}',
].join('\n');

const EXT_CT: Record<string, string> = {
  '.ndjson': 'application/x-ndjson',
  '.json': 'application/json',
  '.xml': 'application/fhir+xml',
};

const MAX_TABLE_ROWS = 100;

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

function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function riskVariant(
  level: RiskReport['summary']['risk_level'],
): 'success' | 'warning' | 'destructive' {
  switch (level) {
    case 'low':
      return 'success';
    case 'medium':
      return 'warning';
    case 'high':
    case 'critical':
      return 'destructive';
  }
}

function riskBannerClasses(level: RiskReport['summary']['risk_level']): string {
  switch (level) {
    case 'low':
      return 'border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400';
    case 'medium':
      return 'border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400';
    case 'high':
    case 'critical':
      return 'border-destructive/50 bg-destructive/10 text-destructive';
  }
}

function riskBannerText(level: RiskReport['summary']['risk_level']): string {
  switch (level) {
    case 'low':
      return 'Low risk -- minimum group size is 5 or greater. The dataset provides strong protection against re-identification attacks.';
    case 'medium':
      return 'Medium risk -- minimum group size is between 3 and 4. Some groups may be vulnerable to targeted re-identification. Consider additional generalization.';
    case 'high':
      return 'High risk -- minimum group size is 2. Several groups contain only pairs of records, making them vulnerable to re-identification. Broader suppression or generalization is recommended.';
    case 'critical':
      return 'Critical risk -- singleton groups exist (k=1). Individuals in these groups are uniquely identifiable and must be suppressed or further generalized before release.';
  }
}

function riskBannerIcon(level: RiskReport['summary']['risk_level']) {
  switch (level) {
    case 'low':
      return <ShieldCheck className="h-5 w-5 shrink-0" />;
    case 'medium':
      return <AlertTriangle className="h-5 w-5 shrink-0" />;
    case 'high':
    case 'critical':
      return <ShieldAlert className="h-5 w-5 shrink-0" />;
  }
}

function getRecommendations(report: RiskReport): string[] {
  const recs: string[] = [];
  const s = report.summary;
  if (s.min_k < 5)
    recs.push(
      'Apply broader date generalization (e.g., year-only birth date) to increase minimum group size.',
    );
  if (s.min_k < 3)
    recs.push(
      'Suppress or redact postal codes for records in singleton or pair groups.',
    );
  if (s.singleton_groups > 0)
    recs.push(
      `${s.singleton_groups} group(s) contain only 1 record \u2014 these individuals are uniquely identifiable and should be suppressed or further generalized.`,
    );
  if (s.records_with_missing_qi > 0)
    recs.push(
      `${s.records_with_missing_qi} record(s) have missing quasi-identifier fields \u2014 verify de-identification rules cover gender, birthDate, and address.postalCode.`,
    );
  const ld = report.l_diversity;
  if (ld.computed && (ld.violations ?? 0) > 0)
    recs.push(
      `${ld.violations} group(s) violate 2-diversity \u2014 patients in these groups share the same Condition codes and may be vulnerable to attribute inference.`,
    );
  if (recs.length === 0)
    recs.push(
      'Dataset meets all configured thresholds. No immediate action required.',
    );
  return recs;
}

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
  const [groupsOpen, setGroupsOpen] = useState(false);
  const [lDiversityDetailsOpen, setLDiversityDetailsOpen] = useState(false);
  const [metadataOpen, setMetadataOpen] = useState(false);

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

  // -- derived data ---------------------------------------------------------

  const chartData = useMemo(() => {
    if (!report) return [];
    const counts: Record<number, number> = {};
    report.groups.forEach((g) => {
      counts[g.k] = (counts[g.k] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([k, count]) => ({ k: Number(k), count }))
      .sort((a, b) => a.k - b.k);
  }, [report]);

  const recommendations = useMemo(() => {
    if (!report) return [];
    return getRecommendations(report);
  }, [report]);

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
      {report && (
        <div className="flex flex-col gap-6">
          {/* (a) Risk level banner */}
          <div
            className={`flex items-start gap-3 rounded-lg border p-4 ${riskBannerClasses(report.summary.risk_level)}`}
          >
            {riskBannerIcon(report.summary.risk_level)}
            <div>
              <p className="font-semibold capitalize">
                {report.summary.risk_level} risk
              </p>
              <p className="mt-1 text-sm">
                {riskBannerText(report.summary.risk_level)}
              </p>
            </div>
          </div>

          {/* (b) Row 1: Primary metrics */}
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <MetricCard
              label="Min k"
              value={report.summary.min_k}
              variant={riskVariant(report.summary.risk_level)}
            />
            <MetricCard
              label="Prosecutor Risk"
              value={pct(report.summary.prosecutor_risk)}
              variant={riskVariant(report.summary.risk_level)}
            />
            <MetricCard
              label="Journalist Risk"
              value={pct(report.summary.journalist_risk)}
              variant={riskVariant(report.summary.risk_level)}
            />
            <MetricCard
              label="Marketer Risk"
              value={pct(report.summary.marketer_risk)}
              variant={riskVariant(report.summary.risk_level)}
            />
          </div>

          {/* (c) Row 2: Dataset metrics */}
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            <MetricCard
              label="Total Patients"
              value={report.summary.total_records}
            />
            <MetricCard
              label="Equivalence Classes"
              value={report.summary.total_groups}
            />
            <MetricCard
              label="Singleton Groups"
              value={report.summary.singleton_groups}
              variant={
                report.summary.singleton_groups > 0 ? 'destructive' : 'default'
              }
            />
            <MetricCard
              label="Missing QI Records"
              value={report.summary.records_with_missing_qi}
              variant={
                report.summary.records_with_missing_qi > 0
                  ? 'warning'
                  : 'default'
              }
            />
          </div>

          {/* (d) Separator */}
          <Separator />

          {/* (e) l-Diversity section */}
          <Card>
            <CardHeader>
              <CardTitle>l-Diversity</CardTitle>
            </CardHeader>
            <CardContent>
              {report.l_diversity.computed ? (
                <div className="flex flex-col gap-4">
                  <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
                    <MetricCard
                      label="Min l"
                      value={report.l_diversity.min_l}
                      variant={
                        report.l_diversity.min_l < 2 ? 'warning' : 'success'
                      }
                    />
                    <MetricCard
                      label="Max l"
                      value={report.l_diversity.max_l}
                    />
                    <MetricCard
                      label="Violations"
                      value={report.l_diversity.violations}
                      variant={
                        report.l_diversity.violations > 0
                          ? 'destructive'
                          : 'success'
                      }
                    />
                  </div>

                  {report.l_diversity.details &&
                    report.l_diversity.details.length > 0 && (
                      <Collapsible
                        open={lDiversityDetailsOpen}
                        onOpenChange={setLDiversityDetailsOpen}
                      >
                        <CollapsibleTrigger className="flex items-center gap-1 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
                          <ChevronDown
                            className={`h-4 w-4 transition-transform ${
                              lDiversityDetailsOpen ? 'rotate-180' : ''
                            }`}
                          />
                          Group details ({report.l_diversity.details.length}{' '}
                          group(s))
                        </CollapsibleTrigger>
                        <CollapsibleContent>
                          <div className="mt-2">
                            <Table>
                              <TableHeader>
                                <TableRow>
                                  <TableHead>Gender</TableHead>
                                  <TableHead>Birth Year</TableHead>
                                  <TableHead>Zip Prefix</TableHead>
                                  <TableHead className="text-right">
                                    l-Value
                                  </TableHead>
                                  <TableHead className="text-right">
                                    Distinct Codes
                                  </TableHead>
                                </TableRow>
                              </TableHeader>
                              <TableBody>
                                {report.l_diversity.details.map((d, i) => (
                                  <TableRow key={i}>
                                    <TableCell>
                                      {d.group.gender || '\u2014'}
                                    </TableCell>
                                    <TableCell>
                                      {d.group.birth_year || '\u2014'}
                                    </TableCell>
                                    <TableCell>
                                      {d.group.zip_prefix || '\u2014'}
                                    </TableCell>
                                    <TableCell className="text-right tabular-nums">
                                      {d.l_value}
                                    </TableCell>
                                    <TableCell className="text-right tabular-nums">
                                      {d.distinct_codes}
                                    </TableCell>
                                  </TableRow>
                                ))}
                              </TableBody>
                            </Table>
                          </div>
                        </CollapsibleContent>
                      </Collapsible>
                    )}
                </div>
              ) : (
                <div className="flex items-start gap-3 rounded-lg border border-blue-500/30 bg-blue-500/5 p-4 text-sm text-blue-700 dark:text-blue-400">
                  <Info className="h-4 w-4 shrink-0 mt-0.5" />
                  <p>
                    l-Diversity was not computed.{' '}
                    {report.l_diversity.reason ||
                      'Condition resources are required to calculate l-diversity.'}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          {/* (f) Bar chart */}
          {chartData.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle>Equivalence Class Size Distribution</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="h-[300px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart
                      data={chartData}
                      margin={{ top: 5, right: 20, left: 10, bottom: 5 }}
                    >
                      <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                      <XAxis
                        dataKey="k"
                        label={{
                          value: 'Group size (k)',
                          position: 'insideBottom',
                          offset: -2,
                          style: { fontSize: 12 },
                        }}
                      />
                      <YAxis
                        allowDecimals={false}
                        label={{
                          value: 'Number of groups',
                          angle: -90,
                          position: 'insideLeft',
                          offset: 5,
                          style: { fontSize: 12 },
                        }}
                      />
                      <Tooltip
                        formatter={(value: any) => [value, 'Groups']}
                        labelFormatter={(label: any) => `k = ${label}`}
                      />
                      <Bar
                        dataKey="count"
                        fill="hsl(var(--primary))"
                        radius={[4, 4, 0, 0]}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </CardContent>
            </Card>
          )}

          {/* (g) Equivalence classes table */}
          <Collapsible open={groupsOpen} onOpenChange={setGroupsOpen}>
            <Card>
              <CardHeader>
                <CollapsibleTrigger className="flex w-full items-center justify-between">
                  <CardTitle>
                    Equivalence Classes ({report.groups.length})
                  </CardTitle>
                  <ChevronDown
                    className={`h-4 w-4 text-muted-foreground transition-transform ${
                      groupsOpen ? 'rotate-180' : ''
                    }`}
                  />
                </CollapsibleTrigger>
              </CardHeader>
              <CollapsibleContent>
                <CardContent>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Gender</TableHead>
                        <TableHead>Birth Year</TableHead>
                        <TableHead>Zip Prefix</TableHead>
                        <TableHead className="text-right">Size (k)</TableHead>
                        <TableHead className="text-right">
                          Risk (1/k)
                        </TableHead>
                        <TableHead className="text-right">Weight</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {report.groups.slice(0, MAX_TABLE_ROWS).map((g, i) => (
                        <TableRow key={i}>
                          <TableCell>{g.qi.gender || '\u2014'}</TableCell>
                          <TableCell>{g.qi.birth_year || '\u2014'}</TableCell>
                          <TableCell>{g.qi.zip_prefix || '\u2014'}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {g.k}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {g.risk_1_over_k.toFixed(3)}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {g.weight.toFixed(3)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                  {report.groups.length > MAX_TABLE_ROWS && (
                    <p className="mt-3 text-sm text-muted-foreground">
                      Showing first {MAX_TABLE_ROWS} of {report.groups.length}{' '}
                      groups
                    </p>
                  )}
                </CardContent>
              </CollapsibleContent>
            </Card>
          </Collapsible>

          {/* (h) Warnings */}
          {report.warnings.length > 0 && (
            <div className="flex flex-col gap-2">
              {report.warnings.map((warning, i) => (
                <div
                  key={i}
                  className="flex items-start gap-3 rounded-lg border border-amber-500/50 bg-amber-500/10 p-4 text-sm text-amber-700 dark:text-amber-400"
                >
                  <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5" />
                  <p>{warning}</p>
                </div>
              ))}
            </div>
          )}

          {/* (i) Recommendations */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <Lightbulb className="h-4 w-4" />
                Recommendations
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ul className="flex flex-col gap-2">
                {recommendations.map((rec, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-2 text-sm text-muted-foreground"
                  >
                    <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-muted-foreground/50" />
                    {rec}
                  </li>
                ))}
              </ul>
            </CardContent>
          </Card>

          {/* (j) Analysis metadata */}
          <Collapsible open={metadataOpen} onOpenChange={setMetadataOpen}>
            <Card>
              <CardHeader>
                <CollapsibleTrigger className="flex w-full items-center justify-between">
                  <CardTitle>Analysis Metadata</CardTitle>
                  <ChevronDown
                    className={`h-4 w-4 text-muted-foreground transition-transform ${
                      metadataOpen ? 'rotate-180' : ''
                    }`}
                  />
                </CollapsibleTrigger>
              </CardHeader>
              <CollapsibleContent>
                <CardContent>
                  <Table>
                    <TableBody>
                      <TableRow>
                        <TableCell className="font-medium">
                          Computed at
                        </TableCell>
                        <TableCell>{report.meta.computed_at}</TableCell>
                      </TableRow>
                      <TableRow>
                        <TableCell className="font-medium">
                          Input lines
                        </TableCell>
                        <TableCell>{report.meta.input_lines}</TableCell>
                      </TableRow>
                      <TableRow>
                        <TableCell className="font-medium">
                          Patient lines
                        </TableCell>
                        <TableCell>{report.meta.patient_lines}</TableCell>
                      </TableRow>
                      <TableRow>
                        <TableCell className="font-medium">
                          Condition lines
                        </TableCell>
                        <TableCell>{report.meta.condition_lines}</TableCell>
                      </TableRow>
                      <TableRow>
                        <TableCell className="font-medium">
                          Quasi-identifiers
                        </TableCell>
                        <TableCell>gender, birth_year, zip_prefix</TableCell>
                      </TableRow>
                    </TableBody>
                  </Table>
                </CardContent>
              </CollapsibleContent>
            </Card>
          </Collapsible>

          {/* (k) Download report */}
          <Card>
            <CardContent>
              <DownloadButton
                data={JSON.stringify(report, null, 2)}
                filename="risk_report.json"
                mime="application/json"
                label="Download JSON report"
              />
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
