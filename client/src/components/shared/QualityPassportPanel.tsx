/**
 * Quality Passport panel, compact sidebar view of the Trust Gate assessment.
 *
 * Scoring: Kahn et al. (2016) (conformance/completeness/plausibility ×
 * verification/validation) + OHDSI DQD (violation-rate vs threshold → % checks
 * passing). PIQI HDQT v2.0 (ASTP/ONC 2024) dimension tags are shown as additive
 * metadata on each check; they do not affect the Kahn score or decision.
 */
import { useState } from 'react';
import type { TrustPassport, TrustCheck, TrustViolationDetail } from '@/api/processingRuns';
import { MarkdownReport } from '@/components/shared/MarkdownReport';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import {
  CheckCircle2,
  AlertTriangle,
  ShieldAlert,
  ChevronDown,
  ShieldCheck,
  SkipForward,
  FileCheck2,
  ListChecks,
  Activity,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { loincLabel } from '@/lib/loincLabels';

// ---------------------------------------------------------------------------
// Pillar metadata (Kahn categories with human descriptions)
// ---------------------------------------------------------------------------

const PILLAR_META: Record<
  string,
  { label: string; description: string; Icon: React.ElementType }
> = {
  conformance: {
    label: 'Conformance',
    description: 'FHIR format, coding structure, reference integrity',
    Icon: FileCheck2,
  },
  completeness: {
    label: 'Completeness',
    description: 'Required elements present; observations carry a value or absent reason',
    Icon: ListChecks,
  },
  plausibility: {
    label: 'Plausibility',
    description: 'Value ranges, temporal ordering, clinical consistency',
    Icon: Activity,
  },
};

const CATEGORY_ORDER = ['conformance', 'completeness', 'plausibility'] as const;

const DECISION_STYLE: Record<
  TrustPassport['decision'],
  { label: string; cls: string; icon: typeof CheckCircle2 }
> = {
  PASS: {
    label: 'PASS',
    cls: 'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800/40 dark:bg-emerald-950/30 dark:text-emerald-300',
    icon: CheckCircle2,
  },
  CONDITIONAL_PASS: {
    label: 'CONDITIONAL',
    cls: 'border-amber-300 bg-amber-50 text-amber-800 dark:border-amber-800/40 dark:bg-amber-950/30 dark:text-amber-300',
    icon: AlertTriangle,
  },
  BLOCK: {
    label: 'BLOCK',
    cls: 'border-red-300 bg-red-50 text-red-800 dark:border-red-800/40 dark:bg-red-950/30 dark:text-red-300',
    icon: ShieldAlert,
  },
};

const HDQT_CATEGORY_STYLE: Record<string, string> = {
  accuracy:    'bg-orange-100 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300',
  availability:'bg-blue-100 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300',
  conformity:  'bg-violet-100 text-violet-700 dark:bg-violet-950/40 dark:text-violet-300',
  plausibility:'bg-rose-100 text-rose-700 dark:bg-rose-950/40 dark:text-rose-300',
};

function rateColor(pct: number): string {
  return pct >= 90 ? 'bg-emerald-500' : pct >= 70 ? 'bg-amber-500' : 'bg-red-500';
}

function rateText(pct: number): string {
  return pct >= 90
    ? 'text-emerald-600 dark:text-emerald-400'
    : pct >= 70
    ? 'text-amber-600 dark:text-amber-400'
    : 'text-red-600 dark:text-red-400';
}

function HdqtBadge({ category, dimension }: { category?: string; dimension?: string }) {
  if (!category) return null;
  const cls = HDQT_CATEGORY_STYLE[category] ?? 'bg-muted text-muted-foreground';
  return (
    <span
      className={cn('rounded px-1 py-0.5 text-[9px] font-semibold leading-none', cls)}
      title={`PIQI HDQT v2.0: ${category}/${dimension ?? ''}`}
    >
      {category}{dimension ? `·${dimension.replace(/_/g, '-')}` : ''}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Pillar bar, includes description and pass/fail count
// ---------------------------------------------------------------------------

function PillarBar({ name, score, checks }: {
  name: string;
  score: number | null;
  checks: TrustCheck[];
}) {
  const meta     = PILLAR_META[name];
  const Icon     = meta?.Icon;
  const assessed = score !== null;
  const pct      = assessed ? Math.round(score) : 0;

  const pillarChecks = checks.filter((c) => c.category === name && c.result !== 'NA');
  const passed  = pillarChecks.filter((c) => c.result === 'PASS').length;
  const failed  = pillarChecks.filter((c) => c.result === 'FAIL').length;

  return (
    <div className="rounded-md border border-muted bg-muted/20 p-2.5 space-y-1.5">
      {/* Header */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          {Icon && <Icon className="size-3.5 shrink-0 text-muted-foreground" />}
          <span className="text-[11px] font-semibold capitalize">{meta?.label ?? name}</span>
        </div>
        <span
          className={cn(
            'text-base font-bold tabular-nums leading-none',
            assessed ? rateText(pct) : 'text-muted-foreground',
          )}
        >
          {assessed ? `${pct}%` : 'n/a'}
        </span>
      </div>

      {/* Progress bar */}
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        {assessed && (
          <div className={cn('h-full rounded-full', rateColor(pct))} style={{ width: `${pct}%` }} />
        )}
      </div>

      {/* Pass/fail counts */}
      <div className="flex items-center gap-2 text-[10px] tabular-nums">
        <span className="text-emerald-600 dark:text-emerald-400">{passed} passed</span>
        {failed > 0 && <span className="text-red-600 dark:text-red-400">{failed} failed</span>}
      </div>

      {/* Description */}
      {meta?.description && (
        <p className="text-[10px] leading-snug text-muted-foreground">{meta.description}</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Mini SVG box-plot for observation stats
// ---------------------------------------------------------------------------

function MiniBoxPlot({
  min, max, mean, stddev, code, unit, count, display,
}: {
  min: number; max: number; mean: number; stddev: number;
  code: string; unit: string; count: number; display?: string;
}) {
  // Source-carried display wins; static LOINC map is the offline fallback.
  const label = display ?? loincLabel(code);
  const q1    = Math.max(min, mean - 0.674 * stddev);
  const q3    = Math.min(max, mean + 0.674 * stddev);
  const range = max - min;
  const toX   = (v: number) => (range > 0 ? ((v - min) / range) * 94 + 3 : 50);

  const xMin  = toX(min);
  const xMax  = toX(max);
  const xQ1   = toX(q1);
  const xQ3   = toX(q3);
  const xMean = toX(mean);
  const CY    = 10;
  const BH    = 6;

  const fmt = (v: number) =>
    Math.abs(v) >= 1000
      ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
      : v.toLocaleString(undefined, { maximumFractionDigits: 1 });

  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-1">
        <div className="min-w-0">
          <span className="text-[10px] font-semibold leading-none">{label}</span>
          {unit && <span className="ml-1 text-[9px] text-muted-foreground">{unit}</span>}
        </div>
        <span className="shrink-0 text-[9px] tabular-nums text-muted-foreground">n={count.toLocaleString()}</span>
      </div>

      <svg viewBox="0 0 100 20" height={20} className="w-full">
        <line x1={xMin} y1={CY} x2={xMax} y2={CY}
          stroke="hsl(var(--muted-foreground))" strokeWidth={0.6} />
        <rect
          x={xQ1} y={CY - BH / 2}
          width={Math.max(xQ3 - xQ1, 0.5)} height={BH}
          fill="hsl(var(--primary) / 0.15)"
          stroke="hsl(var(--primary) / 0.7)"
          strokeWidth={0.6} rx={0.4}
        />
        <line x1={xMean} y1={CY - BH / 2 - 1.5} x2={xMean} y2={CY + BH / 2 + 1.5}
          stroke="hsl(var(--primary))" strokeWidth={1.4} strokeLinecap="round" />
        <line x1={xMin} y1={CY - 2.5} x2={xMin} y2={CY + 2.5}
          stroke="hsl(var(--muted-foreground))" strokeWidth={0.7} />
        <line x1={xMax} y1={CY - 2.5} x2={xMax} y2={CY + 2.5}
          stroke="hsl(var(--muted-foreground))" strokeWidth={0.7} />
      </svg>

      <div className="flex justify-between text-[9px] tabular-nums text-muted-foreground">
        <span>{fmt(min)}</span>
        <span className="font-semibold text-foreground">{fmt(mean)}</span>
        <span>{fmt(max)}</span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Failed checks list
// ---------------------------------------------------------------------------

function FailedChecks({ checks }: { checks: TrustCheck[] }) {
  const failed = checks.filter((c) => c.result === 'FAIL');
  if (failed.length === 0) return null;
  return (
    <div className="space-y-1">
      {failed.map((c) => (
        <div
          key={c.check_id}
          className={cn(
            'rounded border px-2.5 py-1.5 text-[10px]',
            c.critical
              ? 'border-red-200 bg-red-50/60 dark:border-red-800/30 dark:bg-red-950/20'
              : 'border-amber-200 bg-amber-50/60 dark:border-amber-800/30 dark:bg-amber-950/20',
          )}
        >
          <div className="flex items-start justify-between gap-2">
            <div className="flex flex-wrap items-center gap-1">
              <span className="font-mono font-semibold">{c.check_id}</span>
              {c.critical && (
                <span className="rounded bg-red-600 px-1 py-0.5 text-[8px] font-bold uppercase text-white leading-none">
                  critical
                </span>
              )}
              <HdqtBadge category={c.hdqt_category} dimension={c.hdqt_dimension} />
            </div>
            <span className="shrink-0 tabular-nums text-muted-foreground">
              {c.violations}/{c.applicable} ({Math.round(c.violation_fraction * 100)}%)
            </span>
          </div>
          {c.recommendation && (
            <p className="mt-0.5 text-muted-foreground">{c.recommendation}</p>
          )}
        </div>
      ))}
    </div>
  );
}

function SkippedChecksSection({
  skipped,
}: {
  skipped: Record<string, { skipped: number; reason: string }>;
}) {
  const entries = Object.entries(skipped);
  if (entries.length === 0) return null;
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1">
        <SkipForward className="size-3 text-muted-foreground" />
        <p className="text-[10px] font-semibold uppercase text-muted-foreground">
          Skipped checks{' '}
          <span className="font-normal normal-case">(excluded from score)</span>
        </p>
      </div>
      <div className="space-y-0.5">
        {entries.map(([checkId, info]) => (
          <div
            key={checkId}
            className="flex items-start justify-between gap-2 rounded border border-muted bg-muted/30 px-2.5 py-1.5 text-[10px]"
          >
            <div>
              <span className="font-mono font-medium">{checkId}</span>
              <p className="mt-0.5 text-muted-foreground">{info.reason}</p>
            </div>
            {info.skipped > 1 && (
              <span className="shrink-0 tabular-nums text-muted-foreground">×{info.skipped}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function ViolationsSection({ violations }: { violations: TrustViolationDetail[] }) {
  const [open, setOpen] = useState(false);
  if (violations.length === 0) return null;
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="flex w-full items-center gap-1.5 text-[10px] font-medium text-muted-foreground hover:text-foreground">
        <ChevronDown className={cn('size-3 transition-transform', open && 'rotate-180')} />
        Violation details
        <span className="ml-1 tabular-nums text-muted-foreground/70">
          ({violations.length} total)
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent className="pt-1.5">
        <div className="max-h-48 space-y-1 overflow-auto">
          {violations.map((v, i) => {
            const where = v.path ?? v.attribute;
            return (
              <div
                key={i}
                className="rounded border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-[10px] dark:border-amber-800/30 dark:bg-amber-950/20"
              >
                <div className="flex flex-wrap items-center gap-1">
                  {v.resource_type && (
                    <span className="font-mono text-muted-foreground">
                      {v.resource_type}{v.resource_id ? `/${v.resource_id}` : ''}
                    </span>
                  )}
                  {where && <span className="font-mono font-medium">{where}</span>}
                  <HdqtBadge category={v.hdqt_category} dimension={v.hdqt_dimension} />
                </div>
                <p className="mt-0.5 text-muted-foreground">{v.detail}</p>
              </div>
            );
          })}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

export function QualityPassportPanel({ passport }: { passport: TrustPassport }) {
  const [reportOpen, setReportOpen] = useState(false);
  const ds  = DECISION_STYLE[passport.decision] ?? DECISION_STYLE.CONDITIONAL_PASS;
  const Icon = ds.icon;
  const overall = passport.overall_score != null ? Math.round(passport.overall_score) : null;
  const prov    = passport.auditability?.provenance_present;
  const skipped = passport.skipped_checks ?? {};
  const violations = passport.violations ?? [];
  const fv  = passport.framework_versions;
  const checks: TrustCheck[] = passport.checks ?? [];

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <ShieldCheck className="size-4 text-primary" />
        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          Trust Gate, Quality Passport
        </p>
      </div>

      {/* Decision + overall */}
      <div className={cn('flex items-center gap-2.5 rounded-lg border px-3 py-2.5', ds.cls)}>
        <Icon className="size-4 shrink-0" />
        <div className="flex-1">
          <p className="text-xs font-bold">{ds.label}</p>
          <p className="text-[10px] opacity-80">
            {overall != null ? `${overall}% checks passing` : "not assessed"}
            {passport.degraded && ' · advisory (Trust Gate degraded)'}
          </p>
        </div>
        {fv && (
          <span
            className="shrink-0 rounded border border-current/20 bg-current/5 px-1.5 py-0.5 text-[9px] font-medium opacity-70"
            title={`Kahn ${fv.kahn} · HDQT ${fv.hdqt} · ${fv.evaluation_profile}`}
          >
            HDQT {fv.hdqt}
          </span>
        )}
      </div>

      {/* Quality pillars */}
      <div className="space-y-2">
        {CATEGORY_ORDER.map((cat) => (
          <PillarBar
            key={cat}
            name={cat}
            score={passport.category_scores?.[cat] ?? null}
            checks={checks}
          />
        ))}
      </div>

      {/* Blockers */}
      {passport.blockers?.length > 0 && (
        <div className="space-y-1">
          <p className="text-[10px] font-semibold uppercase text-red-600">Blockers</p>
          {passport.blockers.map((b, i) => (
            <p
              key={i}
              className="rounded border border-red-200 bg-red-50/60 px-2.5 py-1.5 text-[10px] dark:border-red-800/30 dark:bg-red-950/20"
            >
              {b}
            </p>
          ))}
        </div>
      )}

      {/* Failed checks */}
      <FailedChecks checks={checks} />

      {/* Structured violation details */}
      <ViolationsSection violations={violations} />

      {/* Skipped checks */}
      <SkippedChecksSection skipped={skipped} />

      {/* Auditability */}
      <div className="flex items-center justify-between rounded border border-muted bg-muted/30 px-2.5 py-1.5 text-[10px]">
        <span className="font-medium">Provenance</span>
        <span className={cn('font-semibold', prov ? 'text-emerald-600' : 'text-amber-600')}>
          {prov ? 'present' : 'missing'}
        </span>
      </div>

      {/* Fitness for use */}
      {(passport.approved_for?.length > 0 || passport.not_approved_for?.length > 0) && (
        <div className="space-y-0.5 text-[10px]">
          {passport.approved_for?.map((u, i) => (
            <p key={`a${i}`} className="text-emerald-700 dark:text-emerald-400">
              Approved: {u}
            </p>
          ))}
          {passport.not_approved_for?.map((u, i) => (
            <p key={`n${i}`} className="text-amber-700 dark:text-amber-400">
              Not approved: {u}
            </p>
          ))}
        </div>
      )}

      {/* Data profile */}
      {passport.profile && passport.profile.total_resources > 0 && (
        <div className="space-y-2">
          <p className="text-[10px] font-semibold uppercase text-muted-foreground">
            Data profile{' '}
            <span className="font-normal normal-case">(descriptive)</span>
          </p>

          {/* Resource counts */}
          {Object.keys(passport.profile.resource_counts).length > 0 && (
            <div className="rounded border border-muted bg-muted/30 px-2.5 py-1.5 space-y-1 text-[10px]">
              {Object.entries(passport.profile.resource_counts)
                .sort(([, a], [, b]) => b - a)
                .map(([type, count]) => {
                  const total = Object.values(passport.profile!.resource_counts).reduce((s, v) => s + v, 0);
                  const pct   = total > 0 ? (count / total) * 100 : 0;
                  return (
                    <div key={type}>
                      <div className="flex justify-between">
                        <span className="font-medium">{type}</span>
                        <span className="tabular-nums text-muted-foreground">{count.toLocaleString()}</span>
                      </div>
                      <div className="h-1 overflow-hidden rounded-full bg-muted">
                        <div className="h-full rounded-full bg-primary/50" style={{ width: `${pct}%` }} />
                      </div>
                    </div>
                  );
                })}
            </div>
          )}

          {/* Gender */}
          {Object.keys(passport.profile.patient_gender_distribution).length > 0 && (
            <div className="flex justify-between rounded border border-muted bg-muted/30 px-2.5 py-1.5 text-[10px]">
              <span className="text-muted-foreground">Gender</span>
              <span className="tabular-nums">
                {Object.entries(passport.profile.patient_gender_distribution)
                  .map(([k, v]) => `${k}: ${v}`)
                  .join(', ')}
              </span>
            </div>
          )}

          {/* Reference density */}
          <div className="flex justify-between rounded border border-muted bg-muted/30 px-2.5 py-1.5 text-[10px]">
            <span className="text-muted-foreground">Reference density</span>
            <span className="tabular-nums">{passport.profile.reference_density}</span>
          </div>

          {/* Observation distributions */}
          {passport.profile.observation_value_stats.length > 0 && (
            <div className="space-y-2">
              <p className="text-[10px] text-muted-foreground/70">
                Clinical value distributions, box = IQR, line = mean
              </p>
              {passport.profile.observation_value_stats.map((s) => (
                <MiniBoxPlot
                  key={`${s.code}-${s.unit}`}
                  min={s.min}
                  max={s.max}
                  mean={s.mean}
                  stddev={s.stddev}
                  code={s.code}
                  unit={s.unit}
                  count={s.count}
                  display={s.display}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Full Markdown report */}
      {passport.report && (
        <Collapsible open={reportOpen} onOpenChange={setReportOpen}>
          <CollapsibleTrigger className="flex w-full items-center gap-1.5 text-[10px] font-medium text-muted-foreground hover:text-foreground">
            <ChevronDown className={cn('size-3 transition-transform', reportOpen && 'rotate-180')} />
            Full passport report
          </CollapsibleTrigger>
          <CollapsibleContent className="pt-1.5">
            <div className="max-h-72 overflow-auto rounded border bg-muted/40 p-2.5">
              <MarkdownReport markdown={passport.report} />
            </div>
          </CollapsibleContent>
        </Collapsible>
      )}

      {/* Framework footer */}
      <p className="text-[9px] text-muted-foreground/70">
        {fv
          ? `Kahn ${fv.kahn} · PIQI HDQT ${fv.hdqt} · OHDSI DQD · ${fv.evaluation_profile}`
          : passport.framework}
      </p>
    </div>
  );
}
