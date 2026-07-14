/**
 * Async job queue, submit, poll, cancel, reprocess.
 */

import { getAuthHeaders } from "./client";

export interface UploadErrorDetail {
  resourceType: string;
  error: string;
}

export interface JobResponse {
  job_id: string;
  type: string;
  status: "pending" | "running" | "done" | "error" | "cancelled";
  created_at: string;
  updated_at: string;
  result_path: string | null;
  error: string | null;
  processed: number;
  staged_count: number | null;
  phase: "queued" | "fetching" | "processing" | "loading" | "uploading" | "done" | string;
  config_profile: string;
  /** Number of resources that failed to upload (bulk-import jobs only). */
  upload_errors?: number;
  /** Per-resource error details, capped at 50 (bulk-import jobs only). */
  upload_error_details?: UploadErrorDetail[];
  summary: {
    total_resources: number;
    error_count: number;
    resource_type_counts: Record<string, number>;
    config_profile: string;
    completed_at: string;
    duration_sec: number;
    compressed: boolean;
    file_size_bytes: number;
  } | null;
  /**
   * Structured score-gate block report. Present only when the job was blocked
   * by the privacy gate (status "error", output deleted, download blocked).
   * Lets the UI render *what leaked* + *what to fix* instead of parsing `error`.
   */
  block_report?: BlockReport | null;
  /** Risk-driven-export: achieved k/l + suppression from the lattice solver. */
  achieved_privacy?: AchievedPrivacy | null;
  /** Risk-driven-export: the Fig-6 disclosure decision made before release. */
  disclosure?: DisclosureRecord | null;
  /** Risk-driven-export: the full Transformation Passport (D7.2 §5.5.1). */
  transformation_passport?: TransformationPassport | null;
}

export interface TransformationPassport {
  passport_version: string;
  generated_at: string;
  identification: {
    data_creator: string;
    permit_id: string | null;
    job_id: string | null;
    hash_key_id?: string;
  };
  original_dataset: Record<string, unknown> | null;
  processing: {
    config_profile: string | null;
    tools: { name: string; version: string; git_sha: string }[];
    tool_assessment?: {
      overall_status: string;
      not_approved: { name: string; version: string | null; status: string }[];
    } | null;
    privacy_model: Record<string, unknown> | null;
    generalization: {
      achieved_k?: number | null;
      achieved_l?: number | null;
      achieved_t?: number | null;
      suppressed_count?: number | null;
      suppression_rate?: number | null;
      information_loss?: number | null;
      feasible?: boolean | null;
    } | null;
    differential_privacy: Record<string, unknown> | null;
  };
  privacy_risk_assessment: Record<string, unknown> | null;
  disclosure: DisclosureRecord | null;
}

export interface AchievedPrivacy {
  achieved_k: number | null;
  achieved_l: number | null;
  suppressed_count: number | null;
  suppression_rate: number | null;
  information_loss: number | null;
  feasible: boolean | null;
  generalization_levels?: Record<string, number> | null;
}

export interface DisclosureRecord {
  decision: "approve" | "refer" | "refuse";
  checks: { rule: string; outcome: string; detail: string }[];
  permit_id?: string | null;
  escalated_by_regulated_mode?: boolean;
  decided_by?: string;
  decided_at?: string;
}

export interface BlockReport {
  blocked: boolean;
  /** true when actual PII was detected (text_risk) or coverage hard-blocked. */
  critical_pii: boolean;
  score: number;
  grade: string;
  min_required: number;
  min_grade: string;
  resources_total: number;
  profile: string;
  text_risk_hits: number;
  identifier_risk_hits: number;
  /** Exact HIPAA paths left uncovered, most frequent first. */
  leaked_fields: Array<{ path: string; resource_count: number }>;
  issues: string[];
  fixes: string[];
  /** Full plain-language message (same as `error`). */
  message: string;
}

export interface BatchPrivacy {
  risk_score: number;
  passed: boolean;
  threshold: number;
  attacker_risk: number;
  identifier_risk: number;
  config_identifier_risk: number;
  text_risk: number;
  evidence: Array<{
    check: string;
    value: number;
    details: Record<string, unknown>;
    severity: string;
  }>;
}

