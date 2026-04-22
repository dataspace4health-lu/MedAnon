/**
 * AI Agent API client — status, config generation, PII detection,
 * rule explanation (SSE), and compliance analysis.
 */

import { fetchApi, getAuthHeaders } from "./client";

export interface AgentStatus {
  enabled: boolean;
  provider: string;
  model: string;
  api_base: string;
  circuit_breaker: Record<string, unknown>;
  cache_size: number;
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
): Promise<PiiDetectionResponse> {
  return fetchApi<PiiDetectionResponse>("/v1/ai/detect-pii", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ resources, use_ai: useAi }),
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
