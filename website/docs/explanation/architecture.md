---
title: "Architecture"
sidebar_position: 1
description: "System topology, the 4-stage pipeline, gPAS, scoring, and auth model."
---

# MedAnon, Architecture

## What it does

MedAnon is a FHIR R4 de-identification engine. It accepts FHIR resources (JSON / NDJSON / XML), applies configurable match-action rules from a YAML profile, and returns transformed data with PII removed or pseudonymized. It is designed to be the privacy layer between an identified FHIR source and any downstream consumer, research databases, dataspace connectors, analytics pipelines.

---

## System topology

```
┌─────────────────────────────────────────────────────────────┐
│  Browser                                                     │
└──────────────────────┬──────────────────────────────────────┘
                       │ http://host:8501
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  nginx (medanon-ui)                                          │
│   /api/*        → strips prefix → anonymizer:8000           │
│   /fhir/*       → passthrough  → hapi-fhir:8080 (300 s)     │
│   /fhir-target/*→ passthrough  → hapi-fhir-target:8080      │
│   /             → React SPA                                  │
└──────────────┬─────────────────────┬────────────────────────┘
               │                     │
               ▼                     ▼
┌──────────────────────┐   ┌──────────────────────────────────┐
│  anonymizer :8000    │   │  hapi-fhir (source) :8080        │
│  (FastAPI)           │◄──┤  PostgreSQL-backed FHIR R4       │
│  worker :9091(metrics│   │  source-net (isolated - no host  │
│                      │   │  port; accessed only via anon.)  │
│  reads  ──────────►  │   └──────────────────────────────────┘
│  de-identifies       │
│  writes ──────────►  │   ┌──────────────────────────────────┐
│                      │──►│  hapi-fhir-target :8082          │
└──────┬───────────────┘   │  PostgreSQL-backed FHIR R4       │
       │                   │  Stores DE-IDENTIFIED data only  │
       │                   └──────────────────────────────────┘
       │
       ├──► gateway (Traefik v3) ──► gpas replicas (sticky for /gpas-web)
       │     │                  └──► gpas-postgres:5432
       │     │   network alias: gpas-lb (legacy URL compatibility)
       │     │
       │     └──────────────────► nlp replicas (round-robin)
       │         network alias: nlp-lb (legacy URL compatibility)
       │         (Presidio + spaCy en_core_web_lg, ~800 MB image)
       │
       ├──► analytics:8100 (risk analysis + synthetic data)
       │
       ├──► app-db:5432 (jobs, configs, subscriptions, staging)
       │
       └──► redis:6379 (password-protected)
             ├── job queue  (Redis Streams, event-driven)
             └── gPAS cache (L2, cross-replica)

Opt-in profiles:
  --profile ha          → gpas-db-replica (PostgreSQL streaming replica)
  --profile trust       → trust-gate:8400 + trust-gate-ui:8401 + fhir-validator
  --profile auth        → keycloak:8180 + keycloak-db (OIDC identity provider)
  --profile s3          → minio:9000 (S3-compatible object storage)
  --profile ai          → ollama (local LLM for AI agents)
  --profile sqltest     → sql-source-test-db (SQL source connector test fixture)
  --profile monitoring  → prometheus:9090 + grafana:3000 + cadvisor + jaeger
```

**Two separate FHIR servers**, identified and de-identified data never share a database. This is a deliberate design: it prevents accidental joins, satisfies physical separation requirements under GDPR Art. 25 (data minimization by design), and allows different access controls per server.

**Why a dedicated NLP microservice?** The Presidio + spaCy `en_core_web_lg` model adds ~800 MB to the Docker image. Running it in the anonymizer process would double memory consumption for every anonymizer replica. The NLP microservice keeps this cost fixed regardless of anonymizer scaling, and its replicas can be independently sized for CPU-intensive NLP workloads.

**NLP cache hierarchy.** Each NLP replica keeps an L1 in-process LRU (~20 k entries) and consults an optional Redis L2 cache on DB 2 (`NLP_REDIS_URL`, default key prefix `medanon:nlp:detect:`, default TTL 7 days). L2 is shared across replicas and survives restarts, eliminating cold-cache penalties on deploy. Redis errors fail-soft: the lookup falls through to compute. Implemented in `services/nlp/src/cache.py`.

---

## Services

