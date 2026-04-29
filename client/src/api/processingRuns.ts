/**
 * Processing runs — history of de-identification operations with scoring.
 */

import { fetchApi } from "./client";

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
}

export interface ProcessingRunListResponse {
  runs: ProcessingRun[];
  total: number;
}

export interface ProcessingRunStats {
  total_runs: number;
  scored_runs?: number;
  avg_composite: number | null;
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

/** GET /v1/processing-runs/stats — aggregate statistics. */
export async function getProcessingRunStats(): Promise<ProcessingRunStats> {
  return fetchApi<ProcessingRunStats>("/v1/processing-runs/stats");
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
