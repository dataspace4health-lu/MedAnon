/**
 * Trust Gate API — pre-privacy FHIR data-quality assessment.
 *
 * The Trust Gate is a standalone microservice (opt-in `--profile trust`). In
 * production nginx proxies `/trust/*` → trust-gate:8400; in Vite dev mode
 * vite.config.ts proxies `/trust` → localhost:8400. Both strip the `/trust`
 * prefix, so the service sees `/v1/trust/assess`.
 *
 * The returned Quality Passport is PHI-free (counts, paths, check ids only) and
 * is grounded in Kahn et al. (2016) + the OHDSI Data Quality Dashboard scoring.
 */

import { ApiError } from "./types";

// ---------------------------------------------------------------------------
// Passport types (mirror services/trust-gate/src/passport.py → to_dict)
// ---------------------------------------------------------------------------

export type Decision = "PASS" | "CONDITIONAL_PASS" | "BLOCK";
export type CheckResultValue = "PASS" | "FAIL" | "NA";

export interface TrustCheck {
  check_id: string;
  category: "conformance" | "completeness" | "plausibility";
  subcategory: string;
  context: "verification" | "validation";
  result: CheckResultValue;
  applicable: number;
  violations: number;
  violation_fraction: number;
  threshold: number;
  critical: boolean;
  description: string;
  recommendation: string;
  hdqt_category?: string;
  hdqt_dimension?: string;
  /** Selectable audit phase (phases.ALL_PHASES). */
  phase?: string;
  /** DQ dimension for the scorecard (DAMA/ISO 25012). */
  dimension?: string;
  tags?: string[];
  skipped?: boolean;
  skip_reason?: string;
  violation_details?: Array<Record<string, unknown>>;
  /** Statistical (non-deterministic) signal: advisory-only, never moves the gate. */
  advisory?: boolean;
}

// ---------------------------------------------------------------------------
// EHDS label + evaluation provenance + findings (the standalone platform)
// ---------------------------------------------------------------------------

/** EHDS Art.56 quality + utility + maturity + FAIR label. */
export interface EhdsLabel {
  scheme: string;
  quality: { grade?: string | null; decision?: Decision; score?: number | null };
  utility: { tier?: string; score?: number | null };
  maturity: { level?: number; name?: string; basis?: string[] };
  fair: Record<string, boolean> | string[];
  /** Formalized EHDS Art. 56 data-quality-and-utility elements (EU compliance). */
  ehds?: {
    regulation?: string;
    data_source?: Record<string, unknown>;
    data_quality?: Record<string, unknown>;
    data_coverage?: Record<string, unknown>;
    technical_quality?: Record<string, unknown>;
    provenance?: Record<string, unknown>;
    assessment_coverage?: Record<string, unknown>;
  };
}

/** Determinism / reproducibility provenance (deterministic verdict contract). */
export interface Evaluation {
  decision_basis?: string;
  sampler_seed?: number;
  advisory_check_ids?: string[];
  validator_used?: boolean;
  terminology_used?: boolean;
  lifecycle_stage?: string;
  org_role?: string;
  [k: string]: unknown;
}

export type FindingStatus = "open" | "triaged" | "resolved";
export type FindingRootCause = "unknown" | "source_error" | "etl_error" | "genuine_biology";
export const FINDING_STATUSES: FindingStatus[] = ["open", "triaged", "resolved"];
export const FINDING_ROOT_CAUSES: FindingRootCause[] = ["unknown", "source_error", "etl_error", "genuine_biology"];

export interface Finding {
  id: string;
  dataset_id: string;
  assessment_id?: string | null;
  check_id: string;
  severity: "critical" | "major" | "minor";
  status: FindingStatus;
  root_cause: FindingRootCause;
  owner?: string | null;
  note: string;
  created_at?: string;
  updated_at?: string;
}

/** One row of a dataset's assessment history. */
export interface HistoryRow {
  id?: string;
  dataset_id?: string;
  decision: Decision;
  overall_score: number | null;
  grade?: string | null;
  generated_at?: string;
  recorded_at?: string;
}