**Always-on services:**

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | 8000 | FastAPI de-identification engine |
| `worker` | `medanon:latest` | 9091 (metrics) | Dedicated async job worker (Prometheus) |
| `ui` | `medanon-ui:latest` | 8501 | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | none (isolated) | Source FHIR R4 (identified data) |
| `hapi-db` | `postgres:16-alpine` | internal | PostgreSQL for source HAPI |
| `fhir-target` | `hapiproject/hapi:v7.6.0` | 8082 | Target FHIR R4 (de-identified data) |
| `hapi-target-db` | `postgres:16-alpine` | internal | PostgreSQL for target HAPI |
| `gateway` | `traefik:v3` | 8080 (gPAS), 8200 (NLP) | Traefik v3 API gateway, Docker-provider service discovery; carries `gpas-lb` and `nlp-lb` network aliases for backward compatibility |
| `gpas` | WildFly 38 + gPAS | via gateway | Reversible pseudonymization (TTP); scaled with `--scale gpas=N` |
| `gpas-db` | `postgres:16-alpine` | internal | gPAS pseudonym store |
| `app-db` | `postgres:16-alpine` | internal | Jobs, configs, subscriptions, staging |
| `redis` | `redis:7-alpine` | internal | Shared job queue + gPAS L2 cache (password-protected) |
| `analytics` | `medanon-analytics:latest` | 8100 | Risk analysis + synthetic data |
| `scoring` | `medanon-scoring:latest` | 8300 | Scoring microservice (privacy × utility × quality); used when `SCORING_SERVICE_URL` is set |
| `nlp` | `medanon-nlp:latest` | via gateway | Presidio NLP microservice (~800 MB image); scaled with `--scale nlp=N` |

**Opt-in profiles (started with `--profile <name>`):**

| Container | Profile | Host Port | Role |
|---|---|---|---|
| `gpas-db-replica` | `ha` | internal | PostgreSQL streaming replica for gPAS HA |
| `trust-gate` | `trust` | 8400 | Pre-privacy data-quality barrier; emits Quality Passport |
| `trust-gate-ui` | `trust` | 8401 | Trust Gate management UI |
| `fhir-validator` | `trust` | internal | HL7 FHIR validator used by Trust Gate for structural/profile conformance |
| `keycloak` + `keycloak-db` | `auth` | 8180 | Keycloak OIDC identity provider (realm `medanon`) |
| `minio` | `s3` | 9000, 9001 | MinIO S3-compatible object storage for job results |
| `ollama` | `ai` | internal | Local LLM inference for AI agent endpoints |
| `sql-source-test-db` | `sqltest` | internal | PostgreSQL fixture for SQL/tabular source connector testing |
| `prometheus` / `grafana` / `cadvisor` / `jaeger` | `monitoring` | 9090 / 3000 / 8888 / 16686 | Prometheus, Grafana dashboards, container metrics, Jaeger traces |

**Shared code (`packages/medanon-core`).** The domain contracts (`domain`), the analytics engine (`analytics`), and the scoring engine (`scoring`) are a single zero-dependency inner package rather than per-service copies. The anonymizer puts it on its path directly; the `scoring` and `analytics` microservices (and trust-gate, for the `domain.trust` phase vocabulary) `pip install` it in their Dockerfiles, so every service runs identical shared logic.

### Edge & routing

Two edge components handle ingress, routing, and horizontal scaling:

| Component | Container | Role |
|--------|-----------|---------|
| `client/nginx.conf` | `medanon-ui` | UI reverse proxy: SPA serving + `/api/*`, `/fhir/*`, `/fhir-target/*` routing |
| Traefik labels on `gpas` / `nlp` | `medanon-gateway` | API gateway: dynamic Docker-provider discovery, round-robin LB, sticky sessions for the gPAS JSF UI |

The `gateway` service joins `processing-net` under the `gpas-lb` and `nlp-lb` aliases so existing env-var values work without changes. `services/gpas/lb/nginx.conf` and `services/nlp/nginx.conf` are kept as reference only.

**UI nginx routes (unchanged):**
- `/` → SPA static files (React app)
- `/api/*` → `anonymizer:8000` (upstream with keepalive 16)
- `/fhir/*` → `hapi-fhir:8080` (source FHIR server)
- `/fhir-target/*` → `hapi-fhir-target:8080` (de-identified data)

