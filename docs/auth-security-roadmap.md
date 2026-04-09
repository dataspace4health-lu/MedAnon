# Authentication & Authorization Security Roadmap

**Status:** Deferred to Phase 4 | **Date:** 2026-04-09

This document consolidates all authentication and authorization issues identified during the architecture review. These are intentionally separated from the current development phase (ScoreService) and will be addressed in a dedicated security hardening phase.

---

## Current State

| Aspect | Implementation | Risk |
|---|---|---|
| API auth | Single `MEDANON_API_KEY` env var | No per-user identity; no rotation without restart |
| Frontend credential storage | `localStorage` | XSS exfiltrates auth credential |
| RBAC | 3 roles (admin/analyst/viewer) | All key holders get `admin` unless overridden |
| Redis | No authentication | Any container on processing-net reads pseudonym cache + job data |
| nginx FHIR proxy | No auth on `/fhir/` | Browser users query identified PHI directly |
| Audit identity | Hardcoded `"api-key-user"` | Cannot attribute actions to individuals |
| Session management | None (stateless key) | No expiry, no refresh, no revocation |

---

## Issues by Severity

### Critical (Tier 0)

1. **Source FHIR server exposed via nginx `/fhir/` proxy with no auth**
   - File: `client/nginx.conf`
   - Impact: Any browser user can query identified patient data directly
   - Fix: Require API key header for `/fhir/` and `/fhir-target/` proxy locations

2. **API key stored in browser localStorage**
   - File: `client/src/api/client.ts:17`
   - Impact: Any XSS vulnerability exfiltrates the admin API key
   - Fix: Replace with httpOnly cookie or short-lived session token

3. **Redis has no authentication**
   - File: `docker-compose.yml` (redis service)
   - Impact: Any container on `processing-net` can read/write pseudonym cache and job data
   - Fix: Add `--requirepass` to Redis command, update all clients with password

### High (Tier 1)

4. **Single shared admin API key, no per-user identity**
   - File: `api/auth.py`
   - Impact: HIPAA audit trail says "api-key-user" for all users; no individual accountability
   - Fix: Support multiple API keys with different roles via JSON config map

5. **No Content-Security-Policy (CSP) header**
   - File: `client/nginx.conf`
   - Impact: XSS can exfiltrate PHI via inline scripts or external resource loading
   - Fix: Add `Content-Security-Policy: script-src 'self'; style-src 'self' 'unsafe-inline'`

6. **No HSTS header**
   - File: `client/nginx.conf`
   - Impact: Downgrade attacks possible when TLS is deployed
   - Fix: Add `Strict-Transport-Security: max-age=31536000; includeSubDomains`

### Medium (Tier 2)

7. **RBAC anomaly: `/v1/upload-to-target` has no role enforcement**
   - File: `api/auth.py` (ENDPOINT_ROLE_PREFIXES)
   - Impact: Any authenticated user can upload to the target FHIR server
   - Fix: Add `"/v1/upload-to-target": "admin"` to ENDPOINT_ROLE_PREFIXES

8. **RBAC anomaly: `/v1/configs` write endpoints inconsistent**
   - File: `api/auth.py` + `api/routers/configs.py`
   - Impact: Middleware maps to `viewer`; admin enforced in router logic, not auth layer
   - Fix: Move admin enforcement to ENDPOINT_ROLE_PREFIXES for POST/PUT/DELETE

9. **Open mode gives admin to everyone with no warning**
   - File: `api/auth.py`
   - Impact: When `MEDANON_API_KEY` is unset, all endpoints are open with full admin access
   - Fix: Log a startup warning; optionally restrict to read-only in open mode

---

## Phased Implementation Plan

### Phase 4a: Quick Fixes (1 week)

| # | Task | Effort |
|---|---|---|
| 1 | Add `--requirepass` to Redis + update all clients | 2h |
| 2 | Add CSP and HSTS headers to nginx config | 1h |
| 3 | Add auth requirement to nginx `/fhir/` proxy | 2h |
| 4 | Fix RBAC for `/v1/upload-to-target` and `/v1/configs` | 1h |
| 5 | Bind source FHIR host port to `127.0.0.1` only | 0.5h |
| 6 | Add startup warning when running in open mode | 0.5h |

### Phase 4b: Multi-Key Auth (1-2 weeks)

| # | Task | Effort |
|---|---|---|
| 7 | Support multiple API keys with per-key roles via JSON config | 4h |
| 8 | Replace localStorage with httpOnly session cookie | 4h |
| 9 | Add brute-force protection (rate limit on auth failures) | 2h |
| 10 | Add API key rotation support (hot reload from config) | 3h |
| 11 | Replace "api-key-user" with key ID in audit trail | 2h |

### Phase 4c: OIDC Integration (2-3 weeks)

| # | Task | Effort |
|---|---|---|
| 12 | Integrate Keycloak (or any OIDC provider) for token-based auth | 8h |
| 13 | Map IdP claims to RBAC roles (admin/analyst/viewer) | 4h |
| 14 | Add MFA support via IdP configuration | 2h |
| 15 | Replace session cookies with short-lived JWTs from IdP | 4h |
| 16 | Add token refresh flow in React client | 4h |
| 17 | Add Keycloak to Docker Compose as opt-in profile | 3h |
| 18 | Add Keycloak sub-chart to Helm umbrella chart | 4h |

---

## Target Architecture

```
Browser → nginx (CSP + HSTS)
  → Keycloak login → JWT (httpOnly cookie)
  → /api/* → anonymizer (validate JWT, extract user + roles)
  → /fhir/* → BLOCKED (or require admin JWT)

Audit trail: real_user_id from JWT claims
Redis: --requirepass + TLS (optional)
Helm: Keycloak sub-chart with NetworkPolicy
```
