/**
 * TypeScript interfaces for all API responses used by the SPE FHIR BlackBox
 * React frontend. Covers MedAnon endpoints and FHIR search results.
 */

// ---------------------------------------------------------------------------
// FHIR search result types
// ---------------------------------------------------------------------------

export interface PatientSummary {
  id: string;
  name: string;
  birthDate: string;
  gender: string;
}

export interface ConditionRow {
  condition_id: string;
  code: string;
  display: string;
  clinical_status: string;
  patient_id: string;
  patient_name: string;
  patient_birth_date: string;
  patient_gender: string;
}

// ---------------------------------------------------------------------------
// Health / readiness
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status: string;
  version?: string;
}

export interface ReadyResponse {
  ready: boolean;
  checks?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Risk report
// ---------------------------------------------------------------------------

export interface RiskSummary {
  min_k: number;
  max_k?: number;
  mean_k?: number;
  prosecutor_risk: number;
  journalist_risk: number;
  marketer_risk: number;
  total_records: number;
  total_groups: number;
  singleton_groups: number;
  records_with_missing_qi: number;
  risk_level: "low" | "medium" | "high" | "critical";
  qi_fields?: string[];
}

export interface RiskGroup {
  qi: {
    gender: string;
    birth_year: string;
    zip_prefix: string;
  };
  size: number;
  k: number;
  risk_1_over_k: number;
  weight: number;
}

export interface LDiversityComputed {
  computed: true;
  min_l: number;
  max_l: number;
  violations: number;
  details: Array<{
    group: {
      gender: string;
      birth_year: string;
      zip_prefix: string;
    };
    l_value: number;
    distinct_codes: number;
  }>;
}

export interface LDiversityNotComputed {
  computed: false;
  reason: string;
}

export type LDiversity = LDiversityComputed | LDiversityNotComputed;

export interface RiskMeta {
  computed_at: string;
  input_lines: number;
  patient_lines: number;
  condition_lines: number;
}

export interface RiskReport {
  summary: RiskSummary;
  l_diversity: LDiversity;
  groups: RiskGroup[];
  warnings: string[];
  meta: RiskMeta;
}

// ---------------------------------------------------------------------------
// Streaming / batch
// ---------------------------------------------------------------------------

export interface StreamLine {
  ok: boolean;
  data: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Config profiles
// ---------------------------------------------------------------------------

export interface ConfigProfile {
  key: string;
  label: string;
  description: string;
}

// ---------------------------------------------------------------------------
// API errors
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  public readonly status: number;
  public readonly detail: string;
  public readonly body: unknown;

  constructor(status: number, detail: string, body?: unknown) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}
