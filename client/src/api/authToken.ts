/**
 * Module-level auth-token bridge.
 *
 * `client.ts` exposes plain (non-React) fetch helpers, so it cannot read the
 * OIDC access token from React context. The AuthContext OIDC branch publishes
 * the current bearer token here; `getAuthHeaders()` reads it. When OIDC is not
 * in use the token stays null and the API-key path is used instead.
 */

let accessToken: string | null = null;

/** Publish (or clear) the current OIDC access token. Called by AuthContext. */
export function setAccessToken(token: string | null): void {
  accessToken = token;
}

/** Read the current OIDC access token, or null when not authenticated via OIDC. */
export function getAccessToken(): string | null {
  return accessToken;
}
