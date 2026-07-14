/**
 * Data-permit governance, list, create, and drive the permit lifecycle.
 *
 * A data permit (TEHDAS2 D7.2 §2 / EHDS Arts 45-49) is the legal authorisation
 * that binds a data use: purpose, legal basis, controller, approved recipient,
 * the scope of variables allowed, and a validity window. Export jobs reference
 * an APPROVED permit by id so pseudonymisation is scoped to it (D7.2 §4.4:
 * pseudonyms must not be reused across permits). All routes are admin-only,
 * served by the anonymizer at /api/v1/permits.
 */

import { fetchApi, getAuthHeaders } from "./client";

export type PermitStatus =
  | "draft"
  | "submitted"
  | "approved"
  | "rejected"
  | "revoked";

export interface Permit {
  id: string;
  purpose: string;
  legal_basis: string;
  controller: string;
  recipient: string;
  /** FHIRPaths (or path prefixes) the permit authorises. Empty = unrestricted. */
  allowed_paths: string[];
  valid_from: string | null;
  valid_until: string | null;
  restrictions: string[];
  status: PermitStatus;
  decided_by: string;
  decision_reason: string;
  created_at: string | null;
}

export interface PermitCreateBody {
  purpose?: string;
  legal_basis?: string;
  controller?: string;
  recipient?: string;
  allowed_paths?: string[];
  valid_from?: string | null;
  valid_until?: string | null;
  restrictions?: string[];
}

async function mutate(path: string, body?: unknown): Promise<Permit> {
  const response = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...getAuthHeaders() },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) {
    const b = await response.json().catch(() => ({}));
    throw new Error(
      `${path} failed (${response.status}): ${
        (b as { detail?: string })?.detail ?? response.statusText
      }`,
    );
  }
  return (await response.json()) as Permit;
}

/** GET /api/v1/permits, list all permits (admin). */
export async function listPermits(): Promise<Permit[]> {
  return fetchApi<Permit[]>("/v1/permits", { cache: "no-store" });
}

/** GET /api/v1/permits/:id, fetch a single permit. */
export async function getPermit(id: string): Promise<Permit> {
  return fetchApi<Permit>(`/v1/permits/${encodeURIComponent(id)}`, {
    cache: "no-store",
  });
}

/** POST /api/v1/permits, create a permit (always starts in DRAFT). */
export async function createPermit(body: PermitCreateBody): Promise<Permit> {
  return mutate("/v1/permits", body);
}

/** POST /api/v1/permits/:id/submit, DRAFT → SUBMITTED. */
export async function submitPermit(id: string): Promise<Permit> {
  return mutate(`/v1/permits/${encodeURIComponent(id)}/submit`);
}

/** POST /api/v1/permits/:id/approve, SUBMITTED → APPROVED. */
export async function approvePermit(id: string): Promise<Permit> {
  return mutate(`/v1/permits/${encodeURIComponent(id)}/approve`);
}

/** POST /api/v1/permits/:id/reject, SUBMITTED → REJECTED. */
export async function rejectPermit(id: string, reason: string): Promise<Permit> {
  return mutate(`/v1/permits/${encodeURIComponent(id)}/reject`, { reason });
}

/** POST /api/v1/permits/:id/revoke, APPROVED → REVOKED. */
export async function revokePermit(id: string, reason: string): Promise<Permit> {
  return mutate(`/v1/permits/${encodeURIComponent(id)}/revoke`, { reason });
}

/** Whether a permit is currently active (APPROVED + within its validity window). */
export function isPermitActive(p: Permit, at: Date = new Date()): boolean {
  if (p.status !== "approved") return false;
  if (p.valid_from && at < new Date(p.valid_from)) return false;
  if (p.valid_until && at > new Date(p.valid_until)) return false;
  return true;
}