/** One point of the score trend (oldest→newest after reverse). */
export interface TrendPoint {
  generated_at: string;
  decision: Decision;
  overall_score: number | null;
}

/** One append-only ALCOA++ audit-trail row. */
export interface AuditRow {
  assessment_id?: string;
  dataset_id?: string;
  provider_id?: string;
  decision?: Decision;
  source_model?: string;
  generated_at?: string;
  recorded_at?: string;
}

/** Per-phase verdict (selectable-suite roll-up). */
export interface PhaseVerdict {
  decision: Decision;
  score: number | null;
  checks_assessed: number;
  checks_passed: number;
  blockers: string[];
}

/** Per-sector verdict (target roll-up). */
export interface SectorVerdict {
  decision: Decision | "NA";
  score: number | null;
  resource_count: number;
  checks_assessed: number;
  checks_passed: number;
  phases: Record<string, PhaseVerdict>;
  blockers: string[];
}

/** One dimension's scorecard entry (DAMA/ISO 25012). */
export interface DimensionScore {
  score: number | null;
  grade: string | null;
  checks_assessed: number;
  checks_passed: number;
}

/** Purpose-bound fitness verdict. */
export interface FitnessVerdict {
  intended_use: string;
  overall_grade: string | null;
  fit: boolean;
  statement: string;
  /** Caveat naming the depth capabilities that did not run (coverage honesty). */
  coverage_caveat?: string;
}

export type ValidationDepth = "comprehensive" | "partial" | "structural_only";

/** Assessment-coverage transparency: how much of the suite actually ran, so a
 *  grade over a thin subset of checks cannot read like a full assessment. */
export interface Coverage {
  checks_assessed: number;
  checks_total: number;
  assessed_fraction: number | null;
  validation_depth: ValidationDepth;
  /** Deterministic depth capabilities not exercised (dependency/input absent). */
  not_exercised: string[];
  /** Per-type sample size when structural validation ran (not exhaustive). */
  structural_sample_per_type?: number;
  source_model?: string;
}

export interface HistogramBin {
  x0: number;
  x1: number;
  n: number;
}

export interface ObservationValueStat {
  code: string;
  unit: string;
  /** Human label carried from the source FHIR coding display / code.text. */
  display?: string;
  count: number;
  min: number;
  max: number;
  mean: number;
  stddev: number;
  median?: number;
  histogram?: HistogramBin[];
  /** Demographic stratum "sex|age-band" (stratified stats only). */
  stratum?: string;
}

export interface TrustProfile {
  total_resources?: number;
  resource_counts?: Record<string, number>;
  code_system_distribution?: Record<string, number>;
  patient_gender_distribution?: Record<string, number>;
  reference_density?: number;
  observation_value_stats?: ObservationValueStat[];
  /** Per (concept, unit, age-band x sex) clinical-value distributions (OMOP). */
  observation_value_stats_stratified?: ObservationValueStat[];
  /** OMOP table row counts. */
  table_row_counts?: Record<string, number>;
  total_rows?: number;
}

export interface QualityPassport {
  dataset_id: string;
  source_types: string[];
  decision: Decision;
  /** null when no checks were assessed (has_assessed=false). */
  overall_score: number | null;
  has_assessed?: boolean;
  category_scores: Record<string, number | null>;
  checks: TrustCheck[];
  violations: Array<Record<string, unknown>>;
  skipped_checks: Record<string, { skipped: number; reason: string }>;
  framework_versions: Record<string, string>;
  auditability: Record<string, unknown>;
  blockers: string[];
  approved_for: string[];
  not_approved_for: string[];
  provenance: Record<string, unknown>;
  generated_at: string;
  privacy_processing_allowed: boolean;
  resource_count: number;
  config_profile: string;
  framework: string;
  profile: TrustProfile;
  report: string;
  /** Per-phase verdicts (selectable suites); empty when not phased. */
  phases?: Record<string, PhaseVerdict>;
  /** Per-sector verdicts; empty when no targets requested. */
  targets?: Record<string, SectorVerdict>;
  /** Per-dimension scorecard (DAMA/ISO 25012). */
  scorecard?: Record<string, DimensionScore>;
  /** Overall letter grade. */
  overall_grade?: string | null;
  /** Purpose-bound fitness verdict. */
  fitness?: FitnessVerdict;
  /** Assessment-coverage transparency (validation depth + not-exercised). */
  coverage?: Coverage;
  /** Set by the anonymizer intake gate when the Trust Gate degraded (advisory). */
  degraded?: boolean;
  /** EHDS quality+utility+maturity label (standalone platform). */
  label?: EhdsLabel;
  /** Determinism / reproducibility provenance. */
  evaluation?: Evaluation;
  /** Reporting attribution (Wassell 2026) — also present under evaluation. */
  lifecycle_stage?: string;
  org_role?: string;
}

