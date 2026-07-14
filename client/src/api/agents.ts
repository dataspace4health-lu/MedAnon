/**
 * AI Agent API client, status, config generation, PII detection,
 * rule explanation (SSE), and compliance analysis.
 */

import { fetchApi, getAuthHeaders } from "./client";

export interface PiiEnforcementStatus {
  configured: boolean;
  model: string;
  api_base: string;
  require_local: boolean;
  local_verified: boolean;
  detail: string;
}

export interface AgentStatus {
  enabled: boolean;
  provider: string;
  model: string;
  api_base: string;
  circuit_breaker: Record<string, unknown>;
  cache_size: number;
  pii_enforcement?: PiiEnforcementStatus;
}

export interface ConfigGenRequest {
  prompt: string;
  regulation?: string;
}

export interface ConfigGenResponse {
  yaml: string;
  valid: boolean;
  validation_error: string;
  source: string;
}

export interface PiiDetection {
  resource_id: string;
  resource_type: string;
  field_path: string;
  type: string;
  evidence: string;
  confidence: number;
  severity: string;
  source: string;
}

export interface PiiDetectionResponse {
  detections: PiiDetection[];
  summary: { total: number; critical: number; high: number; medium: number };
  layers_used: string[];
}

export interface ComplianceResponse {
  regulation: string;
  compliance_score: number;
  gaps: Array<{
    requirement: string;
    description: string;
    severity: string;
  }>;
  excess: Array<{ rule_match: string; reason: string }>;
  recommendations: Array<{
    priority: number;
    action: string;
    rule: Record<string, unknown>;
    reason: string;
  }>;
  source?: string;
}

export async function getAgentStatus(): Promise<AgentStatus> {
  return fetchApi<AgentStatus>("/v1/ai/status");
}

export async function generateConfig(
  req: ConfigGenRequest,
): Promise<ConfigGenResponse> {
  return fetchApi<ConfigGenResponse>("/v1/ai/generate-config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
}

export async function detectPii(
  resources: Record<string, unknown>[],
  useAi: boolean = true,
  /** Shortest string value to scan. Default 15 targets free-text; pass 1 to
   * scan every string field (short SSN/phone/name in structured fields). */
  minFieldLen: number = 15,
): Promise<PiiDetectionResponse> {
  return fetchApi<PiiDetectionResponse>("/v1/ai/detect-pii", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resources, use_ai: useAi, min_field_len: minFieldLen }),
  });
}

export async function explainConfig(
  yamlText: string,
  regulation?: string,
): Promise<{ explanation: string }> {
  return fetchApi<{ explanation: string }>("/v1/ai/explain", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml_text: yamlText, regulation }),
  });
}

export async function* streamExplain(
  yamlText: string,
  regulation?: string,
): AsyncGenerator<string, void, undefined> {
  const response = await fetch("/api/v1/ai/explain", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...getAuthHeaders(),
    },
    body: JSON.stringify({ yaml_text: yamlText, regulation }),
  });
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 2);
      if (line.startsWith("data: ")) {
        const data = line.slice(6);
        if (data === "[DONE]") return;
        try {
          const parsed = JSON.parse(data);
          if (parsed.text) yield parsed.text;
        } catch {
          /* skip malformed SSE chunks */
        }
      }
    }
    if (done) break;
  }
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

/**
 * How structured (object) PII fields are treated when proposing rules.
 * - "values" (default): one rule per identifying leaf sub-field
 *   (Patient.name.family), keeps the FHIR skeleton, blanks only the values.
 * - "whole": one rule on the parent path (Patient.name), removes the element.
 */
export type FieldGranularity = "values" | "whole";

export interface ChatRequest {
  question: string;
  config_yaml?: string;
  history?: ChatTurn[];
  model?: string;
  /** Field-path tree from uploaded examples or server samples. Paths + types
   * only by default; carries sample values (PHI) when `include_values` is set. */
  field_context?: string;
  /** When true, `field_context` includes sample values, the backend then
   * treats the call as a PHI payload and refuses any non-local AI endpoint. */
  include_values?: boolean;
  /** Leaf-vs-whole treatment of structured PII fields. */
  granularity?: FieldGranularity;
}

