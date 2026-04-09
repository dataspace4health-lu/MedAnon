/**
 * Async job queue — submit, poll, cancel, reprocess.
 */

import { getAuthHeaders } from "./client";

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
  phase: "queued" | "fetching" | "processing" | "done";
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
  batch_privacy?: Record<string, unknown> | null;
  config_profile?: string;
}

/** POST /api/v1/jobs/bulk-export — queue a bulk-export job, returns 202. */
export async function submitBulkExportJob(params: {
  server_url?: string;
  level?: string;
  resource_type?: string;
  type_filter?: string;
  since?: string;
  token?: string;
  timeout?: number;
  config_profile?: string;
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

/** GET /api/v1/jobs/:jobId — poll job status. */
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

/** POST /api/v1/jobs/cohort — queue a cohort export job, returns 202. */
export async function submitCohortJob(params: {
  server_url?: string;
  search_type: string;
  search_params?: Record<string, unknown>;
  everything_params?: Record<string, unknown>;
  token?: string;
  timeout?: number;
  config_profile?: string;
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

/** POST /api/v1/jobs/batch-patient-export — queue a batch patient export job, returns 202. */
export async function submitBatchPatientExportJob(params: {
  patient_ids: string[];
  patient_names?: Record<string, string>;
  config_profile?: string;
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

/** GET /api/v1/jobs/:jobId/result — download completed NDJSON result as a Blob. */
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

/** DELETE /api/v1/jobs/:jobId — cancel a pending or running job. */
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

/** POST /api/v1/jobs/:jobId/reprocess — re-process with a different config profile. */
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

/** POST /api/v1/jobs/bulk-import — queue an async upload job, returns 202.
 *  Source job result is uploaded to the target FHIR server in the background.
 *  Poll getJobStatus; progress is reflected in job.processed / job.staged_count.
 */
export async function submitBulkImport(params: {
  job_id?: string;
  ndjson_path?: string;
  target_url?: string;
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

/** GET /api/v1/jobs — list jobs with optional filters. */
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

/** GET /api/v1/jobs/:jobId/score — retrieve cached score for a job. */
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

/** POST /api/v1/jobs/:jobId/score — trigger on-demand scoring for a completed job. */
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
