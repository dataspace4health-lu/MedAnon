/**
 * Typed wrappers for MedAnon REST API endpoints.
 *
 * All functions target the /api prefix which is reverse-proxied to the
 * MedAnon FastAPI service (port 8000).
 */

import { fetchApi, getAuthHeaders } from "./client";
import { streamNdjson } from "./streaming";
import type {
  HealthResponse,
  ReadyResponse,
  RiskReport,
  StreamLine,
} from "./types";

// ---------------------------------------------------------------------------
// Health & readiness
// ---------------------------------------------------------------------------

/** GET /api/health -- liveness probe. */
export function health(): Promise<HealthResponse> {
  return fetchApi<HealthResponse>("/health");
}

/** GET /api/ready -- readiness probe. */
export function ready(): Promise<ReadyResponse> {
  return fetchApi<ReadyResponse>("/ready");
}

// ---------------------------------------------------------------------------
// Processing
// ---------------------------------------------------------------------------

/**
 * POST /api/v1/process/raw -- black-box endpoint for any FHIR format.
 *
 * Sends the raw body string, returns the processed output as a string
 * (JSON, XML, or NDJSON depending on `outputFormat`).
 */
export async function processRaw(
  content: string,
  outputFormat: string = "json",
  configProfile: string = "auto",
): Promise<string> {
  const params = new URLSearchParams({
    output_format: outputFormat,
    config_profile: configProfile,
  });

  const response = await fetch(`/api/v1/process/raw?${params.toString()}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeaders(),
    },
    body: content,
  });

  if (!response.ok) {
    let detail: string;
    try {
      const body = await response.json();
      detail = body?.detail ?? response.statusText;
    } catch {
      detail = response.statusText;
    }
    throw new Error(`processRaw failed (${response.status}): ${detail}`);
  }

  return response.text();
}

/**
 * POST /api/v1/process/batch -- streaming batch processing.
 *
 * Sends FHIR content (JSON, NDJSON, or XML) and streams back processed
 * resources as NDJSON lines. Each yielded object has either resource data
 * or an `error` field.
 */
export function processBatch(
  content: string,
  contentType: string = "application/json",
  configProfile: string = "auto",
  signal?: AbortSignal,
): AsyncGenerator<StreamLine, void, undefined> {
  const params = new URLSearchParams({ config_profile: configProfile });

  return streamNdjson<StreamLine>(
    `/api/v1/process/batch?${params.toString()}`,
    {
      method: "POST",
      headers: { "Content-Type": contentType },
      body: content,
    },
    signal,
  );
}

/**
 * POST /api/v1/process/everything -- fetch $everything, de-identify, stream NDJSON.
 *
 * Delegates to the server-side $everything endpoint. Streams back
 * de-identified resources one per line.
 */
export function processEverything(
  serverUrl: string,
  resourceType: string,
  resourceId: string,
  configProfile: string = "auto",
  signal?: AbortSignal,
): AsyncGenerator<StreamLine, void, undefined> {
  const params = new URLSearchParams({ config_profile: configProfile });

  return streamNdjson<StreamLine>(
    `/api/v1/process/everything?${params.toString()}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        server_url: serverUrl,
        resource_type: resourceType,
        resource_id: resourceId,
      }),
    },
    signal,
  );
}

// ---------------------------------------------------------------------------
// Async job queue
// ---------------------------------------------------------------------------

export interface JobResponse {
  job_id: string;
  type: string;
  status: "pending" | "running" | "done" | "error";
  created_at: string;
  updated_at: string;
  result_path: string | null;
  error: string | null;
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

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

/**
 * POST /api/analyse/risk -- compute re-identification risk metrics.
 *
 * Accepts de-identified FHIR content in any supported format.
 */
export async function analyseRisk(
  content: string,
  contentType: string = "application/x-ndjson",
): Promise<RiskReport> {
  const response = await fetch("/api/v1/analyse/risk", {
    method: "POST",
    headers: {
      "Content-Type": contentType,
      ...getAuthHeaders(),
    },
    body: content,
  });

  if (!response.ok) {
    let detail: string;
    try {
      const body = await response.json();
      detail = body?.detail ?? response.statusText;
    } catch {
      detail = response.statusText;
    }
    throw new Error(`analyseRisk failed (${response.status}): ${detail}`);
  }

  return response.json() as Promise<RiskReport>;
}

// ---------------------------------------------------------------------------
// Synthetic data generation
// ---------------------------------------------------------------------------

/**
 * POST /api/v1/generate/synthetic -- generate synthetic FHIR data.
 *
 * Returns the raw NDJSON response as a Blob so the caller can either
 * parse it line-by-line or offer it as a download.
 */
export async function generateSynthetic(
  content: string,
  count: number = 100,
  contentType: string = "application/x-ndjson",
  seed?: number,
): Promise<Blob> {
  const params = new URLSearchParams({ count: String(count) });
  if (seed !== undefined) {
    params.set("seed", String(seed));
  }

  const response = await fetch(
    `/api/v1/generate/synthetic?${params.toString()}`,
    {
      method: "POST",
      headers: {
        "Content-Type": contentType,
        ...getAuthHeaders(),
      },
      body: content,
    },
  );

  if (!response.ok) {
    let detail: string;
    try {
      const body = await response.json();
      detail = body?.detail ?? response.statusText;
    } catch {
      detail = response.statusText;
    }
    throw new Error(
      `generateSynthetic failed (${response.status}): ${detail}`,
    );
  }

  return response.blob();
}