**Traefik gateway features:**
- Docker provider, read-only socket mount, `exposedbydefault=false`
- gPAS web UI uses sticky cookies (`gpas_session`) so JSF ViewState stays bound to one replica
- Round-robin for both gPAS REST and NLP traffic
- Internal Prometheus metrics endpoint on `:8082/metrics` (gateway-internal; not host-published)

**Scaling (Traefik discovers new replicas via Docker labels, no config reload):**
```bash
docker compose up -d --scale gpas=3              # 3 gPAS replicas
docker compose up -d --scale nlp=4               # 4 NLP replicas
docker compose up -d --scale gpas=3 --scale nlp=4
```

---

## De-identification pipeline

Every resource passes through a fixed sequence of stages:

```
Input (JSON / NDJSON / XML)
    │
    ▼
io_formats.py          Parse. XML uses defusedxml to prevent entity expansion attacks.
    │
    ▼
config_service.py      Load YAML profile. @lru_cache(maxsize=8) - loaded once per profile
                       per process. Env vars interpolated with ${VAR:-default} syntax.
    │
    ▼
rule_matcher.py        Build per-resource-type rule index using FHIRPath expressions.
                       Results cached per (resource_type, rules_hash) to avoid repeat
                       FHIRPath evaluation on identical inputs.
    │
    ▼
action_dispatcher.py   STAGE 1 - match: Iterate matched rules. Apply stateless actions
                       immediately (redact, cryptohash, generalize, substitute, perturb,
                       scrub_text, encrypt). Defer NLP actions (nlp_scrub, nlp_detect_act)
                       into NlpWork items. Collect gPAS-bound values into BatchWork.
                       Neither NLP nor gPAS is called yet - batching is critical
                       for throughput.
    │
    ▼  ┌─────────── STAGES 2 & 3 RUN CONCURRENTLY ───────────┐
       │  (disjoint resource paths, so it is safe to overlap) │
nlp_orchestrator.py    STAGE 2 - phi_detection: Batch NLP detection across all resources.
                       Phase A - extract text fields from all deferred NlpWork items.
                       Phase B - deduplicate and batch-detect unique texts (one HTTP
                       call to the NLP service). Phase C - per-resource replacement with
                       isolated token_state so surrogate tokens are deterministic within
                       a resource but unique across resources.
                                          ‖  (concurrent with)
gpas_orchestrator.py   STAGE 3 - pseudonymize: Send one HTTP request to gPAS for all
                       collected values. Checks cache first (local LRU → Redis L2 → live
                       call). Results written back into the resource tree.
    │  └──────────────────────────────────────────────────────┘
    ▼
post_processor.py      STAGE 4 - finalize: Rewrite FHIR references (Patient/123 →
                       Patient/p-123) and replace pseudonym-changed IDs in text fields.
    │
    ▼
manifest.py            Tag meta.tag with a per-rule transformation summary if
                       MEDANON_MANIFEST_ENABLED=true. Required for the UI Resource
                       Summary view. Stripped before FHIR server upload (the display
                       field would exceed HAPI's varchar(200) column limit).
    │
    ▼
io_formats.py          Serialize. Output format matches input or as requested.
```

**Why staged + concurrent?** Both NLP and gPAS have non-trivial per-call overhead. Processing 300 resources with individual calls would be ~300 HTTP round-trips for each. The **match** stage collects deferred work (NlpWork for NLP, BatchWork for gPAS) without making any external calls. The **phi_detection** and **pseudonymize** stages then run **concurrently**: NLP deduplicates texts across all resources and sends a single batch, while gPAS does the same for pseudonymization, they touch disjoint resource paths, so overlapping them hides one upstream's latency behind the other. This reduces hundreds of HTTP calls to 2-3 regardless of resource count, and the two batch calls overlap rather than running back-to-back. Per-stage latency is exported as `medanon_pipeline_stage_duration_seconds{stage=...}`.

---

## Async job queue

Long-running operations (`bulk-export`, `cohort`) run as background jobs:

```
POST /v1/jobs/bulk-export  ──► 202 Accepted  {"job_id": "abc123"}
                                      │
                               worker loop (background)
                                      │
                               ┌──────▼──────────────────────────┐
                               │ 1. Fetch resource types         │
                               │    (paginated REST, page=500)   │
                               │ 2. De-identify each page        │
                               │ 3. Topological sort by deps     │
                               │ 4. Rewrite references           │
                               │ 5. Upload in tier order         │
                               │    (200 resources/batch bundle) │
                               └─────────────────────────────────┘
                                      │
GET /v1/jobs/abc123         ◄── poll status (pending/running/done/failed)
GET /v1/jobs/abc123/result  ◄── download NDJSON when done
```

