/**
 * Processing runs — history of de-identification operations with scoring.
 */

import { fetchApi } from "./client";
import type {
  DimensionScore,
  FitnessVerdict,
  PhaseVerdict,
  SectorVerdict,
} from "./trustGate";

/** One Trust Gate data-quality check (Kahn 2016 taxonomy, OHDSI-DQD scoring). */
export interface TrustCheck {
  check_id: string;
  category: "conformance" | "completeness" | "plausibility";
  subcategory: string;
  context: "verification" | "validation";
  result: "PASS" | "FAIL" | "NA";
  applicable: number;
  violations: number;
  violation_fraction: number;
  threshold: number;
  critical: boolean;
  description: string;
  recommendation: string;
  // PIQI HDQT v2.0 (ASTP/ONC 2024) — additive metadata, present on tagged checks.
  hdqt_category?: string;
  hdqt_dimension?: string;
  // Honesty mechanism: check could not run (service timeout, n-ary boundary, etc.)
  skipped?: boolean;
  skip_reason?: string;
  // Per-resource violation context (only populated on critical block checks).
  violation_details?: TrustViolationDetail[];
}

/**
 * Per-resource violation detail. The backend's `CheckResult.add_detail`
 * (services/trust-gate/src/passport.py) emits `check_id`, `resource_type`,
 * `resource_id`, `path`, and `detail`. `attribute`/`severity`/`hdqt_*` are not
 * currently populated per-detail; they remain optional for forward-compat.
 */
export interface TrustViolationDetail {
  check_id: string;
  resource_type?: string;
  resource_id?: string;
  resource_index?: number;
  /** Element / FHIRPath the violation was found at (backend field: `path`). */
  path?: string;
  detail: string;
  attribute?: string;
  hdqt_category?: string;
  hdqt_dimension?: string;
  severity?: "critical" | "warning" | "info";
}

/** Framework version tracking (PIQI HDQT v2.0 alignment). */
export interface TrustFrameworkVersions {
  kahn: string;
  hdqt: string;
  evaluation_profile: string;
}

/** Descriptive data profile (analysis support — NOT scored). */
export interface TrustProfile {
  total_resources: number;
  distinct_resource_types: number;
  resource_counts: Record<string, number>;
  code_system_distribution: Record<string, number>;
  observation_value_stats: Array<{
    code: string;
    unit: string;
    /** Human label carried from the source FHIR coding display / code.text. */
    display?: string;
    count: number;
    min: number;
    max: number;
    mean: number;
    stddev: number;
  }>;
  patient_gender_distribution: Record<string, number>;
  reference_density: number;
}

/**
 * Pre-privacy Quality Passport emitted by the Trust Gate microservice.
 * Shape mirrors `QualityPassport.to_dict()` in services/trust-gate/src/passport.py.
 */
export interface TrustPassport {
  dataset_id?: string;
  source_types?: string[];
  decision: "PASS" | "CONDITIONAL_PASS" | "BLOCK";
  overall_score: number | null;
  has_assessed?: boolean;
  category_scores: Record<string, number | null>;
  checks: TrustCheck[];
  auditability: Record<string, unknown>;
  blockers: string[];
  approved_for: string[];
  not_approved_for: string[];
  provenance?: Record<string, unknown>;
  generated_at?: string;
  framework?: string;
  config_profile?: string;
  resource_count?: number;
  privacy_processing_allowed?: boolean;
  profile?: TrustProfile;
  report?: string;
  degraded?: boolean;
  degraded_reason?: string;
  // Structured violations aggregated across all checks (PIQI HDQT v2.0).
  violations?: TrustViolationDetail[];
  // Checks that could not run: { [check_id]: { skipped: N, reason: string } }
  skipped_checks?: Record<string, { skipped: number; reason: string }>;
  // Framework version tracking.
  framework_versions?: TrustFrameworkVersions;
  // Per-dimension scorecard (DAMA/ISO 25012) + graded, purpose-bound verdict.
  scorecard?: Record<string, DimensionScore>;
  overall_grade?: string | null;
  fitness?: FitnessVerdict;
  // Selectable-suite + per-target roll-ups (empty unless requested).
  phases?: Record<string, PhaseVerdict>;
  targets?: Record<string, SectorVerdict>;
}

export interface ProcessingRun {
  id: string;
  created_at: string;
  endpoint: string;
  config_profile: string;
  resource_count: number;
  error_count: number;
  duration_ms: number;
  input_type: string;
  summary: Record<string, unknown> | null;
  score: Record<string, unknown> | null;
  trust_passport: TrustPassport | null;
}

export interface ProcessingRunListResponse {
  runs: ProcessingRun[];
  total: number;
}

export interface ProcessingRunStats {
  total_runs: number;
  scored_runs?: number;
  blocked_runs?: number;
  avg_composite: number | null;
  avg_privacy?: number | null;
  avg_utility?: number | null;
  avg_quality?: number | null;
  total_resources: number;
  runs_by_endpoint: Record<string, number>;
  runs_by_profile: Record<string, number>;
}

/** GET /v1/processing-runs — paginated list with optional filters. */
export async function listProcessingRuns(params?: {
  endpoint?: string;
  config_profile?: string;
  limit?: number;
  offset?: number;
}): Promise<ProcessingRunListResponse> {
  const qs = new URLSearchParams();
  if (params?.endpoint) qs.set("endpoint", params.endpoint);
  if (params?.config_profile) qs.set("config_profile", params.config_profile);
  if (params?.limit != null) qs.set("limit", String(params.limit));
  if (params?.offset != null) qs.set("offset", String(params.offset));
  const query = qs.toString();
  return fetchApi<ProcessingRunListResponse>(
    `/v1/processing-runs${query ? `?${query}` : ""}`,
  );
}

/** GET /v1/processing-runs/stats — aggregate statistics, optionally filtered by profile. */
export async function getProcessingRunStats(params?: {
  config_profile?: string;
}): Promise<ProcessingRunStats> {
  const qs = new URLSearchParams();
  if (params?.config_profile) qs.set("config_profile", params.config_profile);
  const query = qs.toString();
  return fetchApi<ProcessingRunStats>(`/v1/processing-runs/stats${query ? `?${query}` : ""}`);
}

/** GET /v1/processing-runs/:id — single run detail. */
export async function getProcessingRun(id: string): Promise<ProcessingRun> {
  return fetchApi<ProcessingRun>(
    `/v1/processing-runs/${encodeURIComponent(id)}`,
  );
}


/** DELETE /v1/processing-runs?days=N — purge runs older than N days. */
export async function purgeProcessingRuns(days: number = 30): Promise<{ deleted: number }> {
  return fetchApi<{ deleted: number }>(
    `/v1/processing-runs?days=${days}`,
    { method: "DELETE" },
  );
}
