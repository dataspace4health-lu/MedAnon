/**
 * Upload de-identified resources to a target FHIR server.
 */

import { getAuthHeaders } from "./client";

export interface UploadToTargetResult {
  uploaded: number;
  errors: number;
  total: number;
  results?: Array<{
    resourceType: string;
    source_id: string | null;
    server_id: string | null;
    success: boolean;
    error: string | null;
  }>;
}

/** POST /api/v1/jobs/:jobId/upload-to-target, upload completed job results to target FHIR server. */
export async function uploadJobToTarget(
  jobId: string,
  targetUrl?: string,
  targetToken?: string,
): Promise<UploadToTargetResult> {
  const params = new URLSearchParams();
  if (targetUrl) params.set("target_url", targetUrl);
  if (targetToken) params.set("target_token", targetToken);
  const qs = params.toString();
  const response = await fetch(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/upload-to-target${qs ? `?${qs}` : ""}`,
    { method: "POST", headers: getAuthHeaders() },
  );
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `uploadJobToTarget failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<UploadToTargetResult>;
}

/** POST /api/v1/upload-to-target, upload already-de-identified resources to target FHIR server. */
export async function uploadToTarget(
  resources: Record<string, unknown>[],
  targetServerUrl?: string,
  targetToken?: string,
): Promise<UploadToTargetResult> {
  const body: Record<string, unknown> = { resources };
  if (targetServerUrl) body.target_server_url = targetServerUrl;
  if (targetToken) body.target_token = targetToken;
  const response = await fetch("/api/v1/upload-to-target", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `uploadToTarget failed (${response.status}): ${b?.detail ?? response.statusText}`,
    );
  }
  return response.json() as Promise<UploadToTargetResult>;
}
