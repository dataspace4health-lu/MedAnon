---
title: "Configure Authentication"
sidebar_position: 8
description: "Choose an auth provider  open, API key, per-client keys, or OIDC/Keycloak  and understand the SPA login flow."
---

# Configure Authentication

Authentication is pluggable via `MEDANON_AUTH_PROVIDER`. The backend and the React SPA adapt together based on this setting.

| `MEDANON_AUTH_PROVIDER` | Behaviour |
|---|---|
| `auto` (default) | API-key mode if `MEDANON_API_KEY` is set, else open |
| `apikey` | Require `X-API-Key` on all protected endpoints |
| `oidc` | Validate OIDC/JWT bearer tokens (SPA shows login form) |
| `none` | Fully open  local dev only |

**Open paths** (always unauthenticated): `/health`, `/ready`, `/metrics`, `/docs`, `/v1/auth/config`.

**RBAC roles:** `admin` → `analyst` → `viewer`. See [Security Model](../explanation/security-model.md) for the full role-to-endpoint mapping.

---

## Open mode (local dev)

```bash
# .env  no auth required
MEDANON_AUTH_PROVIDER=none
```

The SPA renders without a login screen. All API calls succeed without headers.

---

## API-key mode (single shared key)

```bash
MEDANON_AUTH_PROVIDER=apikey
MEDANON_API_KEY=<generate with: openssl rand -hex 32>
```

All API calls must include:
```http
X-API-Key: <your-key>
```

The SPA shows the existing API-key input in the settings panel; no login screen is shown.

---

## Per-client API keys (multi-tenant)

Issue individual keys per client instead of sharing one key. Keys are stored as bcrypt hashes in `medanon.api_keys` (PostgreSQL):

```bash
# Create a key for a client
curl -s -X POST http://localhost:8000/v1/api-keys \
  -H 'X-API-Key: <admin-key>' \
  -H 'Content-Type: application/json' \
  -d '{"client": "research-team", "role": "analyst"}'
# → returns {"key": "..."} once  store it securely, it is not recoverable

# List all keys
curl http://localhost:8000/v1/api-keys -H 'X-API-Key: <admin-key>'

# Rotate a key
curl -X PUT http://localhost:8000/v1/api-keys/research-team \
  -H 'X-API-Key: <admin-key>'

# Delete a key
curl -X DELETE http://localhost:8000/v1/api-keys/research-team \
  -H 'X-API-Key: <admin-key>'
```

Per-client keys work alongside a shared `MEDANON_API_KEY`  the backend accepts either.

---

## OIDC / Keycloak

### 1. Start the identity provider

```bash
docker compose --profile auth up -d
# starts: keycloak (port 8180) + keycloak-db
```

Keycloak starts with a pre-configured realm `medanon` imported from `services/keycloak/realm-export.json`. It includes:
- Realm roles: `medanon-admin`, `medanon-analyst`, `medanon-viewer`
- A client `medanon-ui` with Direct Access Grants enabled

The UI nginx proxies `/auth/` → `medanon-keycloak:8080`, so Keycloak shares the SPA's origin. The browser reaches it at `http://<host>:8501/auth/...`.

### 2. Configure the backend

```bash
# .env
MEDANON_AUTH_PROVIDER=oidc
MEDANON_AUTH_ALLOW_API_KEY=true          # also accept X-API-Key alongside OIDC

# Issuer is BROWSER-FACING (through nginx /auth)  it must equal the `iss`
# claim Keycloak stamps into tokens, which the browser also receives.
OIDC_ISSUER=http://localhost:8501/auth/realms/medanon
OIDC_CLIENT_ID=medanon-ui                # default; matches the realm client
OIDC_SCOPE=openid profile email          # default

# JWKS is derived as <issuer>/.well-known/jwks.json. If the anonymizer container
# cannot reach the browser-facing issuer host, point it at the internal URL:
OIDC_JWKS_URL=http://medanon-keycloak:8080/auth/realms/medanon/protocol/openid-connect/certs

OIDC_ROLE_CLAIM_PATH=realm_access.roles  # where Keycloak puts realm roles
OIDC_ROLE_MAP={"medanon-admin":"admin","medanon-analyst":"analyst","medanon-viewer":"viewer"}
# OIDC_AUDIENCE=medanon                   # optional  only enforced if set
```

:::warning Split-URL gotcha
`OIDC_ISSUER` must match the token's `iss` claim (browser-facing). But the backend, running inside Docker, may not be able to resolve that host. When that happens, set `OIDC_JWKS_URL` explicitly to the internally-reachable Keycloak certs endpoint so JWKS fetch succeeds while `iss` validation still uses the public issuer.
:::

### 3. How the SPA auth flow works

The React SPA has no hardcoded auth mode. At startup it calls `GET /api/v1/auth/config` and adapts. Note there is **no token URL in the response**  the SPA derives Keycloak's token/logout endpoints from `oidc_issuer`:

