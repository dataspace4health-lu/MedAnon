/**
 * Deterministic identifier classification (direct / quasi / non).
 *
 * Authoritative source lives on the backend (POST /v1/classify-fields): it
 * reuses the same HIPAA_SENSITIVE_PATHS catalog + severity grading the
 * structural output gate enforces, so the Explorer's per-field labels match
 * what the engine actually blocks. The client keeps only a lightweight
 * heuristic as an offline fallback.
 */

import { fetchApi } from "./client";

export type IdentifierClass = "direct" | "quasi" | "non";

/** Classify FHIR leaf paths on the backend. Paths may carry `.where()`/`[i]`;
 * the server strips them (and reads the `.where(url=…)` predicate so race /
 * ethnicity extensions classify correctly). Returns `{path: class}`. */
export async function classifyFields(
  resourceType: string,
  paths: string[],
): Promise<Record<string, IdentifierClass>> {
  const res = await fetchApi<{ classes: Record<string, IdentifierClass> }>(
    "/v1/classify-fields",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resource_type: resourceType, paths }),
    },
  );
  return res.classes;
}
