/**
 * Connection-aware routing seam for the source/target FHIR data layer.
 *
 * The Settings page lets the user pick an **Active Source** and **Active Target**
 * server. This module reads that selection (persisted by SettingsContext) and
 * dispatches each FHIR read to the right transport, mirroring the per-connection
 * dispatch the Trust Gate already does in `fhirScan.ts`:
 *
 *   - built-in         -> nginx `/fhir` / `/fhir-target`      (fetchFhir / fetchFhirTarget)
 *   - custom / override -> backend proxy by URL + token       (fetchFhirProxy)
 *   - saved backend src -> backend proxy by id (token server-side, fetchFhirProxyById)
 *
 * It is intentionally NON-React (reads localStorage) so the plain functions in
 * `api/fhir.ts` / `api/fhirTarget.ts` can stay call-compatible: they just swap
 * `fetchFhir` -> `routedFetchFhir`, etc.
 */

import {
  fetchFhir,
  fetchFhirByUrl,
  fetchFhirTarget,
  fetchFhirTargetByUrl,
  fetchFhirProxy,
  fetchFhirProxyById,
} from "./client";
import { STORAGE_KEY, SAVED_SOURCE_PREFIX } from "@/context/SettingsContext";
import type { FhirConnection } from "./fhirScan";

interface PersistedSettings {
  connections?: FhirConnection[];
  activeSourceId?: string;
  activeTargetId?: string;
}

type Route =
  | { kind: "builtin" }
  | { kind: "custom"; baseUrl: string; token?: string }
  | { kind: "saved"; sourceId: string };

// Deployment-wide defaults (from the open /v1/runtime-config), set once at app
// bootstrap. Used as the fallback when the browser has no explicit selection, and
// to correct a stale built-in selection when the bundled servers don't exist.
interface DeploymentConfig {
  active_source_id: string;
  active_target_id: string;
  builtin_fhir_enabled: boolean;
}
let _deployment: DeploymentConfig | null = null;

export function setDeploymentConfig(cfg: DeploymentConfig): void {
  _deployment = cfg;
}

export function getDeploymentConfig(): DeploymentConfig | null {
  return _deployment;
}

function readSettings(): PersistedSettings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as PersistedSettings) : {};
  } catch {
    return {};
  }
}

const BUILTIN_IDS = new Set(["source", "target"]);

/** The effective active id: browser selection, corrected against the deployment. */
function effectiveId(which: "source" | "target", browserId: string | undefined): string {
  const builtin = which; // 'source' | 'target'
  const deployId =
    which === "source"
      ? _deployment?.active_source_id
      : _deployment?.active_target_id;

  // No browser selection, or it's the bare built-in default: follow deployment.
  if (!browserId || browserId === builtin) {
    if (deployId) return deployId;
    return builtin;
  }
  // The browser picked a built-in but this deployment has none: use deployment.
  if (BUILTIN_IDS.has(browserId) && _deployment && !_deployment.builtin_fhir_enabled) {
    return deployId || browserId;
  }
  return browserId;
}

function trimBase(url: string): string {
  return url.replace(/\/+$/, "");
}

function withParams(path: string, params?: Record<string, string>): string {
  if (!params || Object.keys(params).length === 0) return path;
  return `${path}?${new URLSearchParams(params).toString()}`;
}

/** Resolve the active source (or target) id into a concrete transport route. */
function resolveRoute(which: "source" | "target"): Route {
  const s = readSettings();
  const browserId = which === "source" ? s.activeSourceId : s.activeTargetId;
  const id = effectiveId(which, browserId);

  // Saved backend servers apply to source and target alike (the proxy resolves
  // any saved server's URL + token by id, server-side).
  if (id.startsWith(SAVED_SOURCE_PREFIX)) {
    return { kind: "saved", sourceId: id.slice(SAVED_SOURCE_PREFIX.length) };
  }
  // A built-in ('source'/'target') may carry an override baseUrl+token; a custom
  // connection always has a baseUrl. Either way, route through the proxy.
  const conn = (s.connections ?? []).find((c) => c.id === id);
  if (conn?.baseUrl) {
    return { kind: "custom", baseUrl: conn.baseUrl, token: conn.token };
  }
  return { kind: "builtin" };
}

// ── Source ───────────────────────────────────────────────────────────────────

export function routedFetchFhir<T>(
  path: string,
  params?: Record<string, string>,
): Promise<T> {
  const r = resolveRoute("source");
  if (r.kind === "saved") {
    return fetchFhirProxyById<T>(r.sourceId, withParams(path, params));
  }
  if (r.kind === "custom") {
    return fetchFhirProxy<T>(`${trimBase(r.baseUrl)}${withParams(path, params)}`, r.token);
  }
  return fetchFhir<T>(path, params);
}

export function routedFetchFhirByUrl<T>(absoluteUrl: string): Promise<T> {
  const r = resolveRoute("source");
  if (r.kind === "saved") {
    return fetchFhirProxyById<T>(r.sourceId, absoluteUrl, true);
  }
  if (r.kind === "custom") {
    return fetchFhirProxy<T>(absoluteUrl, r.token);
  }
  return fetchFhirByUrl<T>(absoluteUrl);
}

// ── Target ───────────────────────────────────────────────────────────────────

export function routedFetchFhirTarget<T>(
  path: string,
  params?: Record<string, string>,
): Promise<T> {
  const r = resolveRoute("target");
  if (r.kind === "saved") {
    return fetchFhirProxyById<T>(r.sourceId, withParams(path, params));
  }
  if (r.kind === "custom") {
    return fetchFhirProxy<T>(`${trimBase(r.baseUrl)}${withParams(path, params)}`, r.token);
  }
  return fetchFhirTarget<T>(path, params);
}

export function routedFetchFhirTargetByUrl<T>(absoluteUrl: string): Promise<T> {
  const r = resolveRoute("target");
  if (r.kind === "saved") {
    return fetchFhirProxyById<T>(r.sourceId, absoluteUrl, true);
  }
  if (r.kind === "custom") {
    return fetchFhirProxy<T>(absoluteUrl, r.token);
  }
  return fetchFhirTargetByUrl<T>(absoluteUrl);
}

// ── Job prefill (source/target -> export-job params) ─────────────────────────

/** Map the Active Source selection to export-job params (follows deployment). */
export function activeSourceJobParams(
  activeSourceId: string | undefined,
  connections: FhirConnection[],
): { server_url?: string; source_id?: string; token?: string } {
  const id = effectiveId("source", activeSourceId);
  if (id.startsWith(SAVED_SOURCE_PREFIX)) {
    return { source_id: id.slice(SAVED_SOURCE_PREFIX.length) };
  }
  const conn = connections.find((c) => c.id === id);
  if (conn?.baseUrl) return { server_url: conn.baseUrl, token: conn.token };
  return {}; // built-in nginx source -> server uses FHIR_SOURCE_URL default
}

/** Map the Active Target selection to export/upload params (follows deployment). */
export function activeTargetJobParams(
  activeTargetId: string | undefined,
  connections: FhirConnection[],
): { target_url?: string; target_token?: string; target_id?: string } {
  const id = effectiveId("target", activeTargetId);
  if (id.startsWith(SAVED_SOURCE_PREFIX)) {
    return { target_id: id.slice(SAVED_SOURCE_PREFIX.length) };
  }
  const conn = connections.find((c) => c.id === id);
  if (conn?.baseUrl) return { target_url: conn.baseUrl, target_token: conn.token };
  return {}; // built-in target -> uses FHIR_TARGET_URL default
}