export interface AssessOptions {
  datasetId?: string;
  sourceTypes?: string[];
  configProfile?: string;
  provenance?: Record<string, unknown>;
  /** Select which audit phases run (omit → all). */
  phases?: string[];
  /** Request per-sector verdicts. */
  targets?: Array<Record<string, unknown>>;
  /** Declared downstream use → purpose-bound fitness verdict. */
  intendedUse?: string;
  /** Source-of-truth reference: { records: { "Type/id": { field: expected } } }. */
  reference?: Record<string, unknown>;
  /** Use case → server-side metric subset (decision-tree selection). */
  useCase?: string;
  /** Data provider id (keys provider/dataset history + audit). */
  providerId?: string;
  /** Reporting attribution (Wassell 2026). */
  lifecycleStage?: string;
  orgRole?: string;
  /** Skip the slow external FHIR-validator calls (keep in-process structural
   * checks). Set false for high-volume scans. Defaults to server-side true. */
  externalValidation?: boolean;
}

/** Build the optional tuning fields shared by assess + assessBatch + assessOmop. */
function tuningBody(opts: AssessOptions): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (opts.phases && opts.phases.length) body.phases = opts.phases;
  if (opts.targets && opts.targets.length) body.targets = opts.targets;
  if (opts.intendedUse) body.intended_use = opts.intendedUse;
  if (opts.reference) body.reference = opts.reference;
  if (opts.useCase) body.use_case = opts.useCase;
  if (opts.providerId) body.provider_id = opts.providerId;
  if (opts.lifecycleStage) body.lifecycle_stage = opts.lifecycleStage;
  if (opts.orgRole) body.org_role = opts.orgRole;
  if (opts.externalValidation === false) body.external_validation = false;
  return body;
}

// ---------------------------------------------------------------------------
// Fetch wrapper
// ---------------------------------------------------------------------------

async function postTrust<T>(path: string, body: unknown, timeout = 120_000): Promise<T> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(`/trust${path}`, {
      method: "POST",
      signal: controller.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let detail: string;
      try {
        const b = await response.json();
        detail = (b && typeof b === "object" && "detail" in b)
          ? String((b as Record<string, unknown>).detail)
          : response.statusText;
      } catch {
        detail = response.statusText;
      }
      throw new ApiError(response.status, detail);
    }
    return (await response.json()) as T;
  } finally {
    clearTimeout(timeoutId);
  }
}

/** GET /trust/health — service liveness probe. */
export async function trustGateHealthy(): Promise<boolean> {
  try {
    const r = await fetch("/trust/health", { signal: AbortSignal.timeout(5_000) });
    return r.ok;
  } catch {
    return false;
  }
}

/**
 * Assess a single resource / Bundle / list (POST /v1/trust/assess).
 * The service flattens Bundles and lists server-side.
 */
export function assess(
  resource: Record<string, unknown> | Array<Record<string, unknown>>,
  opts: AssessOptions = {},
): Promise<QualityPassport> {
  return postTrust<QualityPassport>("/v1/trust/assess", {
    resource,
    dataset_id: opts.datasetId ?? "ui-dataset",
    source_types: opts.sourceTypes ?? ["fhir"],
    config_profile: opts.configProfile ?? "auto",
    provenance: opts.provenance ?? {},
    ...tuningBody(opts),
  });
}