export interface JobScoreResponse {
  job_id: string;
  computed: boolean;
  reason?: string;
  total_scored?: number;
  pass_count?: number;
  fail_count?: number;
  error_count?: number;
  avg_composite?: number;
  min_composite?: number;
  avg_utility?: number;
  avg_quality?: number;
  batch_privacy?: BatchPrivacy | null;
  config_profile?: string;
}

/** POST /api/v1/jobs/bulk-export, queue a bulk-export job, returns 202. */
export async function submitBulkExportJob(params: {
  server_url?: string;
  level?: string;
  resource_type?: string;
  type_filter?: string;
  since?: string;
  token?: string;
  timeout?: number;
  config_profile?: string;
  /** Active data-permit id (D7.2 §4.4), scopes pseudonymisation to the permit. */
  permit_id?: string | null;
  source_id?: string | null;
  destination_id?: string | null;
}): Promise<JobResponse> {
  const response = await fetch("/api/v1/jobs/bulk-export", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(params),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `submitBulkExportJob failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/**
 * POST /api/v1/jobs/risk-driven-export, queue a k-anonymity risk-driven export.
 *
 * The one export flow that runs the full D7.2 loop: opt-out exclusion (Art 71),
 * permit-scoped pseudonymisation (§4.4), a lattice solve to a target k/l/t, and
 * a Fig-6 disclosure decision on the actual output before release. Requires a
 * config profile carrying a privacy_model (defaults to config_k_anonymity).
 */
export async function submitRiskDrivenExportJob(params: {
  server_url?: string;
  resource_type?: string;
  type_filter?: string;
  config_profile?: string;
  permit_id?: string | null;
  recipient?: string | null;
  declared_paths?: string[] | null;
  optout_ids?: string[] | null;
  source_id?: string | null;
  destination_id?: string | null;
}): Promise<JobResponse> {
  const response = await fetch("/api/v1/jobs/risk-driven-export", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(params),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `submitRiskDrivenExportJob failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/** GET /api/v1/jobs/:jobId, poll job status. */
export async function getJobStatus(jobId: string): Promise<JobResponse> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `getJobStatus failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/** POST /api/v1/jobs/cohort, queue a cohort export job, returns 202. */
export async function submitCohortJob(params: {
  server_url?: string;
  search_type: string;
  search_params?: Record<string, unknown>;
  everything_params?: Record<string, unknown>;
  token?: string;
  timeout?: number;
  config_profile?: string;
  /** Active data-permit id (D7.2 §4.4), scopes pseudonymisation to the permit. */
  permit_id?: string | null;
  source_id?: string | null;
  destination_id?: string | null;
}): Promise<JobResponse> {
  const response = await fetch("/api/v1/jobs/cohort", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(params),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `submitCohortJob failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/**
 * POST /api/v1/jobs/patient-export, queue a single-patient $everything export.
 *
 * The server re-fetches $everything itself, so the browser sends only the patient
 * id: raw PHI never leaves the server, and the request body stays far under
 * MEDANON_MAX_BODY_BYTES. The job runs the score gate before release and delivers
 * data + manifest + audit to `destination_id`.
 */
export async function submitPatientExportJob(params: {
  patient_id: string;
  server_url?: string;
  config_profile?: string;
  /** Active data-permit id (D7.2 §4.4), scopes pseudonymisation to the permit. */
  permit_id?: string | null;
  source_id?: string | null;
  destination_id?: string | null;
}): Promise<JobResponse> {
  const response = await fetch("/api/v1/jobs/patient-export", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(params),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `submitPatientExportJob failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/** POST /api/v1/jobs/batch-patient-export, queue a batch patient export job, returns 202. */
export async function submitBatchPatientExportJob(params: {
  patient_ids: string[];
  patient_names?: Record<string, string>;
  config_profile?: string;
  /** Active data-permit id (D7.2 §4.4), scopes pseudonymisation to the permit. */
  permit_id?: string | null;
  source_id?: string | null;
  destination_id?: string | null;
}): Promise<JobResponse> {
  const response = await fetch("/api/v1/jobs/batch-patient-export", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(params),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `submitBatchPatientExportJob failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/** GET /api/v1/jobs/:jobId/result, download completed NDJSON result as a Blob. */
export async function getJobResult(jobId: string): Promise<Blob> {
  const response = await fetch(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/result`,
    { headers: getAuthHeaders() },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `getJobResult failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.blob();
}

/** DELETE /api/v1/jobs/:jobId, cancel a pending or running job. */
export async function cancelJob(jobId: string): Promise<JobResponse> {
  const response = await fetch(
    `/api/v1/jobs/${encodeURIComponent(jobId)}`,
    { method: "DELETE", headers: getAuthHeaders() },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `cancelJob failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/** POST /api/v1/jobs/:jobId/reprocess, re-process with a different config profile. */
export async function reprocessJob(
  jobId: string,
  configProfile: string = "auto",
): Promise<JobResponse> {
  const params = new URLSearchParams({ config_profile: configProfile });
  const response = await fetch(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/reprocess?${params.toString()}`,
    { method: "POST", headers: getAuthHeaders() },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `reprocessJob failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/** POST /api/v1/jobs/bulk-import, queue an async upload job, returns 202.
 *  Source job result is uploaded to the target FHIR server in the background.
 *  Poll getJobStatus; progress is reflected in job.processed / job.staged_count.
 */
export async function submitBulkImport(params: {
  job_id?: string;
  ndjson_path?: string;
  target_url?: string;
  target_token?: string;
  target_id?: string;
}): Promise<JobResponse> {
  const response = await fetch("/api/v1/jobs/bulk-import", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(params),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `submitBulkImport failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse>;
}

/** GET /api/v1/jobs, list jobs with optional filters. */
export async function listJobs(params?: {
  status?: string;
  type?: string;
  limit?: number;
  offset?: number;
}): Promise<JobResponse[]> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.type) qs.set("type", params.type);
  if (params?.limit != null) qs.set("limit", String(params.limit));
  if (params?.offset != null) qs.set("offset", String(params.offset));
  const query = qs.toString();
  const response = await fetch(`/api/v1/jobs${query ? `?${query}` : ""}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `listJobs failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobResponse[]>;
}

/** GET /api/v1/jobs/:jobId/score, retrieve cached score for a job. */
export async function getJobScore(jobId: string): Promise<JobScoreResponse> {
  const response = await fetch(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/score`,
    { headers: getAuthHeaders() },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `getJobScore failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobScoreResponse>;
}

/** POST /api/v1/jobs/:jobId/score, trigger on-demand scoring for a completed job. */
export async function triggerJobScore(jobId: string): Promise<JobScoreResponse> {
  const response = await fetch(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/score`,
    { method: "POST", headers: getAuthHeaders() },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `triggerJobScore failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<JobScoreResponse>;
}

// ---------------------------------------------------------------------------
// Job detail cache (parsed result: resource counts + field/PII analysis)
// ---------------------------------------------------------------------------

export interface JobDetail {
  resource_counts: Record<string, number>;
  total_resources: number;
  pii_data: Record<string, unknown>;
  field_summary: Record<string, unknown>;
}

/**
 * GET /api/v1/jobs/:jobId/detail, load cached parsed result.
 * Throws on 404 (cache miss), callers should fall back to NDJSON parse.
 */
export async function getJobDetail(jobId: string): Promise<JobDetail> {
  const response = await fetch(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/detail`,
    { headers: getAuthHeaders() },
  );
  if (!response.ok) throw new Error(`getJobDetail failed (${response.status})`);
  return response.json() as Promise<JobDetail>;
}

/**
 * POST /api/v1/jobs/:jobId/detail, save parsed result to backend cache.
 * Fire-and-forget safe, callers should silently swallow failures.
 */
export async function saveJobDetail(jobId: string, detail: JobDetail): Promise<void> {
  await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/detail`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(detail),
  });
}