**Backend selection at startup (priority order):**
- `MEDANON_REDIS_URL` set → `RedisJobStore`: Redis Streams + consumer groups (at-least-once delivery, no polling), cross-replica safe
- `MEDANON_APP_DB_URL` set → `PostgresJobStore`: LISTEN/NOTIFY event-driven, single-instance
- Neither set → `SqliteJobStore`: polls every 2 s, single-instance only

**Why not always Redis?** Local dev has no Redis. SQLite fallback works fine for single-instance dev and CI. PostgreSQL (via `app-db`) provides event-driven execution without requiring Redis. Redis is required for multi-replica production deployments where multiple anonymizer instances share a job queue.

---

## Upload ordering (topological sort)

**Problem:** FHIR referential integrity. HAPI rejects a resource if it references another resource that doesn't exist yet. For example, uploading an `Encounter` before its `Organization` and `Practitioner` causes HAPI-1094.

**Why a naive 2-tier approach fails:** A simple "base types first, rest second" doesn't work when there are 3+ levels of dependency:
- `Organization` (tier 0, no deps)
- `Practitioner` (tier 0, no deps)
- `Encounter` (tier 1, references Organization + Practitioner)
- `Observation` (tier 2, references Encounter)
- `DiagnosticReport` (tier 3, references Observation)

**Solution:** Bellman-Ford relaxation over the reference graph. `tier[A] = 1 + max(tier[B] for B in deps[A])`. Converges in at most N passes where N = number of distinct resource types. Resources within the same tier upload in a single batch Bundle.

---

## Reference rewriting after ID sanitization

**Problem:** HAPI FHIR rejects purely numeric resource IDs (HAPI-0960). The de-identification pipeline may produce numeric IDs (e.g. gPAS pseudonyms are sometimes numeric, or the source FHIR server uses numeric auto-increment IDs). These are prefixed with `p-` before upload (e.g. `Patient/123` → `Patient/p-123`).

**Consequence:** Any resource that contains `{"reference": "Patient/123"}` will now point to a non-existent resource, causing HAPI-1094 on upload.

**Fix:** Before uploading, `_compute_id_map()` collects all `(resourceType, originalId) → sanitisedId` mappings, and `_rewrite_references()` walks every resource recursively to update matching `reference` fields. This must happen after topological sort (sort is read-only) and before chunking into batch Bundles.

---

## FHIR R4 code binding constraint

`Patient.gender` and `Practitioner.gender` are bound to the `AdministrativeGender` value set: `male | female | other | unknown`. Substituting `[REDACTED]` causes HAPI to reject the resource with HAPI-1821 ("not a valid code").

**Fix in `config_structure_preserving.yaml`:** The gender rules use `substitute_with: "unknown"` instead of the `*redacted` YAML anchor (which resolves to `"[REDACTED]"`). This keeps the field valid while removing its identifying content.

This is a fundamental FHIR constraint: de-identification must produce *valid* FHIR resources, not just *transformed* ones.

---

## gPAS pseudonymization

gPAS (Generic Pseudonym Administration Service) is a TTP (Trusted Third Party) service developed by the University Medicine Greifswald. It maintains a persistent, audited mapping between original values and pseudonyms, making pseudonymization **reversible** under controlled conditions (e.g. adverse event investigation).

```
MedAnon                          gPAS
    │                               │
    │── POST $pseudonymizeAllowCreate ──►│
    │   Parameters: [id1, id2, ...]  │
    │                               │
    │◄── Parameters: [psn1, psn2, ...]──│
    │                               │
    cache result locally (LRU)      │
    optionally cache in Redis (L2)  │
```

**Why batch?** One HTTP call per resource is prohibitive. A bulk export of 20,000 resources with gPAS IDs would require 20,000 HTTP calls. Batching collects all IDs from a page (~500 resources) and sends one request.

**Circuit breaker:** If gPAS fails 5 times in 60 s, the circuit opens. Subsequent requests fail fast (no timeout wait) until a recovery probe succeeds after 30 s. This prevents a gPAS outage from cascading into anonymizer thread exhaustion.