/**
 * Assess a list of resources as one batch (POST /v1/trust/assess/batch).
 * Used for resources pulled from the FHIR server.
 */
export function assessBatch(
  resources: Array<Record<string, unknown>>,
  opts: AssessOptions = {},
): Promise<QualityPassport> {
  return postTrust<QualityPassport>("/v1/trust/assess/batch", {
    resources,
    dataset_id: opts.datasetId ?? "ui-dataset",
    source_types: opts.sourceTypes ?? ["fhir"],
    config_profile: opts.configProfile ?? "auto",
    provenance: opts.provenance ?? {},
    ...tuningBody(opts),
  });
}

/**
 * Assess OMOP CDM or source/tabular data (POST /v1/trust/assess/omop).
 * Pass `tables` (OMOP-shaped) and optionally `mapping` (source-column → OMOP
 * column) for tabular sources, or `resources` (FHIR) to normalize onto OMOP.
 */
export function assessOmop(
  input: {
    tables?: Record<string, Array<Record<string, unknown>>>;
    mapping?: Record<string, Record<string, string>>;
    resources?: Array<Record<string, unknown>>;
  },
  opts: AssessOptions = {},
): Promise<QualityPassport> {
  return postTrust<QualityPassport>("/v1/trust/assess/omop", {
    ...input,
    dataset_id: opts.datasetId ?? "ui-dataset",
    source_types: opts.sourceTypes ?? ["omop"],
    config_profile: opts.configProfile ?? "auto",
    provenance: opts.provenance ?? {},
    ...tuningBody(opts),
  });
}

// ---------------------------------------------------------------------------
// Stateful platform reads + findings workflow (GET/POST helpers)
// ---------------------------------------------------------------------------

async function getTrust<T>(path: string, timeout = 20_000): Promise<T> {
  const r = await fetch(`/trust${path}`, { signal: AbortSignal.timeout(timeout) });
  if (!r.ok) {
    let detail = r.statusText;
    try { const b = await r.json(); if (b?.detail) detail = String(b.detail); } catch { /* keep */ }
    throw new ApiError(r.status, detail);
  }
  return (await r.json()) as T;
}

export async function listUseCases(): Promise<Array<{ id: string; description?: string }>> {
  try {
    const b = await getTrust<{ use_cases?: unknown }>("/v1/use-cases", 8_000);
    const raw = b.use_cases ?? [];
    if (Array.isArray(raw)) return raw.map((u) => (typeof u === "string" ? { id: u } : (u as { id: string })));
    return Object.entries(raw as Record<string, { description?: string }>).map(
      ([id, v]) => ({ id, description: v?.description }),
    );
  } catch { return []; }
}

export async function getHistory(datasetId: string): Promise<HistoryRow[]> {
  const b = await getTrust<{ history?: HistoryRow[] }>(`/v1/datasets/${encodeURIComponent(datasetId)}/history`);
  return b.history ?? [];
}

export async function getTrend(datasetId: string): Promise<TrendPoint[]> {
  const b = await getTrust<{ trend?: TrendPoint[] }>(`/v1/datasets/${encodeURIComponent(datasetId)}/trend`);
  return b.trend ?? [];
}

export async function getAudit(datasetId: string): Promise<AuditRow[]> {
  try {
    const b = await getTrust<{ audit?: AuditRow[] }>(`/v1/datasets/${encodeURIComponent(datasetId)}/audit`);
    return b.audit ?? [];
  } catch { return []; }
}

export async function getDatasetFindings(datasetId: string, status?: FindingStatus): Promise<Finding[]> {
  try {
    const q = status ? `?status=${status}` : "";
    const b = await getTrust<{ findings?: Finding[] }>(`/v1/datasets/${encodeURIComponent(datasetId)}/findings${q}`);
    return b.findings ?? [];
  } catch { return []; }
}

