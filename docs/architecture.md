# MedAnon  Architecture

## What it does

MedAnon is a FHIR R4 de-identification engine. It accepts FHIR resources (JSON / NDJSON / XML), applies configurable match-action rules from a YAML profile, and returns transformed data with PII removed or pseudonymized. It is designed to be the privacy layer between an identified FHIR source and any downstream consumer  research databases, dataspace connectors, analytics pipelines.

---

## System topology

```
┌─────────────────────────────────────────────────────────────┐
│  Browser                                                    │
└──────────────────────┬──────────────────────────────────────┘
                       │ https://host:8501  (TLS terminates here)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  nginx (medanon-ui)  PATH PROXY ONLY, NOT AN AUTH GATE.     │
│  No auth_request: nginx checks no credential. Each upstream │
│  enforces its own auth, or enforces none at all.            │
│                                                             │
│   /api/*        → anonymizer:8000   ◄── ONLY GATED ROUTE    │
│   /auth/*       → keycloak:8080     [auth] token issuer     │
│   /auth/callback→ React SPA (exact match beats /auth/)      │
│   /fhir/*       → hapi-fhir         !! NO AUTH: IDENTIFIED  │
│   /fhir-target/*→ hapi-fhir-target  !! NO AUTH (de-ident.)  │
│   /trust/*      → trust-gate:8400   !! NO AUTH  [trust]     │
│   /grafana/*    → grafana           !! NO AUTH (anon Viewer)│
│   /prometheus/* → prometheus        !! NO AUTH  [monitoring]│
│   /             → React SPA                                 │
└──────────┬───────────────────────────┬──────────────────────┘
           │ Bearer JWT                │ 1. login  2. issue JWT
           ▼                           ▼
┌──────────────────────┐   ┌───────────────────────┐
│  anonymizer :8000    │   │  keycloak      [auth] │
│  ══ AUTH BOUNDARY ══ │   │  OIDC IdP + its own DB│
│  validates key/JWT   │   │  TOKEN ISSUER, not an │
│  per request, in     │   │  inline gate: no proxy│
│  FastAPI (api/auth)  │   │  traffic flows via it │
│  (FastAPI)           │   │  NO host port: served │
│  worker :9091(metrics│   │  only through the UI  │
│  reads  ──────────►  │   │  TLS edge at /auth/   │
│  de-identifies       │   └───────────────────────┘
│  writes ──────────►  │   ┌──────────────────────────────────┐
│                      │◄──┤  hapi-fhir (source) :8080        │
│                      │   │  PostgreSQL-backed FHIR R4       │
│                      │   │  source-net: no host port, BUT   │
│                      │   │  ui joins it too (see /fhir/*)   │
│                      │   └──────────────────────────────────┘
│                      │   ┌──────────────────────────────────┐
│                      │──►│  hapi-fhir-target :8082          │
└──────┬───────────────┘   │  PostgreSQL-backed FHIR R4       │
       │                   │  Stores DE-IDENTIFIED data only  │
       │                   └──────────────────────────────────┘
       │
       ├──► keycloak [auth]  realm JWKS, fetched backchannel to validate
       │     every Bearer JWT (OIDC_ISSUER; OIDC_DISCOVERY_BASE when the
       │     public issuer URL is not reachable from inside the network)
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
       ├──► scoring:8300 (privacy × utility × quality)
       │
       ├──► app-db:5432 (jobs, configs, subscriptions, staging)
       │
       ├──► redis:6379 (password-protected)
       │     ├── job queue  (Redis Streams, event-driven)
       │     └── gPAS cache (L2, cross-replica)
       │
       ├──► minio:9000   [s3]  job results + saved output destinations
       │     console :9001     active when MEDANON_RESULT_STORAGE=s3
       │
       └──► ollama:11434 [ai]  local LLM behind the AI agents
             active when MEDANON_AI_ENABLED=true; PHI-bearing prompts
             are pinned to a local/self-hosted endpoint, never a hosted API

Situational profiles (not drawn):
  --profile ha         → gpas-db-replica (PostgreSQL streaming replica)
  --profile sqltest    → sql-source-test-db (SQL/tabular source fixture)
  --profile trust      → trust-gate + trust-gate-ui + fhir-validator
  --profile monitoring → prometheus + grafana + cadvisor + jaeger
```