/**
 * Stream a config-chat answer as SSE text chunks. Throws on a server-sent
 * `error` event so the caller can surface it.
 */
export async function* streamChat(
  req: ChatRequest,
): AsyncGenerator<string, void, undefined> {
  const response = await fetch("/api/v1/ai/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...getAuthHeaders(),
    },
    body: JSON.stringify(req),
  });
  if (!response.ok) {
    throw new Error(`Chat request failed (HTTP ${response.status})`);
  }
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 2);
      // Comment lines (": keep-alive") are ignored.
      if (line.startsWith("data: ")) {
        const data = line.slice(6);
        if (data === "[DONE]") return;
        try {
          const parsed = JSON.parse(data);
          if (parsed.error) throw new Error(parsed.error);
          if (parsed.text) yield parsed.text;
        } catch (e) {
          if (e instanceof Error && e.message) throw e;
          /* skip malformed SSE chunks */
        }
      }
    }
    if (done) break;
  }
}

export interface PiiScanResult {
  path: string;
  is_pii: boolean;
  reason: string;
  suggested_action: string;
}

interface FieldScanResponse {
  results: PiiScanResult[];
  source: "ai" | "error";
  detail: string;
}

/**
 * Ask the AI to classify every field path in `fieldContext` as PII or not, and
 * suggest an action for each PII field. Uses the dedicated /v1/ai/scan-fields
 * endpoint which returns validated, structured JSON (no fragile prose parsing).
 *
 * Throws when the backend reports `source: "error"` (AI disabled/unreachable)
 * so the caller can surface the `detail` message.
 */
export async function scanFieldsForPii(
  fieldContext: string,
  opts: {
    model?: string;
    granularity?: FieldGranularity;
    includeValues?: boolean;
    guidance?: string;
  } = {},
): Promise<PiiScanResult[]> {
  const res = await fetchApi<FieldScanResponse>("/v1/ai/scan-fields", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      field_context: fieldContext,
      model: opts.model ?? "",
      granularity: opts.granularity ?? "values",
      include_values: opts.includeValues ?? false,
      guidance: opts.guidance ?? "",
    }),
  });
  if (res.source === "error") {
    throw new Error(res.detail || "PII scan failed");
  }
  return res.results;
}

export interface FieldSketchResponse {
  sketch: string;
  types: string[];
  leaf_count: number;
  included_count: number;
  truncated: boolean;
  source: string; // "fhir" | "inline" | "error"
  detail: string;
}

/**
 * Build a compact, PHI-safe schema sketch of the selected resource types: one
 * line per distinct leaf path with its type, presence frequency, cardinality,
 * and a value digest (shape/enum in the default PHI-free mode; raw samples only
 * when `includeValues` is set). The result is a richer `field_context` string
 * to pass to {@link streamChat} / {@link scanFieldsForPii}.
 *
 * Supply `resources` to sketch uploaded examples, or `resourceTypes` to have the
 * server sample the configured source FHIR server.
 */
export async function buildFieldSketch(opts: {
  resourceTypes?: string[];
  resources?: Record<string, unknown>[];
  nPerType?: number;
  includeValues?: boolean;
}): Promise<FieldSketchResponse> {
  return fetchApi<FieldSketchResponse>("/v1/ai/field-sketch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      resource_types: opts.resourceTypes ?? [],
      resources: opts.resources ?? [],
      n_per_type: opts.nPerType ?? 25,
      include_values: opts.includeValues ?? false,
    }),
  });
}

export async function analyseCompliance(
  yamlText: string,
  regulation: string,
): Promise<ComplianceResponse> {
  return fetchApi<ComplianceResponse>("/v1/ai/compliance", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml_text: yamlText, regulation }),
  });
}