export function transitionFinding(
  findingId: string,
  body: { status?: FindingStatus; root_cause?: FindingRootCause; owner?: string; note?: string },
): Promise<Finding> {
  return postTrust<Finding>(`/v1/findings/${encodeURIComponent(findingId)}/transition`, body, 20_000);
}

// ---------------------------------------------------------------------------
// Client-side aggregation for full-server scans
//
// The Trust Gate returns one passport per call and does not aggregate across
// calls. A full-server scan assesses the data in chunks, then folds the
// per-chunk passports into one server-wide passport here. The roll-up mirrors
// the server exactly (services/trust-gate/src/engine.py + passport.py):
//   - per-check counts are summed, then result = FAIL when violations/applicable
//     exceeds the threshold (PASS otherwise, NA when nothing was applicable);
//   - category score = % of applicable checks passing; overall = same over all;
//   - decision policy: a failed CRITICAL check → BLOCK; else overall < 90, any
//     category < 80, or missing provenance → CONDITIONAL_PASS; else PASS.
// Per-resource-type thresholds are not recomputed client-side (noted in the
// aggregated auditability) — they need the server's per-type bookkeeping.
// ---------------------------------------------------------------------------

const PASS_MIN_RATE = 90;
const CATEGORY_MIN_RATE = 80;
const CATEGORIES = ["conformance", "completeness", "plausibility"] as const;

function fitness(decision: Decision, provenancePresent: boolean): [string[], string[]] {
  if (decision === "PASS") return [["secondary use (research, analytics, AI training)"], []];
  if (decision === "CONDITIONAL_PASS") {
    const notOk = ["high-stakes clinical modelling without remediation"];
    if (!provenancePresent) notOk.push("regulated sharing (missing dataset provenance)");
    return [["cohort discovery", "encounter-level descriptive statistics"], notOk];
  }
  return [[], ["any secondary use until blockers are resolved"]];
}

// DQ dimensions (DAMA/ISO 25012) — mirror services/trust-gate/src/dimensions.py.
const DIMENSIONS = [
  "completeness", "conformity", "consistency", "accuracy",
  "uniqueness", "integrity", "currency", "provenance",
] as const;

/** Letter grade for a 0–100 pass-rate (mirror dimensions.grade_for). */
function gradeFor(score: number | null): string | null {
  if (score === null) return null;
  if (score >= 95) return "A";
  if (score >= 85) return "B";
  if (score >= 75) return "C";
  if (score >= 60) return "D";
  return "F";
}

/** Purpose-bound fitness statement (mirror engine._fitness_statement). */
function fitnessStatement(
  decision: Decision,
  overall: number,
  grade: string | null,
  intendedUse?: string,
): FitnessVerdict {
  const use = (intendedUse || "secondary use (general)").trim();
  const pct = Math.round(overall * 10) / 10;
  const g = grade ?? "NA";
  if (decision === "BLOCK") {
    return {
      intended_use: use, overall_grade: grade, fit: false,
      statement: `Not fit for ${use}: blocking quality failures must be resolved (grade ${g}, ${pct}% checks passing).`,
    };
  }
  if (decision === "CONDITIONAL_PASS") {
    return {
      intended_use: use, overall_grade: grade, fit: true,
      statement: `Conditionally fit for ${use}: grade ${g} (${pct}% checks passing) — remediate the flagged dimensions before high-stakes use.`,
    };
  }
  return {
    intended_use: use, overall_grade: grade, fit: true,
    statement: `Fit for ${use}: grade ${g} (${pct}% checks passing).`,
  };
}

/** A globally-computed check that replaces the per-chunk-summed version. */
export interface CheckOverride {
  applicable: number;
  violations: number;
  violation_details: Array<Record<string, unknown>>;
}

export interface AggregateOpts {
  datasetId: string;
  provenance: Record<string, unknown>;
  resourceCount: number;
  chunkCount: number;
  sourceTypes?: string[];
  /** Replace conformance.reference_integrity with a batch-wide computation
   * (chunked summing is wrong — references resolve across the whole scan). */
  referenceIntegrity?: CheckOverride;
  /** Declared downstream use → purpose-bound fitness statement. */
  intendedUse?: string;
}

