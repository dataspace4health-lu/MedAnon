/**
 * Dataspace connectors API, saved input sources + S3 output destinations.
 *
 * Mirrors the backend endpoints under /v1:
 *
 *   GET    /v1/source-connections
 *   POST   /v1/source-connections
 *   POST   /v1/source-connections/{id}/test
 *   DELETE /v1/source-connections/{id}
 *   GET    /v1/output-destinations
 *   POST   /v1/output-destinations
 *   POST   /v1/output-destinations/{id}/test
 *   DELETE /v1/output-destinations/{id}
 *
 * Secrets (FHIR bearer tokens, S3 secret keys) are encrypted at rest and never
 * returned by the API, create forms are write-only for the secret fields.
 */

import { fetchApi } from "./client";

// ── Input sources ──────────────────────────────────────────────────────────

export type ServerRole = "source" | "target" | "both";

export interface SourceConnection {
  id: string;
  name: string;
  kind: string;
  role: ServerRole;
  server_url: string;
  has_token: boolean;
  created_at: string;
}

export interface SourceConnectionCreate {
  name: string;
  server_url: string;
  token?: string;
  kind?: string;
  role?: ServerRole;
}

export async function listSources(
  role?: ServerRole,
): Promise<SourceConnection[]> {
  const q = role ? `?role=${encodeURIComponent(role)}` : "";
  const { sources } = await fetchApi<{ sources: SourceConnection[] }>(
    `/v1/source-connections${q}`,
  );
  return sources;
}

export async function createSource(
  body: SourceConnectionCreate,
): Promise<SourceConnection> {
  return fetchApi<SourceConnection>("/v1/source-connections", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function testSource(
  id: string,
): Promise<{ ok: boolean; resource_types: number }> {
  return fetchApi(`/v1/source-connections/${encodeURIComponent(id)}/test`, {
    method: "POST",
  });
}

export async function deleteSource(id: string): Promise<void> {
  await fetchApi<void>(`/v1/source-connections/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

// ── S3 output destinations ─────────────────────────────────────────────────

export interface OutputDestination {
  id: string;
  name: string;
  endpoint: string;
  region: string | null;
  bucket: string;
  key_prefix: string;
  access_key: string;
  secure: boolean;
  path_style: boolean;
  created_at: string;
}

export interface OutputDestinationCreate {
  name: string;
  endpoint: string;
  bucket: string;
  access_key: string;
  secret_key: string;
  region?: string | null;
  key_prefix?: string;
  secure?: boolean;
  path_style?: boolean;
}

export async function listDestinations(): Promise<OutputDestination[]> {
  const { destinations } = await fetchApi<{
    destinations: OutputDestination[];
  }>("/v1/output-destinations");
  return destinations;
}

export async function createDestination(
  body: OutputDestinationCreate,
): Promise<OutputDestination> {
  return fetchApi<OutputDestination>("/v1/output-destinations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function testDestination(
  id: string,
): Promise<{ ok: boolean; bucket: string; created: boolean }> {
  return fetchApi(`/v1/output-destinations/${encodeURIComponent(id)}/test`, {
    method: "POST",
  });
}

export async function deleteDestination(id: string): Promise<void> {
  await fetchApi<void>(`/v1/output-destinations/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}
