/**
 * Health & readiness probes.
 */

import { fetchApi } from "./client";
import type { HealthResponse, ReadyResponse } from "./types";

/** GET /api/health -- liveness probe. */
export function health(): Promise<HealthResponse> {
  return fetchApi<HealthResponse>("/health");
}

/** GET /api/ready -- readiness probe. */
export function ready(): Promise<ReadyResponse> {
  return fetchApi<ReadyResponse>("/ready");
}