const DETAIL_CAP = 50;

/**
 * Estimate the median from a histogram via ogive (linear) interpolation within
 * the bin that crosses the n/2 cumulative boundary. Approximate for merged data
 * but far more useful than showing "—" across every distribution card.
 */
function histMedian(bins: Array<{ x0: number; x1: number; n: number }>, total: number): number | undefined {
  if (!bins.length || !total) return undefined;
  const half = total / 2;
  let cum = 0;
  for (const b of bins) {
    if (cum + b.n >= half) {
      const frac = b.n > 0 ? (half - cum) / b.n : 0;
      return +(b.x0 + frac * (b.x1 - b.x0)).toFixed(4);
    }
    cum += b.n;
  }
  return +(bins[bins.length - 1]?.x1 ?? 0).toFixed(4);
}

/**
 * Pool a set of per-chunk distributions, keyed by (code, unit) plus an optional
 * demographic stratum. Pooled count/min/max + count-weighted mean; sd reconstructed
 * from each chunk's (mean, sd, count); histogram re-binned onto the global range
 * (chunk bins are placed by their midpoint); median estimated from the pooled
 * histogram via ogive interpolation. Approximate but enough to show the shape of
 * the clinical values across a full-server scan.
 */
function poolStats(stats: ObservationValueStat[]): ObservationValueStat[] {
  type Acc = { code: string; unit: string; display?: string; stratum?: string; count: number; sum: number; sumsq: number; min: number; max: number; bins: Array<{ x0: number; x1: number; n: number }> };
  const acc = new Map<string, Acc>();
  for (const s of stats) {
    const key = `${s.code} ${s.unit} ${s.stratum ?? ""}`;
    const e: Acc = acc.get(key) ?? { code: s.code, unit: s.unit, display: s.display, stratum: s.stratum, count: 0, sum: 0, sumsq: 0, min: Infinity, max: -Infinity, bins: [] };
    if (!e.display && s.display) e.display = s.display;
    e.count += s.count;
    e.sum += s.mean * s.count;
    // Σx² for this chunk from sample sd: (n-1)·sd² + n·mean².
    e.sumsq += (s.count > 1 ? (s.count - 1) * s.stddev * s.stddev : 0) + s.count * s.mean * s.mean;
    e.min = Math.min(e.min, s.min);
    e.max = Math.max(e.max, s.max);
    for (const b of s.histogram ?? []) e.bins.push(b);
    acc.set(key, e);
  }
  const BINS = 10;
  return [...acc.values()].map((e) => {
    const mean = e.count ? e.sum / e.count : 0;
    const variance = e.count > 1 ? Math.max(0, (e.sumsq - e.count * mean * mean) / (e.count - 1)) : 0;
    let histogram: Array<{ x0: number; x1: number; n: number }> = [];
    if (e.max > e.min) {
      const width = (e.max - e.min) / BINS;
      const counts = new Array(BINS).fill(0);
      for (const b of e.bins) {
        const mid = (b.x0 + b.x1) / 2;
        counts[Math.min(Math.floor((mid - e.min) / width), BINS - 1)] += b.n;
      }
      histogram = counts.map((n, i) => ({ x0: +(e.min + i * width).toFixed(4), x1: +(e.min + (i + 1) * width).toFixed(4), n }));
    } else {
      histogram = [{ x0: e.min, x1: e.max, n: e.count }];
    }
    const out: ObservationValueStat = {
      code: e.code, unit: e.unit, display: e.display, count: e.count,
      min: +e.min.toFixed(4), max: +e.max.toFixed(4),
      mean: +mean.toFixed(4), stddev: +Math.sqrt(variance).toFixed(4),
      median: histMedian(histogram, e.count),
      histogram,
    };
    if (e.stratum !== undefined) out.stratum = e.stratum;
    return out;
  }).sort((a, b) => b.count - a.count);
}