**Domain management:** gPAS maintains an in-memory `domainLocks HashMap` populated only when domains are created through its own REST API. Direct SQL inserts work at the database level but bypass this cache, causing "domain not found" errors at runtime. **Always create domains via the gPAS web UI or `make init-domains`.**

---

## Config profiles

Eight bundled profiles. Auto-selected based on environment; overridable per-request via `?config_profile=<name>`.

| Profile | ID handling | Dates | Geographic | Requires gPAS | Use case |
|---|---|---|---|---|---|
| `config.yaml` | SHA3-256 hash | Year only | Zip prefix (3-digit) | No | Local dev / testing |
| `config_gpas.yaml` | gPAS pseudonym (reversible) | Year only | Zip prefix | Yes | Production with re-linkage |
| `config_gdpr_eu.yaml` | HMAC SHA3-256 | Redacted | Redacted | No | GDPR Art. 4(5) |
| `config_hipaa_safe_harbor.yaml` | Redacted | Year only | State + 3-digit zip | No | US HIPAA Safe Harbor |
| `config_research_pseudonymous.yaml` | SHA3-256 hash | Year-month | 3-digit zip | No | IRB research |
| `config_structure_preserving.yaml` | gPAS pseudonym (reversible) | Year (birthDate only) | Preserved | Yes | Full FHIR structure downstream |
| `config_value_masking.yaml` | gPAS pseudonym (reversible) | Decade (birth), year (clinical) | Masked to `[REDACTED]` | Yes | Field-complete with `nlp_detect_act` |
| `config_k_anonymity.yaml` | gPAS pseudonym (reversible) | Year-month | 3-digit zip | Yes | OLA-style k-anon lattice solver; requires staging layer |

Auto-selection: `GPAS_URL` set → `config_gpas.yaml`; otherwise → `config.yaml`.

---

## AI agents

Seven AI-powered agents are exposed via `/v1/ai/*` when `MEDANON_AI_ENABLED=true`:

| Agent | Endpoint | Role | Description |
|---|---|---|---|
| **Status** | `GET /v1/ai/status` | viewer | Check AI provider availability and active model |
| **Config generator** | `POST /v1/ai/generate-config` | admin | YAML profile generation from natural language. All 8 bundled profiles injected as few-shot examples (prompt context, not vector retrieval). Validates output through the Settings loader before returning. |
| **PII detector** | `POST /v1/ai/detect-pii` | analyst | 3-layer PII scan: regex → NER → LLM. The LLM layer **must** use a local model (`MEDANON_AI_PII_PROVIDER`). |
| **Field scanner** | `POST /v1/ai/scan-fields` | analyst | Classify FHIR field paths in a sample resource as PHI-bearing and suggest de-identification actions. |
| **Rule explainer** | `POST /v1/ai/explain` | analyst | Plain-language explanation of config rules via SSE streaming. |
| **Compliance advisor** | `POST /v1/ai/compliance` | analyst | Regulatory gap analysis vs HIPAA, GDPR, and other frameworks. |
| **Config chat** | `POST /v1/ai/chat` | analyst | Conversational config-building assistant with access to the active profile and bundled examples. |

Agent implementations live under `integrations/ai/agents/`: `config_generator.py`, `pii_detector.py`, `field_scanner.py`, `rule_explainer.py`, `compliance.py`, `config_chat.py`, `action_policy.py`.

**LLM provider:** `integrations/ai/provider.py` wraps `litellm`, which supports OpenAI-compatible APIs and Ollama for local inference. The `--profile ai` Docker profile starts an Ollama container.

**Known open issues:**
- PHI must not be sent to external LLM providers. Code-level enforcement for the PII detector's AI layer is pending (`integrations/ai/agents/pii_detector.py`).
- Prompt injection: user input is interpolated directly into LLM messages in `config_generator.py`.
- `LLMProvider._cache` grows without eviction.

---

## Scoring system

Every de-identification operation can be scored against three dimensions:

| Dimension | What it measures | Weight |
|---|---|---|
| **Privacy** | Re-identification risk (k-anonymity, l-diversity, HIPAA identifier coverage) | Hard constraint |
| **Utility** | Data usefulness for downstream analytics (field retention, date precision, code preservation) | Configurable |
| **Quality** | FHIR structural validity (required fields, valid codes, reference integrity) | Configurable |

