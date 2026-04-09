import { useState, useMemo } from 'react';
import {
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  Info,
  Lightbulb,
  ChevronDown,
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
import { MetricCard } from '@/components/shared/MetricCard';
import { DownloadButton } from '@/components/shared/DownloadButton';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
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
import type { RiskReport } from '@/api/types';
import {
  pct,
  riskVariant,
  riskBannerClasses,
  riskBannerText,
  getRecommendations,
  MAX_TABLE_ROWS,
} from './RiskHelpers';

// ---------------------------------------------------------------------------
// Local JSX helper (cannot live in a .ts file)
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface RiskResultsPanelProps {
  report: RiskReport;
}

export function RiskResultsPanel({ report }: RiskResultsPanelProps) {
  // Collapsible state
  const [groupsOpen, setGroupsOpen] = useState(false);
  const [lDiversityDetailsOpen, setLDiversityDetailsOpen] = useState(false);
  const [metadataOpen, setMetadataOpen] = useState(false);

  // Derived data
  const chartData = useMemo(() => {
    const counts: Record<number, number> = {};
    report.groups.forEach((g) => {
      counts[g.k] = (counts[g.k] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([k, count]) => ({ k: Number(k), count }))
      .sort((a, b) => a.k - b.k);
  }, [report]);

  const recommendations = useMemo(() => {
    return getRecommendations(report);
  }, [report]);

  return (
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
  );
}
