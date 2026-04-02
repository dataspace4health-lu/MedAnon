/**
 * Analytics — risk assessment and synthetic data generation.
 */

import { getAuthHeaders } from "./client";
import type { RiskReport } from "./types";

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
