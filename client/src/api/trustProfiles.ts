/**
 * Trust Gate audit-profile management, list, get, create, update, delete.
 *
 * A trust profile is a reusable selection of audit phases (+ sector targets,
 * thresholds, and a declared intended use) that tunes what the pre-privacy Trust
 * Gate measures. Stored by the anonymizer at /api/v1/trust-profiles and forwarded
 * to the gate per request.
 */

import { fetchApi, getAuthHeaders } from "./client";

export interface SectorTarget {
  id?: string;
  resource_types?: string[];
  code_systems?: string[];
  /** FHIRPath cohort predicate, e.g. "Patient.gender = 'female'". */
  fhirpath?: string;
}

export interface TrustProfileMeta {
  name: string;
  description: string;
  phases: string[];
  thresholds: Record<string, number>;
  targets: SectorTarget[];
  intended_use: string;
  is_system: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface TrustProfileCreateBody {
  name: string;
  description?: string;
  phases: string[];
  thresholds?: Record<string, number>;
  targets?: SectorTarget[];
  intended_use?: string;
}

export interface TrustProfileUpdateBody {
  description?: string;
  phases?: string[];
  thresholds?: Record<string, number>;
  targets?: SectorTarget[];
  intended_use?: string;
}

/** GET /api/v1/trust-profiles/_phases, the catalog of selectable phases. */
export async function listPhases(): Promise<string[]> {
  const data = await fetchApi<{ phases: string[] }>("/v1/trust-profiles/_phases", {
    cache: "no-store",
  });
  return data.phases;
}

/** GET /api/v1/trust-profiles, list all profiles (system + user-defined). */
export async function listTrustProfiles(): Promise<TrustProfileMeta[]> {
  const data = await fetchApi<{ profiles: TrustProfileMeta[] }>("/v1/trust-profiles", {
    cache: "no-store",
  });
  return data.profiles;
}

/** POST /api/v1/trust-profiles, create a user-defined profile. */
export async function createTrustProfile(
  body: TrustProfileCreateBody,
): Promise<TrustProfileMeta> {
  const response = await fetch("/api/v1/trust-profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `createTrustProfile failed (${response.status}): ${b?.detail ?? response.statusText}`,
    );
  }
  return (await response.json() as { profile: TrustProfileMeta }).profile;
}

/** PUT /api/v1/trust-profiles/:name, update a user-defined profile. */
export async function updateTrustProfile(
  name: string,
  body: TrustProfileUpdateBody,
): Promise<TrustProfileMeta> {
  const response = await fetch(`/api/v1/trust-profiles/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `updateTrustProfile failed (${response.status}): ${b?.detail ?? response.statusText}`,
    );
  }
  return (await response.json() as { profile: TrustProfileMeta }).profile;
}

/** DELETE /api/v1/trust-profiles/:name, delete a user-defined profile. */
export async function deleteTrustProfile(name: string): Promise<void> {
  const response = await fetch(`/api/v1/trust-profiles/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `deleteTrustProfile failed (${response.status}): ${b?.detail ?? response.statusText}`,
    );
  }
}
