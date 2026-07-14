/**
 * Analytics, risk assessment and synthetic data generation.
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

// ---------------------------------------------------------------------------
// Privacy-risk (D7.2 §5.5.7) + Synthetic Data Passport (§5.4/§5.5)
// ---------------------------------------------------------------------------

export interface PrivacyRiskReport {
  re_identification: RiskReport;
  distance: {
    computed: boolean;
    reason?: string;
    dcr_mean?: number;
    dcr_min?: number;
    nndr_mean?: number;
    tau?: number;
    tau_dcr_share?: number;
    records_at_or_below_tau?: number;
    synthetic_records?: number;
    exact_duplicates?: number;
  };
  attribute_inference: {
    computed: boolean;
    reason?: string;
    attribute_inference?: Record<string, number | { na?: string; error?: string }>;
  };
  meta: {
    mode: "deidentified" | "synthetic";
    quasi_identifiers: string[];
    sensitive_attributes: string[];
  };
}

export interface SyntheticPassport {
  passport_version: string;
  generated_at: string;
  record_counts: { real: number; synthetic: number };
  fidelity: {
    per_column: Record<string, number>;
    mean_fidelity: number;
    method: string;
    grade: string;
  };
  privacy: PrivacyRiskReport;
  differential_privacy: Record<string, unknown> | null;
  verdict: {
    privacy_pass: boolean;
    fidelity_grade: string;
    overall: "release" | "review" | "reject";
  };
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail: string;
    try {
      const b = await response.json();
      detail = (b as { detail?: string })?.detail ?? response.statusText;
    } catch {
      detail = response.statusText;
    }
    throw new Error(`${path} failed (${response.status}): ${detail}`);
  }
  return response.json() as Promise<T>;
}

/**
 * POST /api/v1/analyse/privacy-risk, full privacy-risk report (re-identification
 * + inference + record-level distance). Supply *synthetic* to compute the
 * DCR/NNDR/attribute-inference metrics against *resources*.
 */
export async function analysePrivacyRisk(
  resources: object[],
  synthetic?: object[],
  privacyModel?: object,
): Promise<PrivacyRiskReport> {
  return postJson<PrivacyRiskReport>("/v1/analyse/privacy-risk", {
    resources,
    synthetic,
    privacy_model: privacyModel,
  });
}

/**
 * POST /api/v1/synthetic/passport, fidelity + privacy passport with a graded
 * release/review/reject verdict for a synthetic dataset vs the real source.
 */
export async function synthesizePassport(
  real: object[],
  synthetic: object[],
  privacyModel?: object,
): Promise<SyntheticPassport> {
  return postJson<SyntheticPassport>("/v1/synthetic/passport", {
    real,
    synthetic,
    privacy_model: privacyModel,
  });
}

/** Parse NDJSON / JSON array / FHIR Bundle / single resource into a resource array. */
export function parseResourceArray(text: string): object[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  // Try a single JSON value first (array, Bundle, or one resource).
  try {
    const parsed = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return parsed as object[];
    if (parsed && typeof parsed === "object") {
      const obj = parsed as Record<string, unknown>;
      if (obj.resourceType === "Bundle" && Array.isArray(obj.entry)) {
        return (obj.entry as Array<{ resource?: object }>)
          .map((e) => e.resource)
          .filter((r): r is object => !!r);
      }
      return [parsed as object];
    }
  } catch {
    // Fall through to NDJSON (one JSON object per line).
  }
  const out: object[] = [];
  for (const line of trimmed.split("\n")) {
    const s = line.trim();
    if (!s) continue;
    try {
      out.push(JSON.parse(s) as object);
    } catch {
      /* skip unparseable lines */
    }
  }
  return out;
}
