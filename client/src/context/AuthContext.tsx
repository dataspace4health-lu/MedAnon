/**
 * AuthContext, Authorization Code Flow + PKCE via oidc-client-ts.
 *
 * Flow:
 *   1. On mount: fetch /v1/auth/config → build UserManager → restore session
 *      from localStorage (cross-tab, survives browser restarts).
 *   2. login()  → UserManager.signinRedirect() → Keycloak login page → /auth/callback
 *   3. /auth/callback → handleCallback() → code exchange → user stored, token in memory
 *   4. automaticSilentRenew: UserManager auto-refreshes tokens using the refresh
 *      token (stored in localStorage as part of the User object).
 *   5. logout() → UserManager.signoutRedirect() → Keycloak session terminated →
 *      back to /login.
 *
 * Non-OIDC modes (auto / none / apikey) keep the existing behaviour:
 *   auto/none , all callers get admin (local dev).
 *   apikey    , caller supplies X-API-Key; no redirect login.
 */

import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  useRef,
  type ReactNode,
} from 'react';
import {
  UserManager,
  WebStorageStateStore,
  type User as OidcUser,
} from 'oidc-client-ts';
import { type Role, ROLE_HIERARCHY, STORAGE_KEYS } from '@/config/constants';
import { setAccessToken } from '@/api/authToken';
import { fetchAuthConfig, type AuthConfig } from '@/api/auth';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface User {
  name: string;
  roles: Role[];
}

type AuthMode = 'none' | 'auto' | 'apikey' | 'oidc';

