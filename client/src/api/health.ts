/**
 * Health & readiness probes.
 */

import { fetchApi } from "./client";
import { ApiError } from "./types";
import type { HealthResponse, ReadyResponse } from "./types";

/** GET /api/health -- liveness probe. */
export function health(): Promise<HealthResponse> {
  return fetchApi<HealthResponse>("/health");
}

function isReadyResponse(body: unknown): body is ReadyResponse {
  return (
    typeof body === "object" &&
    body !== null &&
    typeof (body as Record<string, unknown>).ready === "boolean"
  );
}

/**
 * GET /api/ready -- readiness probe.
 *
 * A 503 is a *meaningful* response here, not a transport failure: the backend
 * answers 503 with the full `checks` body whenever a critical upstream is
 * down. Letting `fetchApi` throw would discard that body and blind the status
 * dashboard at precisely the moment a dependency has failed, so unwrap it.
 */
export async function ready(): Promise<ReadyResponse> {
  try {
    return await fetchApi<ReadyResponse>("/ready");
  } catch (err) {
    if (err instanceof ApiError && err.status === 503 && isReadyResponse(err.body)) {
      return err.body;
    }
    throw err;
  }
}
