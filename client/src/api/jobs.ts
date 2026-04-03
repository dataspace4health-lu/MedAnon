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