interface AuthContextValue {
  mode: AuthMode;
  /** True while bootstrapping (fetching config / restoring session). */
  loading: boolean;
  isAuthenticated: boolean;
  user: User;
  hasRole: (required: Role) => boolean;
  /**
   * Initiate login.
   * OIDC → redirects to Keycloak; after successful auth Keycloak redirects to
   * /auth/callback which calls handleCallback() and navigates to returnPath.
   * No-op in non-OIDC modes.
   */
  login: (returnPath?: string) => Promise<void>;
  /**
   * Sign out.
   * OIDC → RP-Initiated Logout: Keycloak session terminated, redirect to /login.
   * apikey → clears the stored key.
   */
  logout: () => Promise<void>;
  /**
   * Complete a PKCE code exchange after Keycloak redirects to /auth/callback.
   * Returns the path the user was trying to reach before the login redirect.
   * Throws on failure (expired/invalid code, state mismatch, etc.).
   */
  handleCallback: () => Promise<string>;
  /** Legacy / dual-accept API key (apikey mode or OIDC dual-accept). */
  apiKey: string;
  setApiKey: (key: string) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// ---------------------------------------------------------------------------
// Role helpers
// ---------------------------------------------------------------------------

const ROLE_PREFIX = 'medanon-';

function mapRoles(realmRoles: unknown): Role[] {
  if (!Array.isArray(realmRoles)) return [];
  const out: Role[] = [];
  for (const r of realmRoles) {
    if (typeof r !== 'string') continue;
    const stripped = r.startsWith(ROLE_PREFIX) ? r.slice(ROLE_PREFIX.length) : r;
    if ((ROLE_HIERARCHY as readonly string[]).includes(stripped)) {
      out.push(stripped as Role);
    }
  }
  return out.sort((a, b) => ROLE_HIERARCHY.indexOf(b) - ROLE_HIERARCHY.indexOf(a));
}

function userFromOidc(oidcUser: OidcUser): User {
  const p = oidcUser.profile;
  const name =
    (p['preferred_username'] as string | undefined) ||
    (p['email'] as string | undefined) ||
    (p['name'] as string | undefined) ||
    'user';
  const realmAccess = p['realm_access'] as Record<string, unknown> | undefined;
  const roles = mapRoles(realmAccess?.['roles']);
  return { name, roles };
}

// ---------------------------------------------------------------------------
// UserManager factory (created once after config is fetched)
// ---------------------------------------------------------------------------

/**
 * Resolve the OIDC authority the browser should talk to.
 *
 * In this deployment Keycloak is ALWAYS reached same-origin through the UI's
 * nginx `/auth/` proxy, so the realm URL must use the page's own scheme+host.
 * If the backend's OIDC_ISSUER was configured with a different scheme/host
 * (e.g. http:// while the page is https://), using it verbatim triggers a
 * mixed-content / CORS failure on the discovery fetch. So for a proxied
 * Keycloak realm issuer (path contains `/realms/`) we rebuild it against
 * window.location.origin. External IdPs (Azure/Auth0, no `/realms/`) are
 * used verbatim.
 */
function resolveAuthority(issuer: string): string {
  try {
    const u = new URL(issuer);
    if (u.pathname.includes('/realms/')) {
      const sameOrigin = `${window.location.origin}${u.pathname}`.replace(/\/$/, '');
      if (sameOrigin !== issuer.replace(/\/$/, '')) {
        console.warn(
          `[auth] OIDC_ISSUER "${issuer}" does not match the page origin; ` +
          `using "${sameOrigin}" for the browser. Set OIDC_ISSUER to this exact ` +
          `value in the backend .env so token "iss" validation matches.`,
        );
      }
      return sameOrigin;
    }
  } catch {
    /* malformed issuer, fall through to verbatim */
  }
  return issuer;
}

function buildUserManager(cfg: AuthConfig): UserManager {
  const authority = resolveAuthority(cfg.oidc_issuer!);
  const base = authority.replace(/\/$/, '');

  // Provide all Keycloak endpoint URLs explicitly so oidc-client-ts never
  // fetches the discovery document. Keycloak's discovery doc returns absolute
  // URLs built from KC_HOSTNAME (e.g. https://10.168.192.22:8501/...). When
  // the browser is on a different origin (e.g. https://localhost:8501) those
  // URLs are cross-origin → CSP connect-src 'self' blocks the token POST →
  // sign-in fails with "Failed to fetch". By constructing every endpoint from
  // `authority` (which resolveAuthority() already pinned to window.location.origin)
  // all browser→Keycloak calls stay same-origin regardless of KC_HOSTNAME.
  const metadata = {
    issuer: base,
    authorization_endpoint: `${base}/protocol/openid-connect/auth`,
    token_endpoint:         `${base}/protocol/openid-connect/token`,
    end_session_endpoint:   `${base}/protocol/openid-connect/logout`,
    jwks_uri:               `${base}/protocol/openid-connect/certs`,
    userinfo_endpoint:      `${base}/protocol/openid-connect/userinfo`,
    revocation_endpoint:    `${base}/protocol/openid-connect/revoke`,
  };

  return new UserManager({
    authority,
    metadata,
    client_id:    cfg.oidc_client_id ?? 'medanon-ui',
    redirect_uri: `${window.location.origin}/auth/callback`,
    scope:        cfg.oidc_scope ?? 'openid profile email',
    response_type: 'code', // Authorization Code Flow (PKCE added automatically)
    userStore: new WebStorageStateStore({ store: window.localStorage }),
    automaticSilentRenew: true,
    monitorSession: false,
    revokeTokensOnSignout: true,
  });
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function AuthProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [oidcUser, setOidcUser] = useState<OidcUser | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const managerRef = useRef<UserManager | null>(null);

  // Legacy API-key state (dual-accept alongside OIDC, or apikey-only mode).
  const [apiKey, setApiKeyState] = useState<string>(
    () => localStorage.getItem(STORAGE_KEYS.API_KEY) ?? '',
  );

  // ── Bootstrap ─────────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const cfg = await fetchAuthConfig();
        if (cancelled) return;
        setAuthConfig(cfg);

        if (cfg.provider === 'oidc' && cfg.oidc_enabled && cfg.oidc_issuer) {
          const mgr = buildUserManager(cfg);
          managerRef.current = mgr;

          // ── Event subscriptions ─────────────────────────────────────────
          mgr.events.addUserLoaded((u) => {
            if (cancelled) return;
            setOidcUser(u);
            setAccessToken(u.access_token);
          });
          mgr.events.addUserUnloaded(() => {
            if (cancelled) return;
            setOidcUser(null);
            setAccessToken(null);
          });
          mgr.events.addUserSignedOut(() => {
            if (cancelled) return;
            setOidcUser(null);
            setAccessToken(null);
          });
          mgr.events.addAccessTokenExpired(() => {
            if (cancelled) return;
            setOidcUser(null);
            setAccessToken(null);
          });
          mgr.events.addSilentRenewError((err) => {
            console.warn('[auth] silent renew failed:', err.message);
          });

          // ── Restore session from localStorage ──────────────────────────
          try {
            const stored = await mgr.getUser();
            if (cancelled) return;
            if (stored && !stored.expired) {
              setOidcUser(stored);
              setAccessToken(stored.access_token);
            }
            // If expired but refresh_token present, automaticSilentRenew will
            // pick it up on the next page interaction. We don't eagerly renew
            // here to keep the boot path fast.
          } catch {
            // Storage read failure, start unauthenticated.
          }
        }
      } catch {
        // Backend unreachable, fall back to open/auto mode.
        if (!cancelled) {
          setAuthConfig({ provider: 'auto', oidc_enabled: false });
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      // Do NOT destroy the manager on cleanup, StrictMode double-fires this
      // effect; destroying would break the already-established subscriptions.
    };
  }, []);

  // ── Derived values ────────────────────────────────────────────────────────
  const mode: AuthMode = (authConfig?.provider ?? 'auto') as AuthMode;
  const oidcActive = mode === 'oidc' && !!authConfig?.oidc_enabled;

  const user: User = oidcActive
    ? oidcUser
      ? userFromOidc(oidcUser)
      : { name: 'guest', roles: [] }
    : { name: 'local-dev', roles: ['admin'] };

  const isAuthenticated = oidcActive ? oidcUser !== null : true;

  // ── Actions ───────────────────────────────────────────────────────────────

  const login = useCallback(async (returnPath?: string) => {
    const mgr = managerRef.current;
    if (!mgr) return;
    // Store the intended destination so handleCallback() can navigate there.
    await mgr.signinRedirect({
      state: returnPath ?? window.location.pathname,
    });
  }, []);

  const logout = useCallback(async () => {
    if (oidcActive) {
      const mgr = managerRef.current;
      if (mgr) {
        await mgr.signoutRedirect({
          post_logout_redirect_uri: `${window.location.origin}/login`,
        });
      }
    } else {
      // apikey / auto / none, just clear the stored key.
      setApiKeyState('');
      localStorage.removeItem(STORAGE_KEYS.API_KEY);
    }
  }, [oidcActive]);

  const handleCallback = useCallback(async (): Promise<string> => {
    const mgr = managerRef.current;
    if (!mgr) throw new Error('OIDC provider not configured');
    const u = await mgr.signinRedirectCallback();
    // The callback fires the addUserLoaded event which updates oidcUser, but
    // we also set it here for immediate synchronous consistency.
    setOidcUser(u);
    setAccessToken(u.access_token);
    return (typeof u.state === 'string' && u.state) ? u.state : '/';
  }, []);

  const setApiKey = useCallback((key: string) => {
    if (key) localStorage.setItem(STORAGE_KEYS.API_KEY, key);
    else localStorage.removeItem(STORAGE_KEYS.API_KEY);
    setApiKeyState(key);
  }, []);

  const hasRole = useCallback(
    (required: Role): boolean => {
      const requiredLevel = ROLE_HIERARCHY.indexOf(required);
      return user.roles.some(
        (role) => ROLE_HIERARCHY.indexOf(role) >= requiredLevel,
      );
    },
    [user.roles],
  );

  return (
    <AuthContext.Provider
      value={{
        mode,
        loading,
        isAuthenticated,
        user,
        hasRole,
        login,
        logout,
        handleCallback,
        apiKey,
        setApiKey,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}