```
SPA boots
  │
  ├─ GET /api/v1/auth/config
  │   → { provider: "oidc", oidc_enabled: true,
  │        oidc_issuer: "http://localhost:8501/auth/realms/medanon",
  │        oidc_client_id: "medanon-ui",
  │        oidc_scope: "openid profile email",
  │        api_key_accepted: true }
  │
  ├─ Check sessionStorage for a stored refresh token
  │   → if found: silent refresh → restore session (no login screen)
  │   → if not found or expired: show LoginPage
  │
  └─ User submits username + password
      → POST <issuer>/protocol/openid-connect/token   grant_type=password
      ← { access_token, refresh_token, expires_in }

      access_token  → held in memory only (never written to storage)
      refresh_token → sessionStorage  (tab-scoped: survives reload, cleared on close)

      All API calls: Authorization: Bearer <access_token>
      Before expiry:  silent refresh (grant_type=refresh_token) → new tokens
      On logout:      best-effort POST <issuer>/protocol/openid-connect/logout + clear tokens
```

An authenticated token whose roles don't map to any MedAnon role is granted `viewer` (not rejected).

**Why sessionStorage for the refresh token?**
The refresh token needs to survive a page reload (so the user doesn't have to log in again after `F5`), but should not outlive the browser tab (to minimize the window if XSS were to occur). `localStorage` would survive tab close and is shared across tabs  too broad. `sessionStorage` is tab-scoped and cleared on close.

The access token is **never written to any storage**  it lives only in the `AuthContext` React state and the `authToken.ts` in-memory bridge. If the page reloads, the refresh token is used to re-derive it silently.

### 4. Create users in Keycloak

Access the Keycloak admin console at `http://localhost:8180/auth` (admin credentials from `.env`: `KEYCLOAK_ADMIN_USER` / `KEYCLOAK_ADMIN_PASSWORD`):

1. Go to **Realm: medanon** → **Users** → **Add user**
2. Set username + email, save
3. Go to **Credentials** → set a password (disable temporary)
4. Go to **Role Mappings** → assign `medanon-admin`, `medanon-analyst`, or `medanon-viewer`

### 5. Swap to Azure AD (or any OIDC provider)

No code changes needed. Update `.env`:

```bash
MEDANON_AUTH_PROVIDER=oidc
OIDC_ISSUER=https://login.microsoftonline.com/<tenant-id>/v2.0
OIDC_AUDIENCE=<client-id>
OIDC_ROLE_CLAIM_PATH=roles          # Azure puts app roles here
OIDC_ROLE_MAP={"MedAnonAdmin":"admin","MedAnonAnalyst":"analyst"}
```

Note: Azure AD uses Authorization Code flow in most deployments. The SPA's Direct Access Grant (`grant_type=password`) requires the Azure AD app to have "Allow public client flows" enabled. If your Azure policy prohibits this, use the Authorization Code + PKCE flow instead (requires adding `react-oidc-context` redirect-based login  the embedded form currently uses Direct Access Grant only).

---

## SPA behavior by auth mode

| Mode | Login screen shown | API-key field shown | Bearer token used |
|---|---|---|---|
| `none` / open | No | No | No |
| `apikey` or `auto` (key set) | No | Yes (settings panel) | No |
| `oidc` | Yes (`LoginPage`) | No (unless `ALLOW_API_KEY=true`) | Yes |

The mode is determined entirely by the `/v1/auth/config` response at runtime  the SPA bundle itself is the same for all modes.

---

## Token storage summary

| Token | Storage | Why |
|---|---|---|
| Access token | JS memory (`AuthContext` state + `authToken.ts`) | Never persisted  lost on reload, re-derived from refresh token automatically |
| Refresh token | `sessionStorage` | Survives page reload within the tab; cleared on tab close; not shared across tabs |
| API key | `localStorage` | Intentional persistence  the user explicitly configures it once |

---

## Environment variables reference

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_AUTH_PROVIDER` | `auto` | `auto` / `apikey` / `oidc` / `none` |
| `MEDANON_API_KEY` | (blank) | Single shared API key (blank = open in `auto` mode) |
| `MEDANON_AUTH_ALLOW_API_KEY` | `true` | Accept `X-API-Key` alongside OIDC |
| `OIDC_ISSUER` | (blank) | Browser-facing issuer URL; must equal the token `iss` (e.g. `http://localhost:8501/auth/realms/medanon`). Also gates `oidc_enabled`. |
| `OIDC_CLIENT_ID` | `medanon-ui` | Public client ID used by the SPA's password grant |
| `OIDC_SCOPE` | `openid profile email` | Scopes requested at login |
| `OIDC_JWKS_URL` | derived | Defaults to `<issuer>/.well-known/jwks.json`; override when the backend can't reach the public issuer host |
| `OIDC_AUDIENCE` | (blank) | Expected `aud` claim  only enforced when set |
| `OIDC_ROLE_CLAIM_PATH` | `realm_access.roles` | Dotted path to the role list (`roles` for Azure) |
| `OIDC_ROLE_MAP` | (blank) | JSON mapping OIDC role names → MedAnon roles; unmapped authenticated users default to `viewer` |
| `OIDC_USERNAME_CLAIM` | `preferred_username` | JWT claim used as display name in SPA `UserMenu` |
| `OIDC_CLOCK_SKEW_SEC` | `10` | Allowed JWT clock skew in seconds |
| `KEYCLOAK_PORT` | `8180` | Host port for Keycloak (`--profile auth`) |
| `KEYCLOAK_ADMIN_USER` / `_PASSWORD` |  | Keycloak bootstrap admin credentials |
| `KEYCLOAK_PUBLIC_URL` |  | Public URL for Keycloak (needed when browser and server use different URLs) |
