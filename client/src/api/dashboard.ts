/**
 * Dashboard BFF consumer.
 *
 * Wraps ``GET /v1/dashboard/summary``, which aggregates four upstream calls
 * (health, readiness, recent jobs, processing-run stats) into a single
 * round-trip, eliminating the request waterfall the SPA used to incur on
 * dashboard mount.
 */

import { fetchApi } from "./client";

export interface HealthSnapshot {
  status: string;
  version?: string | null;
}

export interface ReadinessSnapshot {
  status: string;
  checks: Record<string, string>;
}

export interface JobSummary {
  id: string;
  type: string;
  status: string;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ProcessingRunStats {
  total_runs?: number;
  total_resources?: number;
  total_errors?: number;
  avg_duration_ms?: number;
  [key: string]: unknown;
}

export interface DashboardSummary {
  health: HealthSnapshot;
  ready: ReadinessSnapshot;
  recent_jobs: JobSummary[];
  processing_runs: ProcessingRunStats | null;
  generated_at: string;
}

/** Fetch one consolidated dashboard payload. */
export function getDashboardSummary(
  limit = 5,
  signal?: AbortSignal,
): Promise<DashboardSummary> {
  return fetchApi<DashboardSummary>(
    `/v1/dashboard/summary?limit=${encodeURIComponent(limit)}`,
    { signal },
  );
}
