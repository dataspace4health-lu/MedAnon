# MedAnon — Architecture

## What it does

MedAnon is a FHIR R4 de-identification engine. It accepts FHIR resources (JSON / NDJSON / XML), applies configurable match-action rules from a YAML profile, and returns transformed data with PII removed or pseudonymized. It is designed to be the privacy layer between an identified FHIR source and any downstream consumer — research databases, dataspace connectors, analytics pipelines.

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
│  worker :9091(metrics│   │  source-net (isolated — no host  │
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
  --profile ha  → gpas-db-replica (PostgreSQL streaming replica)
  --profile s3  → minio:9000 (S3-compatible object storage)
  --profile ai  → ollama (local LLM for AI agents)
```

**Two separate FHIR servers** — identified and de-identified data never share a database. This is a deliberate design: it prevents accidental joins, satisfies physical separation requirements under GDPR Art. 25 (data minimization by design), and allows different access controls per server.

**Why a dedicated NLP microservice?** The Presidio + spaCy `en_core_web_lg` model adds ~800 MB to the Docker image. Running it in the anonymizer process would double memory consumption for every anonymizer replica. The NLP microservice keeps this cost fixed regardless of anonymizer scaling, and its replicas can be independently sized for CPU-intensive NLP workloads.

**NLP cache hierarchy.** Each NLP replica keeps an L1 in-process LRU (~20 k entries) and consults an optional Redis L2 cache on DB 2 (`NLP_REDIS_URL`, default key prefix `medanon:nlp:detect:`, default TTL 7 days). L2 is shared across replicas and survives restarts, eliminating cold-cache penalties on deploy. Redis errors fail-soft: the lookup falls through to compute. Implemented in `services/nlp/src/cache.py`.

---

## Services

**Always-on (14 services):**

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | 8000 | FastAPI de-identification engine |
| `worker` | `medanon:latest` | 9091 (metrics) | Dedicated async job worker (Prometheus) |
| `ui` | `medanon-ui:latest` | 8501 | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | none (isolated) | Source FHIR R4 (identified data) |
| `hapi-db` | `postgres:16-alpine` | internal | PostgreSQL for source HAPI |
| `fhir-target` | `hapiproject/hapi:v7.6.0` | 8082 | Target FHIR R4 (de-identified data) |
| `hapi-target-db` | `postgres:16-alpine` | internal | PostgreSQL for target HAPI |
| `gateway` | `traefik:v3` | 8080 (gPAS), 8200 (NLP) | Traefik v3 API gateway — Docker-provider service discovery; carries `gpas-lb` and `nlp-lb` network aliases for backward compatibility |
| `gpas` | WildFly 38 + gPAS | via gateway | Reversible pseudonymization (TTP); scaled with `--scale gpas=N` |
| `gpas-db` | `postgres:16-alpine` | internal | gPAS pseudonym store |
| `app-db` | `postgres:16-alpine` | internal | Jobs, configs, subscriptions, staging |
| `redis` | `redis:7-alpine` | internal | Shared job queue + gPAS L2 cache (password-protected) |
| `analytics` | `medanon-analytics:latest` | 8100 | Risk analysis + synthetic data |
| `nlp` | `medanon-nlp:latest` | via gateway | Presidio NLP microservice (~800 MB image); scaled with `--scale nlp=N` |

**Opt-in profiles (started with `--profile <name>`):**

| Container | Profile | Host Port | Role |
|---|---|---|---|
| `gpas-db-replica` | `ha` | internal | PostgreSQL streaming replica for gPAS HA |
| `minio` | `s3` | 9000, 9001 | S3-compatible object storage for job results |
| `ollama` | `ai` | internal | Local LLM inference for AI agents |

### Edge & routing

Two edge components handle ingress, routing, and horizontal scaling:

| Component | Container | Role |
|--------|-----------|---------|
| `client/nginx.conf` | `medanon-ui` | UI reverse proxy: SPA serving + `/api/*`, `/fhir/*`, `/fhir-target/*` routing |
| Traefik labels on `gpas` / `nlp` | `medanon-gateway` | API gateway: dynamic Docker-provider discovery, round-robin LB, sticky sessions for the gPAS JSF UI |

**Migration note (P2.1, April 2026):** The legacy standalone `gpas-lb` and `nlp-lb` nginx containers were replaced by a single Traefik v3 gateway. The `gateway` service joins `processing-net` under both legacy aliases (`gpas-lb`, `nlp-lb`) so existing `GPAS_URL=http://gpas-lb:80/...` and `NLP_SERVICE_URL=http://nlp-lb:8200` values continue to work without `.env` changes. The standalone `services/gpas/lb/nginx.conf` and `services/nlp/nginx.conf` are no longer mounted by docker-compose and are kept only as reference.

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

**Scaling (Traefik discovers new replicas via Docker labels — no config reload):**
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
config_service.py      Load YAML profile. @lru_cache(maxsize=8) — loaded once per profile
                       per process. Env vars interpolated with ${VAR:-default} syntax.
    │
    ▼
rule_matcher.py        Build per-resource-type rule index using FHIRPath expressions.
                       Results cached per (resource_type, rules_hash) to avoid repeat
                       FHIRPath evaluation on identical inputs.
    │
    ▼
action_dispatcher.py   Pass 1: Iterate matched rules. Apply stateless actions immediately
                       (redact, cryptohash, generalize, substitute, perturb, scrub_text,
                       encrypt). Defer NLP actions (nlp_scrub, nlp_detect_act) into
                       NlpWork items. Collect gPAS-bound values into BatchWork.
                       Neither NLP nor gPAS is called yet — batching is critical
                       for throughput.
    │
    ▼
nlp_orchestrator.py    Pass 1.5: Batch NLP detection across all resources.
                       Phase A — extract text fields from all deferred NlpWork items.
                       Phase B — deduplicate and batch-detect unique texts (one HTTP
                       call for remote NLP service, or cache-prewarming for local
                       Presidio). Phase C — per-resource replacement with isolated
                       token_state so surrogate tokens are deterministic within a
                       resource but unique across resources.
    │
    ▼
gpas_orchestrator.py   Pass 2: Send one HTTP request to gPAS for all collected values.
                       Checks cache first (local LRU → Redis L2 → live call).
                       Results written back into the resource tree.
    │
    ▼
post_processor.py      Rewrite FHIR references (Patient/123 → Patient/p-123) and
                       replace pseudonym-changed IDs that appear in text fields.
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

**Why three passes?** Both NLP and gPAS have non-trivial per-call overhead. Processing 300 resources with individual calls would be ~300 HTTP round-trips for each. Pass 1 collects deferred work (NlpWork for NLP, BatchWork for gPAS) without making any external calls. Pass 1.5 deduplicates texts across all resources and sends a single batch to the NLP service (or pre-warms the local Presidio cache), then applies replacements per-resource. Pass 2 does the same for gPAS pseudonymization. This reduces hundreds of HTTP calls to 2-3 regardless of resource count.

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
- `Organization` (tier 0 — no deps)
- `Practitioner` (tier 0 — no deps)
- `Encounter` (tier 1 — references Organization + Practitioner)
- `Observation` (tier 2 — references Encounter)
- `DiagnosticReport` (tier 3 — references Observation)

**Solution:** Bellman-Ford relaxation over the reference graph. `tier[A] = 1 + max(tier[B] for B in deps[A])`. Converges in at most N passes where N = number of distinct resource types. Resources within the same tier upload in a single batch Bundle.

---

## Reference rewriting after ID sanitization

**Problem:** HAPI FHIR rejects purely numeric resource IDs (HAPI-0960). The de-identification pipeline may produce numeric IDs (e.g. gPAS pseudonyms are sometimes numeric, or the source FHIR server uses numeric auto-increment IDs). These are prefixed with `p-` before upload (e.g. `Patient/123` → `Patient/p-123`).

**Consequence:** Any resource that contains `{"reference": "Patient/123"}` will now point to a non-existent resource — causing HAPI-1094 on upload.

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

Six bundled profiles plus one specialist profile. Auto-selected based on environment; overridable per-request via `?config_profile=<name>`.

| Profile | ID handling | Dates | Geographic | Requires gPAS | Use case |
|---|---|---|---|---|---|
| `config.yaml` | SHA3-256 hash | Year only | Zip prefix (3-digit) | No | Local dev / testing |
| `config_gpas.yaml` | gPAS pseudonym (reversible) | Year only | Zip prefix | Yes | Production with re-linkage |
| `config_gdpr_eu.yaml` | HMAC SHA3-256 | Redacted | Redacted | No | GDPR Art. 4(5) |
| `config_hipaa_safe_harbor.yaml` | Redacted | Year only | State + 3-digit zip | No | US HIPAA Safe Harbor |
| `config_research_pseudonymous.yaml` | SHA3-256 hash | Year-month | 3-digit zip | No | IRB research |
| `config_structure_preserving.yaml` | gPAS pseudonym (reversible) | Year (birthDate only) | Preserved | Yes | Full FHIR structure downstream |
| `config_value_masking.yaml` | gPAS pseudonym (reversible) | Decade (birth), year (clinical) | Masked to `[REDACTED]` | Yes | Field-complete de-identification with `nlp_detect_act` for entity-specific conditional NLP |

Auto-selection logic: `GPAS_URL` set → `config_gpas.yaml`; otherwise → `config.yaml`.

---

## AI agents (Phase 4)

Four AI-powered agents are exposed via `/v1/ai/*` when `MEDANON_AI_ENABLED=true`:

| Agent | Endpoint | Description |
|---|---|---|
| **Config generator** | `POST /v1/ai/generate-config` | RAG-based YAML profile generation from natural language. Uses the 7 bundled profiles as few-shot examples. Validates output through the Settings loader before returning. Falls back to keyword matching when AI is disabled. |
| **PII detector** | `POST /v1/ai/detect-pii` | 3-layer PII scan on de-identified output: regex → NER → LLM. PHI safety boundary: the LLM used for PII detection must be local/self-hosted (`MEDANON_AI_PII_PROVIDER`). |
| **Rule explainer** | `POST /v1/ai/explain` | Plain-language explanation of config rules via SSE streaming. Falls back to static descriptions when AI is unavailable. |
| **Compliance advisor** | `POST /v1/ai/compliance` | Regulatory gap analysis vs HIPAA, GDPR, and other frameworks. Static HIPAA fallback when AI is unavailable. |

**LLM provider:** `integrations/ai/provider.py` wraps `litellm`, which supports OpenAI-compatible APIs (OpenAI, Azure OpenAI, Anthropic) and Ollama for local inference. The `--profile ai` Docker profile starts an Ollama container.

**Known limitations (tracked):**
- PHI must not be sent to external LLM providers. Code-level enforcement for the PII detector's AI layer is pending.
- Prompt injection: user input is interpolated into LLM messages in `config_generator.py`.
- AI response cache (`LLMProvider._cache`) has no eviction — grows unbounded.

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

## Authentication

| `MEDANON_API_KEY` | Behaviour |
|---|---|
| Unset | All endpoints open (dev only) |
| Set | All endpoints except `/health`, `/ready`, `/metrics`, `/docs` require `X-API-Key: <key>` |

RBAC roles: `admin` (all), `analyst` (processing + jobs + scoring + AI), `viewer` (read-only).

**Health check strategy:** Docker's `healthcheck` targets `/health` (lightweight — returns `{"status":"ok"}` immediately). The `depends_on: condition: service_healthy` chain requires this. `/ready` is more expensive — it probes FHIR and gPAS connectivity with a 5 s timeout each — and is used for readiness gates only, not for Docker's healthcheck polling.

---

## Performance tuning

| Variable | Default | Effect |
|---|---|---|
| `MEDANON_JOB_WORKERS` | 3 | Concurrent background jobs per anonymizer instance |
| `MEDANON_BATCH_SIZE` | 300 | Resources per gPAS batch (1 gPAS HTTP call per batch) |
| `FHIR_PAGE_SIZE` | 500 | Resources per FHIR paginated fetch |
| `MEDANON_FHIR_FETCH_PARALLEL` | 1 | Parallel FHIR resource-type fetch threads |
| `MEDANON_COHORT_PARALLEL` | 2 | Parallel `$everything` threads for cohort export |

**Why `FHIR_FETCH_PARALLEL=1`?** The pipeline bottleneck is gPAS (sequential HTTP calls per batch). Adding parallel FHIR fetch threads makes them compete for the GIL and the internal queue lock while gPAS is processing — this adds overhead without reducing total processing time. Sequential fetching avoids this contention. `COHORT_PARALLEL=2` is safe because cohort is I/O-bound (waiting on FHIR server), not CPU-bound.

**Memory:** Anonymizer container is allocated 6 GB / 2 CPU. A bulk export of ~20,000 resources uses ~5.7 GB peak (gPAS pseudonym cache + in-flight resource batches + NDJSON write buffer).