**`[auth]`, `[s3]`, and `[ai]` are drawn because a real deployment runs them,** even though compose still gates them behind profiles. `make up` on its own starts the 15 always-on services only; the reference deployment adds `--profile auth --profile s3 --profile ai`. Each of the three needs its profile **and** the env flag that points the anonymizer at it (`MEDANON_AUTH_PROVIDER=oidc`, `MEDANON_RESULT_STORAGE=s3`, `MEDANON_AI_ENABLED=true`) - starting the container alone changes nothing.

**Where authentication actually happens.** Auth is **not** a gate in front of the stack. nginx uses no `auth_request` and validates no credential; Keycloak is a token issuer that no proxied traffic passes through. The single enforcement point is inside the anonymizer, applied per request by `api/auth.py`, and it therefore protects **only** what nginx routes to `/api/*`. The other proxied upstreams answer the same TLS edge with no credential at all, including `/fhir/*`, which reaches the identified source server. See [security.md § 1.4](security.md#14-edge-routes-that-bypass-authentication) for the exposure and the options for closing it. A deployment that assumes "the login screen protects the data" is mistaken: the SPA is behind a login, but the routes underneath it are not.

**Two separate FHIR servers**  identified and de-identified data never share a database. This is a deliberate design: it prevents accidental joins, satisfies physical separation requirements under GDPR Art. 25 (data minimization by design), and allows different access controls per server.

**Why a dedicated NLP microservice?** The Presidio + spaCy `en_core_web_lg` model adds ~800 MB to the Docker image. Running it in the anonymizer process would double memory consumption for every anonymizer replica. The NLP microservice keeps this cost fixed regardless of anonymizer scaling, and its replicas can be independently sized for CPU-intensive NLP workloads.

**NLP cache hierarchy.** Each NLP replica keeps an L1 in-process LRU (~20 k entries) and consults an optional Redis L2 cache on DB 2 (`NLP_REDIS_URL`, default key prefix `medanon:nlp:detect:`, default TTL 7 days). L2 is shared across replicas and survives restarts, eliminating cold-cache penalties on deploy. Redis errors fail-soft: the lookup falls through to compute. Implemented in `services/nlp/src/cache.py`.

---

## Services

**Always-on (15 services):**

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | 8000 | FastAPI de-identification engine |
| `worker` | `medanon:latest` | 9091 (metrics) | Dedicated async job worker (Prometheus) |
| `ui` | `medanon-ui:latest` | 8501 | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | none (isolated) | Source FHIR R4 (identified data) |
| `hapi-db` | `postgres:16-alpine` | internal | PostgreSQL for source HAPI |
| `fhir-target` | `hapiproject/hapi:v7.6.0` | 8082 | Target FHIR R4 (de-identified data) |
| `hapi-target-db` | `postgres:16-alpine` | internal | PostgreSQL for target HAPI |
| `gateway` | `traefik:v3` | 8080 (gPAS), 8200 (NLP) | Traefik v3 API gateway  Docker-provider service discovery; carries `gpas-lb` and `nlp-lb` network aliases for backward compatibility |
| `gpas` | WildFly 38 + gPAS | via gateway | Reversible pseudonymization (TTP); scaled with `--scale gpas=N` |
| `gpas-db` | `postgres:16-alpine` | internal | gPAS pseudonym store |
| `app-db` | `postgres:16-alpine` | internal | Jobs, configs, subscriptions, staging |
| `redis` | `redis:7-alpine` | internal | Shared job queue + gPAS L2 cache (password-protected) |
| `analytics` | `medanon-analytics:latest` | 8100 | Risk analysis + synthetic data |
| `scoring` | `medanon-scoring:latest` | internal (8300) | Privacy × utility × quality scoring microservice |
| `nlp` | `medanon-nlp:latest` | via gateway | Presidio NLP microservice (~800 MB image); scaled with `--scale nlp=N` |

**Reference deployment (profile-gated, but running in any real install):**

These three carry compose `profiles:` and so do not start with a bare `make up`, but a deployment that authenticates users, stores results off-box, or exposes the AI agents runs all of them. Starting the container is only half the wiring: each also needs the env flag that points the anonymizer at it.

| Container | Profile | Host Port | Wired in by | Role |
|---|---|---|---|---|
| `keycloak` + `keycloak-db` | `auth` | none (via UI TLS edge) | `MEDANON_AUTH_PROVIDER=oidc` + `OIDC_ISSUER` | OIDC identity provider + its PostgreSQL. Deliberately has no published port: the admin console is a PKCE public client and needs an HTTPS secure context, so it is served only under `/auth/` on the UI's TLS edge. |
| `minio` | `s3` | 9000 (API), 9001 (console) | `MEDANON_RESULT_STORAGE=s3` + `MINIO_*` | S3-compatible object storage for job results and saved output destinations. `MINIO_ENDPOINT` defaults to `minio:9000`, bucket `medanon-results`. |
| `ollama` | `ai` | 11434 | `MEDANON_AI_ENABLED=true` + `MEDANON_AI_API_BASE` | Local LLM inference behind the AI agents. `MEDANON_AI_API_BASE` defaults to `http://ollama:11434`. Any prompt that can carry PHI is pinned to a local endpoint (see [AI agents](#ai-agents-phase-4)). |

**Situational profiles:**

| Container | Profile | Host Port | Role |
|---|---|---|---|
| `gpas-db-replica` | `ha` | internal | PostgreSQL streaming replica for gPAS HA |
| `sql-source-test-db` | `sqltest` | 55432 | PostgreSQL fixture for the SQL/tabular source connector |
| `trust-gate` + `trust-gate-ui` | `trust` | 8400, 8401 | Pre-privacy Quality Passport service + its UI |
| `fhir-validator` | `trust` | internal | Inferno HL7 FHIR validator (backs Trust Gate conformance) |
| `prometheus` / `grafana` / `cadvisor` / `jaeger` | `monitoring` | 9090 / 3000 / 8888 / 16686 | Metrics, dashboards, container metrics, traces |

RabbitMQ macro-stage streaming has no bundled compose service; set `MEDANON_AMQP_URL` to an external broker to enable it.

**Shared code (`packages/medanon-core`).** The domain contracts (`domain`), the analytics engine (`analytics`), and the scoring engine (`scoring`) are a single zero-dependency inner package rather than per-service copies. The anonymizer puts it on its path directly; the `scoring` and `analytics` microservices (and trust-gate, for the `domain.trust` phase vocabulary) `pip install` it in their Dockerfiles, so every service runs identical shared logic. See [components.md](components.md).

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

**Scaling (Traefik discovers new replicas via Docker labels  no config reload):**
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
config_service.py      Load YAML profile. @lru_cache(maxsize=8)  loaded once per profile
                       per process. Env vars interpolated with ${VAR:-default} syntax.
    │
    ▼
rule_matcher.py        Build per-resource-type rule index using FHIRPath expressions.
                       Results cached per (resource_type, rules_hash) to avoid repeat
                       FHIRPath evaluation on identical inputs.
    │
    ▼
action_dispatcher.py   STAGE 1  match: Iterate matched rules. Apply stateless actions
                       immediately (redact, cryptohash, generalize, substitute, perturb,
                       scrub_text, encrypt). Defer NLP actions (nlp_scrub, nlp_detect_act)
                       into NlpWork items. Collect gPAS-bound values into BatchWork.
                       Neither NLP nor gPAS is called yet  batching is critical
                       for throughput.
    │
    ▼  ┌─────────── STAGES 2 & 3 RUN CONCURRENTLY ───────────┐
       │  (disjoint resource paths, so it is safe to overlap) │
nlp_orchestrator.py    STAGE 2  phi_detection: Batch NLP detection across all resources.
                       Phase A  extract text fields from all deferred NlpWork items.
                       Phase B  deduplicate and batch-detect unique texts (one HTTP
                       call to the NLP service). Phase C  per-resource replacement with
                       isolated token_state so surrogate tokens are deterministic within
                       a resource but unique across resources.
                                          ‖  (concurrent with)
gpas_orchestrator.py   STAGE 3  pseudonymize: Send one HTTP request to gPAS for all
                       collected values. Checks cache first (local LRU → Redis L2 → live
                       call). Results written back into the resource tree.
    │  └──────────────────────────────────────────────────────┘
    ▼
post_processor.py      STAGE 4  finalize: Rewrite FHIR references (Patient/123 →
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

**Why staged + concurrent?** Both NLP and gPAS have non-trivial per-call overhead. Processing 300 resources with individual calls would be ~300 HTTP round-trips for each. The **match** stage collects deferred work (NlpWork for NLP, BatchWork for gPAS) without making any external calls. The **phi_detection** and **pseudonymize** stages then run **concurrently**: NLP deduplicates texts across all resources and sends a single batch, while gPAS does the same for pseudonymization  they touch disjoint resource paths, so overlapping them hides one upstream's latency behind the other. This reduces hundreds of HTTP calls to 2-3 regardless of resource count, and the two batch calls overlap rather than running back-to-back. Per-stage latency is exported as `medanon_pipeline_stage_duration_seconds{stage=...}`.

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
- `Organization` (tier 0  no deps)
- `Practitioner` (tier 0  no deps)
- `Encounter` (tier 1  references Organization + Practitioner)
- `Observation` (tier 2  references Encounter)
- `DiagnosticReport` (tier 3  references Observation)

**Solution:** Bellman-Ford relaxation over the reference graph. `tier[A] = 1 + max(tier[B] for B in deps[A])`. Converges in at most N passes where N = number of distinct resource types. Resources within the same tier upload in a single batch Bundle.

---

## Reference rewriting after ID sanitization

**Problem:** HAPI FHIR rejects purely numeric resource IDs (HAPI-0960). The de-identification pipeline may produce numeric IDs (e.g. gPAS pseudonyms are sometimes numeric, or the source FHIR server uses numeric auto-increment IDs). These are prefixed with `p-` before upload (e.g. `Patient/123` → `Patient/p-123`).

**Consequence:** Any resource that contains `{"reference": "Patient/123"}` will now point to a non-existent resource  causing HAPI-1094 on upload.

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

## AI agents (Phase 4)

Four AI-powered agents are exposed via `/v1/ai/*` when `MEDANON_AI_ENABLED=true`:

| Agent | Endpoint | Description |
|---|---|---|
| **Config generator** | `POST /v1/ai/generate-config` | YAML profile generation from natural language. All 8 bundled profiles injected as few-shot examples (prompt context, not vector retrieval). Validates output through the Settings loader before returning. Falls back to keyword matching when AI is disabled. |
| **PII detector** | `POST /v1/ai/detect-pii` | 3-layer PII scan on de-identified output: regex → NER → LLM. PHI safety boundary: the LLM used for PII detection must be local/self-hosted (`MEDANON_AI_PII_PROVIDER`). |
| **Rule explainer** | `POST /v1/ai/explain` | Plain-language explanation of config rules via SSE streaming. Falls back to static descriptions when AI is unavailable. |
| **Compliance advisor** | `POST /v1/ai/compliance` | Regulatory gap analysis vs HIPAA, GDPR, and other frameworks. Static HIPAA fallback when AI is unavailable. |

**LLM provider:** `integrations/ai/provider.py` wraps `litellm`, which supports OpenAI-compatible APIs (OpenAI, Azure OpenAI, Anthropic) and Ollama for local inference. The `ai` profile starts an Ollama container on `processing-net`; `MEDANON_AI_API_BASE` (default `http://ollama:11434`) can equally point at an Ollama host outside the stack, which is how a GPU box is attached. Nothing in the AI path activates until `MEDANON_AI_ENABLED=true`.

**PHI safety boundary.** `LLMProvider` calls take a `phi_payload` flag that defaults to `True` (fail-closed). When set, `integrations/ai/local_guard.py::require_local` rejects the call unless the endpoint is provably self-hosted, so a PHI-bearing prompt cannot reach a hosted provider even if one is configured. Requests that carry no PHI (explaining a rule, generating YAML) opt out with `phi_payload=False`. The PII detector re-checks this early, before any resource content is assembled into a prompt.

"Self-hosted" means a litellm provider prefix that routes locally by default (`ollama/`, `vllm/`, `lm_studio/`, `local/`), a known local hostname (`ollama`, `localhost`, `host.docker.internal`), or an endpoint resolving to loopback, RFC1918, or link-local space. This is a **private-network** boundary, not a single-host one: an Ollama VM at `10.x.x.x` is accepted, so treat that host as in-scope for PHI handling. Two flags control enforcement: `MEDANON_AI_PII_REQUIRE_LOCAL` (default `true`) covers PHI payloads, and `MEDANON_AI_REQUIRE_LOCAL` (default `false`) is a site-wide lock that forces **every** LLM call local regardless of `phi_payload`.

**Other guards:** `prompt_guard.py` sanitizes and tag-wraps user input before it reaches `config_generator.py` (prompt injection); `provider.py` wraps calls in a `CircuitBreaker` and caches responses in a `_BoundedTtlCache` (bounded, TTL-evicted); `api/services/agents.py::_validate_proxy_url` blocks SSRF on the proxy path.

**Known limitations (tracked):**
- No dedicated regression test covers the local, prompt, and SSRF guards.
- `/explain` awaits its SSE producer to completion before draining, so it buffers rather than streaming incrementally. `/chat` streams correctly.

---

## Scoring system

Every de-identification operation can be scored against three dimensions:

| Dimension | What it measures | Weight |
|---|---|---|
| **Privacy** | Re-identification risk (k-anonymity, l-diversity, HIPAA identifier coverage) | Hard constraint |
| **Utility** | Data usefulness for downstream analytics (field retention, date precision, code preservation) | Configurable |
| **Quality** | FHIR structural validity (required fields, valid codes, reference integrity) | Configurable |

The composite score is `privacy × utility × quality` (0.0–1.0). Scores are stored in `medanon.processing_runs` and exposed via `/v1/jobs/{id}/score` and `/v1/processing-runs`.

Scoring is opt-in: `MEDANON_SCORING_ENABLED=true`. When enabled, every processed batch is scored and persisted. The `/v1/jobs/{id}/score/report` endpoint returns a Markdown audit report.

---

## Governance & EHDS compliance

For regulated secondary use (EHDS Arts 45-49, 66, 71, 78-79; TEHDAS2 D7.2), the anonymizer carries a governance layer on top of the de-identification engine. Everything here is additive and inert unless configured, so ordinary de-identification is unchanged.

**Data permits and permit-scoped pseudonymisation.** A `Permit` domain model (`pipeline/governance/permit.py`) enforces a lifecycle state machine (draft → submitted → approved / rejected → revoked; illegal transitions return HTTP 409) with a validity window and a path scope. Permits are managed through `/v1/permits` (admin-only) and persisted in Postgres. When a permit is active for a request, it is propagated via a contextvar (`pipeline/permit_context.py`); the keyed actions (`cryptohash`, `tokenize`, `date_shift`) derive a permit-scoped key with HKDF-SHA256 and gPAS domains are suffixed `__permit-{id}`. The result: the same source subject produces **unrelated** pseudonyms across different permits (D7.2 §4.4, which forbids reusing pseudonyms across purposes), but stable pseudonyms within one permit.

**The assess → decide loop (D7.2 Fig 6).** The risk-driven export executor runs the full "process → assess → decide → release" loop on the actual de-identified output, not just the intended k/l/t of the generalisation lattice:

1. **Opt-out exclusion** (`pipeline/exclusion.py`, EHDS Art 71) drops opted-out subjects and their linked resources before pseudonymisation, matching on the original identifiers a national register would supply.
2. **Privacy-risk assessment** (`analytics/privacy_risk.py`) measures re-identification (k-anonymity), plus distance-to-closest-record / nearest-neighbour ratios and attribute-inference (SDMetrics) for synthetic data.
3. **Disclosure decision** (`pipeline/disclosure/decision.py`) applies transparent Five-Safes output-checking rules - residual direct identifiers, re-id risk, minimum k, synthetic duplicates, unjustified variables, and permit/recipient/scope checks - and returns the most restrictive of APPROVE / REFER / REFUSE (in regulated mode a REFER escalates to REFUSE). A REFUSE deletes the written output and fails the job so nothing is ever exposed.
4. **Transformation Passport** (`pipeline/transformation_passport.py`) records the release: identification, permit, tools + versions, privacy-model intent and achieved k/l/t, privacy-risk results, and the disclosure verdict. It is anonymous by construction and persisted with a structural PII guard (`PostgresPassportStore.assert_pii_safe`).

**Advisory and release endpoints** (evaluate-only, run locally regardless of any microservice split): `/v1/minimise/assess` (minimisation report, D7.2 §3), `/v1/export/decision` (ad-hoc Five-Safes check), `/v1/exposure/assess` (cumulative-exposure / differencing risk across prior releases via a durable release ledger, §5.5.7), `/v1/export/statistical` (aggregate release protected by small-cell suppression and/or differential privacy, §5.5.4), `/v1/catalog/descriptor` (HealthDCAT-AP JSON-LD dataset descriptor, §4.3), `/v1/synthetic/passport`, `/v1/analyse/privacy-risk`, and `/v1/reports` (durable passports).

**Dataspace connectors and instance settings.** Saved, encrypted input sources (FHIR servers) and S3 output destinations (`/v1/source-connections`, `/v1/output-destinations`) make wiring the engine into a dataspace a matter of configuration. Deployment-wide admin defaults live behind `/v1/settings`; the SPA reads a small non-secret slice pre-login from the open `/v1/runtime-config`.

**Regulated mode** (`MEDANON_REGULATED_MODE=true`, `utils/regulated.py`) is one switch that turns fail-soft defaults into hard requirements: no plain-hash fallback for any keyed action, the output and disclosure barriers cannot be disabled, `warn` identifier modes are forced to `block`, disclosure REFER escalates to REFUSE, Trust Gate conformance NA/SKIPPED becomes FAIL, and an unresolvable opt-out source fails closed. Reversal actions (`gpas_depseudonymize`, `decrypt`) require the `admin` role. The flag is read at call time so it can be toggled without re-importing modules.

Dependency note: `Anonymeter`, `SDV`, `torch`, and `opacus` are not installable in this environment, so privacy-risk uses in-house DCR/NNDR/τ-DCR plus SDMetrics, synthesis uses `copulas`, and differential privacy is a standard-library implementation (`analytics/dp.py`: Laplace, analytic Gaussian, and basic-composition budget accounting).

---

## Authentication

Authentication is pluggable. `MEDANON_AUTH_PROVIDER` selects the provider at startup (`api/auth_providers.py`); switching it is an env change plus a restart, no code change.

| `MEDANON_AUTH_PROVIDER` | Accepts | Use for |
|---|---|---|
| `auto` (default) | DB key → env key → SMART bearer → open | Legacy behaviour, preserved verbatim for existing deployments |
| `apikey` | `X-API-Key` only (no bearer, no open access) | Service-to-service and CLI workflows |
| `oidc` | OIDC JWT via `Authorization: Bearer`, plus `X-API-Key` when `MEDANON_AUTH_ALLOW_API_KEY=true` (the default) | Human users behind Keycloak or Azure AD |
| `none` | Everything, as `admin` | Local development only |

In `auto` and `apikey` modes, `MEDANON_API_KEY` is the switch: unset leaves all endpoints open (dev only), set requires `X-API-Key` on everything except `/health`, `/ready`, `/metrics`, and `/docs`.

RBAC roles: `admin` (all), `analyst` (processing + jobs + scoring + AI), `viewer` (read-only). Parameterized paths resolve through `ENDPOINT_ROLE_PREFIXES` in `api/auth.py`.

### OIDC login flow (Keycloak)

Keycloak runs under the `auth` profile with no published host port. Both the SPA's login redirect and Keycloak's own admin console are PKCE public clients, which browsers only allow in an HTTPS secure context, so everything is served through the UI's TLS edge at `/auth/`. A plain-HTTP host port would fail on `crypto.subtle requires HTTPS`. One URL, no split-brain.

```
1. Browser  → https://host:8501/auth/realms/<realm>/protocol/openid-connect/auth
              (nginx /auth/* → keycloak:8080)
2. Keycloak → redirects back to https://host:8501/auth/callback?code=...
              nginx matches `= /auth/callback` EXACTLY, which beats the /auth/
              prefix, so the SPA (not Keycloak) serves the redirect target
3. SPA      → exchanges code for tokens, then sends Authorization: Bearer <JWT>
              on every /api/* call
4. anonymizer → validates the JWT against the realm JWKS, fetched backchannel
```

**JWKS discovery** (`integrations/oidc/validator.py`) resolves in three steps: an explicit `OIDC_JWKS_URL`, else the `jwks_uri` from the discovery document, else a derived `{base}/.well-known/jwks.json`.

**Split-horizon issuer.** The browser reaches Keycloak at its public URL, but the anonymizer sits inside the Docker network where that URL may not resolve. `OIDC_ISSUER` stays the public value (it must match the token's `iss` claim exactly), while `OIDC_DISCOVERY_BASE` points at the internal realm URL for backchannel fetches. `OIDC_DISCOVERY_BASE` falls back to `OIDC_ISSUER` when unset. Keycloak itself needs `KEYCLOAK_PUBLIC_URL` (`KC_HOSTNAME`) set to the same public base so it builds correct `https://` URLs behind the proxy.

**Role mapping.** `OIDC_ROLE_CLAIM_PATH` is a dotted path into the JWT claims (`realm_access.roles` for Keycloak, `roles` for Azure AD) and `OIDC_ROLE_MAP` maps provider roles onto MedAnon's three. An Azure AD swap is env-only.

Two deliberate failure behaviours:

- **Deny by default.** A validly authenticated user whose token carries no mapped role gets zero roles and is rejected with 403, rather than silently receiving access. Assign a realm role or set `OIDC_DEFAULT_ROLE` to grant a floor.
- **No silent downgrade.** If a Bearer token's `iss` matches `OIDC_ISSUER` but validation fails, the request is rejected with 401 instead of falling through to the API-key path. An expired or forged token must not quietly downgrade to a weaker credential.

**Health check strategy:** Docker's `healthcheck` targets `/health` (lightweight  returns `{"status":"ok"}` immediately). The `depends_on: condition: service_healthy` chain requires this. `/ready` is more expensive  it probes FHIR and gPAS connectivity with a 5 s timeout each  and is used for readiness gates only, not for Docker's healthcheck polling.

---

## Performance tuning

| Variable | Default | Effect |
|---|---|---|
| `MEDANON_JOB_WORKERS` | 3 | Concurrent background jobs per anonymizer instance |
| `MEDANON_BATCH_SIZE` | 1000 | Resources per gPAS batch (1 gPAS HTTP call per batch) |
| `FHIR_PAGE_SIZE` | 500 | Resources per FHIR paginated fetch |
| `MEDANON_FHIR_FETCH_PARALLEL` | 1 | Parallel FHIR resource-type fetch threads |
| `MEDANON_COHORT_PARALLEL` | 2 | Parallel `$everything` threads for cohort export |

**Why `FHIR_FETCH_PARALLEL=1`?** The pipeline bottleneck is gPAS (sequential HTTP calls per batch). Adding parallel FHIR fetch threads makes them compete for the GIL and the internal queue lock while gPAS is processing  this adds overhead without reducing total processing time. Sequential fetching avoids this contention. `COHORT_PARALLEL=2` is safe because cohort is I/O-bound (waiting on FHIR server), not CPU-bound.

**Memory:** See [DEPLOYMENT.md § Resource limits](DEPLOYMENT.md) for container memory allocations. NLP inference runs in the separate NLP microservice, not in the anonymizer process.
