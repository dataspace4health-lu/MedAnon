/**
 * FHIR resource processing — raw, batch, $everything.
 */

import { getAuthHeaders } from "./client";
import { streamNdjson } from "./streaming";
import type { StreamLine } from "./types";

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