The composite score is `privacy × utility × quality` (0.0-1.0). Scores are stored in `medanon.processing_runs` and exposed via `/v1/jobs/{id}/score` and `/v1/processing-runs`.

Scoring is opt-in: `MEDANON_SCORING_ENABLED=true`. When enabled, every processed batch is scored and persisted. The `/v1/jobs/{id}/score/report` endpoint returns a Markdown audit report.

---

## Authentication

### Pluggable backend providers

Authentication mode is selected at startup via `MEDANON_AUTH_PROVIDER`. The selection logic lives in `api/auth_providers.py` (`get_provider()`); all providers implement the `AuthProvider` Protocol.

| `MEDANON_AUTH_PROVIDER` | Provider class | Behaviour |
|---|---|---|
| `auto` (default) | `ApiKeyProvider` or `OpenProvider` | API-key mode if `MEDANON_API_KEY` is set, else open |
| `apikey` | `ApiKeyProvider` | Require `X-API-Key` header on all protected endpoints |
| `oidc` | `OidcProvider` | Validate OIDC/JWT bearer tokens; optionally also accept `X-API-Key` when `MEDANON_AUTH_ALLOW_API_KEY=true` |
| `none` | `OpenProvider` | Fully open  local dev only |

**Open paths** (never require auth regardless of mode): `/health`, `/ready`, `/metrics`, `/docs`, `/openapi.json`, `/redoc`, `/.well-known/smart-configuration`, `/v1/auth/config`.

**RBAC roles:** `admin` (all endpoints) → `analyst` (processing + jobs + analytics + AI) → `viewer` (read-only). Role resolution for parameterized paths is handled by `ENDPOINT_ROLE_PREFIXES` in `api/auth.py`.

### OIDC / Keycloak flow

When `MEDANON_AUTH_PROVIDER=oidc`, the backend (`OidcProvider` + `integrations/oidc/validator.py`) validates JWTs against the issuer's JWKS keys. The SPA adapts its login flow at runtime by calling `GET /v1/auth/config`.

The bundled identity provider is Keycloak (realm `medanon`, client `medanon-ui`), started with `--profile auth`. The UI nginx proxies `/auth/` → `medanon-keycloak:8080`, so Keycloak is browser-reachable on the same origin as the SPA  the OIDC issuer is therefore a `/auth`-prefixed URL (e.g. `http://localhost:8501/auth/realms/medanon`), not the internal container address.

The `/v1/auth/config` response carries **no token URL**  the SPA derives Keycloak's `/protocol/openid-connect/{token,logout}` endpoints from `oidc_issuer` itself (`api/auth.ts → endpoints()`):

```
Browser / SPA (AuthContext.tsx)
   │
   │  1. GET /api/v1/auth/config
   │     → { provider: "oidc", oidc_enabled: true,
   │          oidc_issuer: "http://localhost:8501/auth/realms/medanon",
   │          oidc_client_id: "medanon-ui",
   │          oidc_scope: "openid profile email",
   │          api_key_accepted: true }
   │
   │  2. User submits login form (LoginPage.tsx)
   │     POST <issuer>/protocol/openid-connect/token
   │          grant_type=password, client_id=medanon-ui, username, password, scope
   │     ← { access_token, refresh_token, expires_in }
   │
   │     access_token  → memory only (never written to storage)
   │     refresh_token → sessionStorage (tab-scoped; cleared on tab close)
   │
   │  3. All API calls add:  Authorization: Bearer <access_token>
   │     OidcProvider validates JWT signature (JWKS) + iss + exp → roles → RBAC
   │
   │  4. Auto-refresh: setTimeout(expires_in - 60s)
   │     POST <issuer>/protocol/openid-connect/token  grant_type=refresh_token
   │     Keycloak rotates the refresh token on every use → always persist latest
   │
   │  5. Logout: POST <issuer>/protocol/openid-connect/logout (best-effort revoke)
   │             + clear access_token from memory
   │             + remove refresh_token from sessionStorage
   │
   └─ On page reload: read refresh_token from sessionStorage → silent refresh
      → restore session without showing login form again
```

