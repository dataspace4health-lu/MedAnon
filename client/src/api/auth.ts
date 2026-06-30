/**
 * Auth configuration helpers.
 *
 * The SPA uses Authorization Code Flow + PKCE via oidc-client-ts.
 * The backend exposes GET /v1/auth/config (open path) so the OIDC client can
 * be bootstrapped at runtime without a rebuild — swap Keycloak → Azure AD by
 * changing env vars and restarting the anonymizer.
 *
 * All token acquisition and renewal is handled by UserManager in AuthContext.
 * This module only owns the config fetch and the JWT decode used for display.
 */

export interface AuthConfig {
  provider: "none" | "auto" | "apikey" | "oidc";
  oidc_enabled: boolean;
  oidc_issuer?: string;
  oidc_client_id?: string;
  oidc_scope?: string;
  api_key_accepted?: boolean;
}

/** Fetch the runtime auth configuration (no auth required). */
export async function fetchAuthConfig(): Promise<AuthConfig> {
  const res = await fetch("/api/v1/auth/config", {
    headers: { Accept: "application/json" },
  });
  if (!res.ok) throw new Error(`auth config fetch failed: ${res.status}`);
  return (await res.json()) as AuthConfig;
}

/**
 * Decode a JWT payload without verifying the signature.
 * Used client-side only to read username/roles for display.
 * The backend independently verifies every token.
 */
export function decodeJwt(token: string): Record<string, unknown> | null {
  try {
    const payload = token.split(".")[1];
    const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(decodeURIComponent(escape(json)));
  } catch {
    return null;
  }
}
