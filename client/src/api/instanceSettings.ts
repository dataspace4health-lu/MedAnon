/**
 * Deployment-wide instance settings + open runtime config.
 *
 * - `getRuntimeConfig()` is an OPEN endpoint (no auth): the small, non-secret
 *   slice every user's FHIR routing needs, the active source/target ids and
 *   whether the built-in (bundled) FHIR servers exist in this deployment.
 * - `getSettings()` / `updateSettings()` are admin-only (`/v1/settings`): the full
 *   instance-settings row, including setting the deployment-wide active source/target.
 */

import { fetchApi } from "./client";

export interface RuntimeConfig {
  active_source_id: string;
  active_target_id: string;
  builtin_fhir_enabled: boolean;
}

export interface InstanceSettings extends RuntimeConfig {
  config_profile: string;
  fhir_page_size: number;
  dataset_id: string;
  source_system: string;
  scan_max_resources: number;
  updated_at?: string | null;
  updated_by?: string | null;
}

export async function getRuntimeConfig(): Promise<RuntimeConfig> {
  return fetchApi<RuntimeConfig>("/v1/runtime-config");
}

export async function getSettings(): Promise<InstanceSettings> {
  return fetchApi<InstanceSettings>("/v1/settings");
}

export async function updateSettings(
  patch: Partial<
    Pick<
      InstanceSettings,
      | "active_source_id"
      | "active_target_id"
      | "config_profile"
      | "fhir_page_size"
      | "dataset_id"
      | "source_system"
      | "scan_max_resources"
    >
  >,
): Promise<InstanceSettings> {
  return fetchApi<InstanceSettings>("/v1/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}