**Why Direct Access Grant (username/password) instead of Authorization Code + redirect?**
The SPA renders its own `LoginPage` and posts credentials directly to Keycloak's token endpoint (Resource Owner Password Credentials grant; the `medanon-ui` client has `directAccessGrantsEnabled: true`). This keeps the user inside the product UI rather than redirecting to Keycloak's hosted login page. The password-reset link is the one place that *does* redirect: `ResetPasswordPage` sends the user to Keycloak's `login-actions/reset-credentials` flow.

**SPA auth components** (all in `client/src/`):

| Component / Module | Role |
|---|---|
| `context/AuthContext.tsx` | Boots by fetching `/v1/auth/config`, restores session from `sessionStorage`, schedules token refresh, exposes `loginWithCredentials()` / `logout()` |
| `components/auth/AuthGate.tsx` | Wraps the app; renders the login route when `isAuthenticated=false` in OIDC mode |
| `components/auth/UserMenu.tsx` | Top-bar dropdown showing username, role, and logout button |
| `pages/LoginPage.tsx` | Username/password form; calls `AuthContext.loginWithCredentials()` |
| `pages/ResetPasswordPage.tsx` | Redirects to Keycloak's `reset-credentials` flow (`api/auth.ts → resetPasswordUrl()`) |
| `api/auth.ts` | `fetchAuthConfig()`, `passwordLogin()`, `refreshLogin()`, `serverLogout()`, `resetPasswordUrl()`, `decodeJwt()` (client-side display only  backend re-verifies) |
| `api/authToken.ts` | In-memory access-token bridge  `getAccessToken()` read by `api/client.ts`, `setAccessToken()` written by `AuthContext` |

**Role mapping (`integrations/oidc/validator.py`):** the JWT's role list is read from the dotted `OIDC_ROLE_CLAIM_PATH` (default `realm_access.roles`) and translated by `OIDC_ROLE_MAP`. An authenticated token with no *recognized* role defaults to `viewer` rather than being rejected. The SPA separately strips the `medanon-` prefix from realm roles for display.

**Downgrade guard:** if a Bearer token's unverified `iss` matches `OIDC_ISSUER`, it is validated strictly  a failure returns 401 and never falls through to the API-key path. A Bearer from a *different* issuer (e.g. a SMART launch token) is routed to SMART introspection instead.

**Azure AD swap:** point `OIDC_ISSUER`/`OIDC_AUDIENCE` at the tenant and set `OIDC_ROLE_CLAIM_PATH=roles`. No code changes.

### Per-client API keys

When using API-key mode, a single shared key (`MEDANON_API_KEY`) works for simple setups. For multi-client setups, issue per-client keys stored as bcrypt hashes in `medanon.api_keys`:

```bash
curl -X POST http://localhost:8000/v1/api-keys \
  -H 'X-API-Key: <admin-key>' \
  -d '{"client":"research-team","role":"analyst"}'
# returns the key once; manage via /v1/api-keys/*
```

Implemented in `api/routers/api_keys.py` + `integrations/postgres/api_key_store.py`.

**Health check strategy:** Docker's `healthcheck` targets `/health` (lightweight, no external calls). The `depends_on: condition: service_healthy` chain requires this. `/ready` probes FHIR and gPAS with a 5 s timeout and is used for readiness gates, not for healthcheck polling.

---

## Performance tuning

| Variable | Default | Effect |
|---|---|---|
| `MEDANON_JOB_WORKERS` | 3 | Concurrent background jobs per anonymizer instance |
| `MEDANON_BATCH_SIZE` | 1000 | Resources per gPAS batch (1 gPAS HTTP call per batch) |
| `FHIR_PAGE_SIZE` | 500 | Resources per FHIR paginated fetch |
| `MEDANON_FHIR_FETCH_PARALLEL` | 1 | Parallel FHIR resource-type fetch threads |
| `MEDANON_COHORT_PARALLEL` | 2 | Parallel `$everything` threads for cohort export |

**Why `FHIR_FETCH_PARALLEL=1`?** The pipeline bottleneck is gPAS (sequential HTTP calls per batch). Adding parallel FHIR fetch threads makes them compete for the GIL and the internal queue lock while gPAS is processing, this adds overhead without reducing total processing time. Sequential fetching avoids this contention. `COHORT_PARALLEL=2` is safe because cohort is I/O-bound (waiting on FHIR server), not CPU-bound.

**Memory:** See [DEPLOYMENT.md § Resource limits](../how-to/deploy-docker.md) for container memory allocations. NLP inference runs in the separate NLP microservice, not in the anonymizer process.
