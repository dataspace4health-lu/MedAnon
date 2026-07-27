/**
 * Deterministic identifier classification (direct / quasi / non).
 *
 * The backend (POST /v1/classify-fields) is the ONLY source: it reuses the same
 * HIPAA_SENSITIVE_PATHS catalog + severity grading the structural output gate
 * enforces, so the Explorer's per-field labels match what the engine actually
 * blocks. The client deliberately keeps no second heuristic - a divergent local
 * copy can label a field "non" (safe) while the gate blocks it, which fails
 * open. When the endpoint is unreachable the Explorer reports "unknown" rather
 * than guessing.
 */

import { fetchApi } from "./client";

export type IdentifierClass = "direct" | "quasi" | "non";

/** A path whose class could not be obtained from the backend. Never treat this
 * as safe: it means the authoritative classifier did not answer. */
export type ResolvedClass = IdentifierClass | "unknown";

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
