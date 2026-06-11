/**
 * FHIR resource processing — raw, batch, $everything.
 */

import { getAuthHeaders } from "./client";
import { streamNdjson } from "./streaming";
import type { StreamLine } from "./types";

export interface PiiLeakInfo {
  leaked: true;
  identifier_risk_hits: number;
  text_risk_hits: number;
  resources_affected: number;
  message: string;
  remediation: string;
}

export interface ProcessRawResult {
  text: string;
  piiLeak: PiiLeakInfo | null;
}

/**
 * POST /api/v1/process/raw -- black-box endpoint for any FHIR format.
 *
 * Returns { text, piiLeak } where piiLeak is non-null when the backend
 * detected HIPAA-sensitive fields not covered by the config profile.
 */
export async function processRaw(
  content: string,
  outputFormat: string = "json",
  configProfile: string = "auto",
): Promise<ProcessRawResult> {
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

  // 422 with pii_leak_detected code = output blocked by PII gate.
  // Treat it as a structured result (not a thrown error) so the UI can
  // show the blocked state and remediation banner.
  if (response.status === 422) {
    try {
      const body = await response.json();
      if (body?.detail?.code === "pii_leak_detected") {
        return { text: "", piiLeak: body.detail.pii_leak as PiiLeakInfo };
      }
      // Other 422 (e.g. invalid input) — rethrow as normal error
      throw new Error(`processRaw failed (422): ${body?.detail ?? response.statusText}`);
    } catch (e) {
      if (e instanceof Error && e.message.startsWith("processRaw")) throw e;
      throw new Error(`processRaw failed (422): ${response.statusText}`);
    }
  }

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

  const raw = await response.text();
  let text = raw;

  if (outputFormat === "json") {
    try {
      text = JSON.stringify(JSON.parse(raw), null, 2);
    } catch { /* leave as-is */ }
  }

  return { text, piiLeak: null };
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