/** Merge per-chunk flat observation_value_stats into one distribution per (code,unit). */
function mergeObsStats(chunks: QualityPassport[]): ObservationValueStat[] {
  return poolStats(chunks.flatMap((p) => p.profile?.observation_value_stats ?? []));
}

/** Merge per-chunk stratified stats into one distribution per (code,unit,stratum). */
function mergeObsStatsStratified(chunks: QualityPassport[]): ObservationValueStat[] {
  return poolStats(chunks.flatMap((p) => p.profile?.observation_value_stats_stratified ?? []));
}

export function aggregateChunks(chunks: QualityPassport[], opts: AggregateOpts): QualityPassport {
  const byId = new Map<string, TrustCheck>();
  for (const p of chunks) {
    for (const c of p.checks) {
      const acc = byId.get(c.check_id);
      if (!acc) {
        // Clone so we never mutate the source chunk's check.
        byId.set(c.check_id, {
          ...c,
          applicable: c.applicable,
          violations: c.violations,
          violation_details: [...(c.violation_details ?? [])].slice(0, DETAIL_CAP),
        });
      } else {
        acc.applicable += c.applicable;
        acc.violations += c.violations;
        if (acc.violation_details!.length < DETAIL_CAP) {
          acc.violation_details = [
            ...acc.violation_details!,
            ...(c.violation_details ?? []),
          ].slice(0, DETAIL_CAP);
        }
        // A check skipped in every chunk stays skipped; if it ran anywhere,
        // clear the skipped flag so it is scored.
        acc.skipped = acc.skipped && c.skipped;
      }
    }
  }

  // Replace reference integrity with the global (cross-chunk) computation.
  const refOverride = opts.referenceIntegrity;
  if (refOverride) {
    const ref = byId.get("conformance.reference_integrity");
    if (ref) {
      ref.applicable = refOverride.applicable;
      ref.violations = refOverride.violations;
      ref.violation_details = refOverride.violation_details.slice(0, DETAIL_CAP);
    }
  }

  const checks: TrustCheck[] = [...byId.values()].map((c) => {
    const frac = c.applicable > 0 ? c.violations / c.applicable : 0;
    const result: CheckResultValue =
      c.applicable <= 0 ? "NA" : frac > c.threshold ? "FAIL" : "PASS";
    return { ...c, violation_fraction: frac, result };
  });

  const categoryScores: Record<string, number | null> = {};
  for (const cat of CATEGORIES) {
    const assessed = checks.filter((c) => c.category === cat && c.result !== "NA");
    categoryScores[cat] = assessed.length
      ? (100 * assessed.filter((c) => c.result === "PASS").length) / assessed.length
      : null;
  }
  const assessedAll = checks.filter((c) => c.result !== "NA");
  const overall = assessedAll.length
    ? (100 * assessedAll.filter((c) => c.result === "PASS").length) / assessedAll.length
    : 100;

  const provenancePresent = Boolean(
    (opts.provenance.source_system as string)?.trim?.() &&
      (opts.provenance.extraction_time as string)?.trim?.(),
  );
  const blockers = checks
    .filter((c) => c.critical && c.result === "FAIL")
    .map(
      (c) =>
        `${c.check_id}: ${c.violations}/${c.applicable} violating ` +
        `(${(c.violation_fraction * 100).toFixed(1)}%) — ${c.recommendation}`.trim(),
    );
  const belowCategory = CATEGORIES.some(
    (cat) => categoryScores[cat] !== null && (categoryScores[cat] as number) < CATEGORY_MIN_RATE,
  );

  let decision: Decision;
  if (blockers.length) decision = "BLOCK";
  else if (overall < PASS_MIN_RATE || belowCategory || !provenancePresent) decision = "CONDITIONAL_PASS";
  else decision = "PASS";

  const [approved, notApproved] = fitness(decision, provenancePresent);

  // Per-dimension scorecard (DAMA/ISO 25012) from the aggregated checks.
  const scorecard: Record<string, DimensionScore> = {};
  for (const dim of DIMENSIONS) {
    const dchecks = checks.filter((c) => c.dimension === dim);
    if (!dchecks.length) continue;
    const assessed = dchecks.filter((c) => c.result !== "NA");
    const passed = assessed.filter((c) => c.result === "PASS").length;
    const score = assessed.length ? (100 * passed) / assessed.length : null;
    scorecard[dim] = {
      score,
      grade: gradeFor(score),
      checks_assessed: assessed.length,
      checks_passed: passed,
    };
  }

  // Per-phase verdicts (mirror engine._phase_report) over the aggregated checks.
  const phasesReport: Record<string, PhaseVerdict> = {};
  for (const ph of [...new Set(checks.map((c) => c.phase).filter(Boolean))] as string[]) {
    const pchecks = checks.filter((c) => c.phase === ph);
    const assessed = pchecks.filter((c) => c.result !== "NA");
    const passed = assessed.filter((c) => c.result === "PASS").length;
    const score = assessed.length ? (100 * passed) / assessed.length : null;
    const pblockers = pchecks
      .filter((c) => c.critical && c.result === "FAIL")
      .map((c) => c.check_id);
    const pdecision: Decision = pblockers.length
      ? "BLOCK"
      : score !== null && score < PASS_MIN_RATE
        ? "CONDITIONAL_PASS"
        : "PASS";
    phasesReport[ph] = {
      decision: pdecision,
      score,
      checks_assessed: assessed.length,
      checks_passed: passed,
      blockers: pblockers,
    };
  }

  const overallGrade = assessedAll.length ? gradeFor(overall) : null;

  // Merge descriptive profile counts (sum) across chunks; value distributions
  // are merged separately via mergeObsStats so the scan shows clinical shapes.
  const resourceCounts: Record<string, number> = {};
  const codeSystems: Record<string, number> = {};
  const genders: Record<string, number> = {};
  for (const p of chunks) {
    for (const [k, v] of Object.entries(p.profile?.resource_counts ?? {})) resourceCounts[k] = (resourceCounts[k] ?? 0) + v;
    for (const [k, v] of Object.entries(p.profile?.code_system_distribution ?? {})) codeSystems[k] = (codeSystems[k] ?? 0) + v;
    for (const [k, v] of Object.entries(p.profile?.patient_gender_distribution ?? {})) genders[k] = (genders[k] ?? 0) + v;
  }

  const first = chunks[0];
  return {
    dataset_id: opts.datasetId,
    source_types: opts.sourceTypes ?? ["fhir"],
    decision,
    overall_score: overall,
    category_scores: categoryScores,
    checks,
    violations: checks.flatMap((c) => c.violation_details ?? []),
    skipped_checks: {},
    framework_versions: first?.framework_versions ?? {},
    auditability: {
      provenance_present: provenancePresent,
      aggregated_chunks: opts.chunkCount,
      note: refOverride
        ? "Server-wide scan: per-check counts summed across chunks; reference integrity computed globally over the whole scanned set."
        : "Server-wide scan: per-check counts summed across chunks.",
      resource_type_thresholds: "not recomputed client-side",
    },
    blockers,
    approved_for: approved,
    not_approved_for: notApproved,
    provenance: opts.provenance,
    generated_at: new Date().toISOString(),
    privacy_processing_allowed: decision !== "BLOCK",
    resource_count: opts.resourceCount,
    config_profile: "auto",
    framework: first?.framework ?? "",
    profile: {
      total_resources: opts.resourceCount,
      resource_counts: resourceCounts,
      code_system_distribution: codeSystems,
      patient_gender_distribution: genders,
      observation_value_stats: mergeObsStats(chunks),
      observation_value_stats_stratified: mergeObsStatsStratified(chunks),
    },
    phases: phasesReport,
    scorecard,
    overall_grade: overallGrade,
    fitness: fitnessStatement(decision, overall, overallGrade, opts.intendedUse),
    report: "",
  };
}
