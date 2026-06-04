/**
 * Config profile management — list, get, create, update, delete.
 */

import { fetchApi, getAuthHeaders } from "./client";

export interface ConfigMeta {
  name: string;
  description: string;
  created_at: string;
  is_system: boolean;
}

export interface ConfigRule {
  match: string;
  action: string;
  params?: Record<string, unknown>;
  name?: string;
}

export interface ConfigCreateBody {
  name: string;
  description?: string;
  rules: ConfigRule[];
  general?: Record<string, unknown>;
}

export interface ConfigUpdateBody {
  description?: string;
  rules: ConfigRule[];
  general?: Record<string, unknown>;
}

/** GET /api/v1/configs — list all config profiles. */
export async function listConfigs(): Promise<ConfigMeta[]> {
  const data = await fetchApi<{ configs: ConfigMeta[] }>("/v1/configs", { cache: "no-store" });
  return data.configs;
}

/** GET /api/v1/configs/:name — fetch raw YAML for a named config. */
export async function getConfigYaml(name: string): Promise<string> {
  const response = await fetch(`/api/v1/configs/${encodeURIComponent(name)}`, {
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      `getConfigYaml failed (${response.status}): ${body?.detail ?? response.statusText}`,
    );
  }
  return response.text();
}

/** POST /api/v1/configs — create a new user-defined config. */
export async function createConfig(body: ConfigCreateBody): Promise<ConfigMeta> {
  const response = await fetch("/api/v1/configs", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `createConfig failed (${response.status}): ${b?.detail ?? response.statusText}`,
    );
  }
  const data = await response.json() as { config: ConfigMeta };
  return data.config;
}

/** PUT /api/v1/configs/:name — replace rules of a user-defined config. */
export async function updateConfig(name: string, body: ConfigUpdateBody): Promise<ConfigMeta> {
  const response = await fetch(`/api/v1/configs/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `updateConfig failed (${response.status}): ${b?.detail ?? response.statusText}`,
    );
  }
  const data = await response.json() as { config: ConfigMeta };
  return data.config;
}

/** DELETE /api/v1/configs/:name — delete a user-defined config. */
export async function deleteConfig(name: string): Promise<void> {
  const response = await fetch(`/api/v1/configs/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `deleteConfig failed (${response.status}): ${b?.detail ?? response.statusText}`,
    );
  }
}
