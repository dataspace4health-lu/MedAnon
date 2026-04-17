# MedAnon — Component Reference

Technical reference for all services and modules. For architecture diagrams and design decisions see [architecture.md](architecture.md). For data flow traces see [data-flow.md](data-flow.md).

---

## Services

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | 8000 | FastAPI de-identification engine |
| `worker` | `medanon:latest` | 9091 (metrics) | Dedicated async job worker (Prometheus) |
| `ui` | `medanon-ui:latest` | 8501 | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | none (isolated) | Source HAPI FHIR R4 (identified data) |
| `fhir-target` | `hapiproject/hapi:v7.6.0` | 8082 | Target HAPI FHIR R4 (de-identified data) |
| `gpas` | WildFly 38 + gPAS | via gpas-lb | Reversible pseudonymization (TTP) |
| `gpas-lb` | `nginx:1.27-alpine` | 8080 | Round-robin LB for gPAS replicas |
| `gpas-db` | `postgres:16-alpine` | internal | gPAS pseudonym store |
| `app-db` | `postgres:16-alpine` | internal | Jobs, configs, subscriptions, staging |
| `redis` | `redis:7-alpine` | internal | Job queue (BLPOP) + gPAS L2 cache (password-protected) |
| `analytics` | `medanon-analytics:latest` | 8100 | Risk analysis + synthetic data |

Opt-in (not started by default):

| Container | Profile flag | Host Port | Role |
|---|---|---|---|
| `nlp` | `--profile nlp` | via nlp-lb | Presidio NLP (~800 MB, saves anonymizer RAM) |
| `nlp-lb` | `--profile nlp` | 8200 | Least-conn LB for NLP replicas |
| `gpas-db-replica` | `--profile ha` | internal | PostgreSQL read replica for gPAS HA |
| `minio` | `--profile s3` | 9000, 9001 | S3-compatible object storage for job results |

---

## REST API endpoints

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/health` | — | Liveness (fast, no external calls) |
| GET | `/ready` | — | Readiness (probes FHIR + gPAS, 5 s timeout each) |
| GET | `/metrics` | — | Prometheus metrics |
| GET | `/docs` | — | Swagger UI |
| POST | `/process` | analyst | De-identify single resource |
| POST | `/process/batch` | analyst | De-identify NDJSON / Bundle / XML |
| POST | `/process/ndjson` | analyst | NDJSON-only endpoint |
| POST | `/process/raw` | analyst | Raw text scrubbing without FHIR parsing |
| POST | `/process/from-server` | analyst | Fetch from FHIR server + de-identify + stream |
| POST | `/process/everything` | analyst | Patient `$everything` de-identify |
| POST | `/process/and-upload` | analyst | Fetch + de-identify + upload to target |
| POST | `/process/round-trip` | analyst | Full round-trip: source → de-id → target |
| POST | `/v1/jobs/bulk-export` | analyst | Submit async bulk export job |
| POST | `/v1/jobs/cohort` | analyst | Submit async cohort export job |
| GET | `/v1/jobs` | analyst | List jobs (`?status=`, `?type=`, `?limit=`, `?offset=`) |
| GET | `/v1/jobs/{job_id}` | analyst | Poll job status |
| GET | `/v1/jobs/{job_id}/result` | analyst | Download completed job NDJSON |
| DELETE | `/v1/jobs/{job_id}` | admin | Cancel / delete job |
| POST | `/analyse/risk` | analyst | k-anonymity + l-diversity assessment |
| POST | `/generate/synthetic` | analyst | Generate synthetic FHIR patients |
| POST | `/v1/score` | analyst | Score a single de-identified resource (ad-hoc) |
| POST | `/v1/jobs/{job_id}/score` | analyst | Trigger on-demand scoring for a completed job |
| GET | `/v1/jobs/{job_id}/score` | analyst | Retrieve cached score for a job |
| GET | `/v1/jobs/{job_id}/score/report` | analyst | Download Markdown audit report for a scored job |
| GET | `/v1/configs` | viewer | List all config profiles (system + user-defined) |
| GET | `/v1/configs/{name}` | viewer | Fetch YAML for a named config profile |
| POST | `/v1/configs` | admin | Create a user-defined config profile |
| PUT | `/v1/configs/{name}` | admin | Replace rules of a user-defined config |
| DELETE | `/v1/configs/{name}` | admin | Delete a user-defined config (system configs are read-only) |
| POST | `/process/dicom` | analyst | De-identify a single DICOM file |
| POST | `/process/dicom/batch` | analyst | De-identify multiple DICOM files (multipart/form-data) |
| POST | `/process/hl7v2` | analyst | De-identify a single HL7 v2 message |
| POST | `/process/hl7v2/batch` | analyst | De-identify a batch of HL7 v2 messages |
| POST | `/fhir/Subscription` | analyst | Create a FHIR R4 Subscription (rest-hook only) |
| GET | `/fhir/Subscription/{id}` | analyst | Retrieve a subscription |
| PUT | `/fhir/Subscription/{id}` | analyst | Update a subscription |
| DELETE | `/fhir/Subscription/{id}` | analyst | Delete a subscription |
| GET | `/fhir/Subscription` | admin | List all subscriptions |
| GET | `/.well-known/smart-configuration` | — | SMART on FHIR capability discovery (RFC 8414) |
| POST | `/oauth2/introspect` | — | Token introspection endpoint (RFC 7662) |
| GET | `/v1/audit/events` | admin | Query recent audit events (`?count=`, `?event_type=`) |

RBAC roles: `admin` (all), `analyst` (processing + jobs + analytics), `viewer` (read-only).
Auth: `X-API-Key` header required when `MEDANON_API_KEY` is set.

---

## Anonymizer — Python modules

### API layer (`src/api/`)

| Module | Role |
|---|---|
| `main.py` | FastAPI entry point. Startup wires Redis cache + job store + metrics + NLP adapter. Prometheus middleware. CORS. |
| `auth.py` | API-key auth + RBAC. `get_required_role()` resolves role for parameterized paths (e.g. `/v1/jobs/{job_id}`). `ENDPOINT_ROLE_PREFIXES` maps path prefixes to required roles. |
| `deps.py` | Request middleware: rate limiter (slowapi), body-size cap, SSRF guard (blocks RFC-1918 private ranges in `server_url` params), `_get_url_from_request_or_env` (resolves source/target URL with env fallback). |
| `routers/process.py` | `POST /process`, `/process/ndjson`, `/process/raw`, `/process/batch` |
| `routers/fhir_server.py` | `POST /process/from-server`, `/everything`, `/and-upload`, `/round-trip`, `/bulk-export`, `/cohort` |
| `routers/jobs.py` | `POST/GET/DELETE /v1/jobs/*` — delegates to `api/services/jobs.py` |
| `routers/analytics.py` | `POST /analyse/risk` — proxies to analytics service when `ANALYTICS_SERVICE_URL` set |
| `routers/synthetic.py` | `POST /generate/synthetic` — proxies to analytics service when `ANALYTICS_SERVICE_URL` set |
| `routers/scoring.py` | `POST /v1/score`, `POST /v1/jobs/{id}/score`, `GET /v1/jobs/{id}/score`, `GET /v1/jobs/{id}/score/report` |
| `routers/configs.py` | `GET/POST /v1/configs`, `GET/PUT/DELETE /v1/configs/{name}` — config profile CRUD (system configs read-only) |
| `routers/dicom.py` | `POST /process/dicom`, `/process/dicom/batch` — DICOM de-identification |
| `routers/hl7v2.py` | `POST /process/hl7v2`, `/process/hl7v2/batch` — HL7 v2 message de-identification |
| `routers/fhir_subscriptions.py` | FHIR R4 Subscription CRUD: `POST/GET/PUT/DELETE /fhir/Subscription` (rest-hook only) |
| `routers/smart.py` | SMART on FHIR: `GET /.well-known/smart-configuration`, `POST /oauth2/introspect` |
| `routers/audit.py` | `GET /v1/audit/events` — query recent audit events from centralized store |
| `schemas/` | Pydantic models: `fhir_ops.py`, `fhir_bulk.py`, `jobs.py`, `processing.py`, `scoring.py`, `responses.py` |
| `services/` | Business logic: `jobs.py`, `processing.py`, `analytics.py`, `fhir_server.py`, `synthetic.py`, `health.py`, `scoring.py`, `dicom.py`, `hl7v2.py`, `subscriptions.py` |

**Important:** `api/services/jobs.py` reads the job store via `import pipeline.jobs.store as _store_mod; _store_mod._job_store` — directly from the authoritative module. This avoids the Python import trap where `from module import variable` captures the value at import time (which is `None` before `init_job_store()` runs).

### Pipeline layer (`src/pipeline/`)

| Module | Role |
|---|---|
| `processor.py` | Orchestrator (~150 lines). Composes rule_matcher + action_dispatcher + gpas_orchestrator + post_processor + manifest. |
| `rule_matcher.py` | `_build_rule_index` — FHIRPath evaluation per resource type. Results cached per `(resource_type, rules_hash)`. |
| `action_dispatcher.py` | Pass 1: per-rule action dispatch. Stateless actions applied immediately. gPAS-bound values accumulated into `BatchWork`. NLP actions (`nlp_scrub`, `nlp_detect_act`) deferred into `NlpWork` items. Returns `tuple[list[BatchWork], list[NlpWork]]`. |
| `nlp_orchestrator.py` | Pass 1.5: batch NLP detection and replacement. Phase A: extract text fields from all deferred `NlpWork` items across resources. Phase B: deduplicate and batch-detect (one HTTP call for remote NLP, cache pre-warm for local Presidio). Phase C: per-resource replacement with isolated `token_state`. |
| `gpas_orchestrator.py` | Pass 2: sends one gPAS HTTP request per batch. Cross-chunk dedup via `seen_values`. Writes results back. |
| `post_processor.py` | `_rewrite_references` — updates cross-resource `reference` strings after ID changes. `_rewrite_text_ids` — replaces changed IDs in text fields. |
| `manifest.py` | `_attach_manifest` — writes per-rule transformation summary to `meta.tag`. Stripped before FHIR upload (display field would exceed HAPI's `tag_display varchar(200)` column). |
| `config.py` | Parses YAML profile. `${VAR:-default}` env interpolation. Raises `ValueError` on invalid config (never `sys.exit`). |
| `config_service.py` | `get_settings(profile)` with `@lru_cache(maxsize=8)` — single entry point. |
| `deidentify.py` | Unified action registry. `nlp_detect_by_path` routes through `_get_nlp_adapter()` lazy singleton — avoids importing Presidio unless NLP is used. `nlp_detect_act` provides entity-specific conditional NLP with per-entity-type replacement strategies. |
| `jobs/store.py` | `SqliteJobStore` (WAL mode, thread-safe). `list_jobs(status, job_type, limit, offset)`. |
| `jobs/__init__.py` | Re-exports `SqliteJobStore`, `JobStore`, `Job`, `JobStatus`, `init_job_store`. Does NOT re-export `_job_store` (would capture `None` at import time). |
| `worker.py` | Async job executor. `RedisJobStore`: BLPOP-based. `PostgresJobStore`: LISTEN/NOTIFY. `SqliteJobStore`: polls every 2 s. `max_concurrent` semaphore limits parallel jobs. |
| `nlp_orchestrator.py` | Pass 1.5: batch NLP detection and replacement. Collects texts from all deferred `NlpWork` items, runs entity detection in a single batch, then applies replacements per-resource with token_state isolation. |
| `scoring/` | Scoring sub-package. `engine.py` — composite scorer. `privacy.py` — privacy risk (hard constraint). `utility.py` — utility preservation. `quality.py` — output quality. `models.py` — score dataclasses. `constants.py` — weights, thresholds, action classifications. `audit.py` — Markdown audit report collector. |
| `config/service.py` | Centralised config profile loader with `@lru_cache(maxsize=8)`. Resolves profile aliases, env-aware auto-selection, optional TTL-based cache invalidation. Replaced top-level `config_service.py`. |
| `config/store.py` | SQLite-backed config profile metadata index. Tracks name, description, created_at, is_system for all profiles without reading YAML on each request. |
| `jobs/executors.py` | FHIR bulk-operation executors: `bulk-export`, `cohort`, `patient-export`, `batch-patient-export`, `bulk-import`, `reprocess`. Run inside `asyncio.to_thread` in the worker loop. Cross-chunk gPAS dedup. |

### Actions (`src/actions/`)

One file per action. All are pure functions: input field value → output value.

| File | Action | Notes |
|---|---|---|
| `redact.py` | `redact` | Replaces with `replacement` param (default `""`) |
| `cryptohash.py` | `cryptohash` | HMAC-SHA3-256 when `MEDANON_HASH_KEY` set; plain SHA3-256 otherwise (warns in prod) |
| `encrypt.py` | `encrypt` | RSA public-key encryption |
| `decrypt.py` | `decrypt` | RSA private-key decryption |
| `perturb.py` | `perturb` | Random noise; `secrets.randbelow()` (CSPRNG, not `random`) |
| `substitute.py` | `substitute` | Replace with fixed `substitute_with` value |
| `generalize.py` | `generalize` | `date_year`, `date_year_month`, `zip_prefix`, `age_bracket`, `number_round`, `category` |
| `scrub_text.py` | `scrub_text` | Regex-based free-text PHI removal |
| `deidentify.py` | `nlp_detect_act` | Entity-specific conditional NLP: detect PII first, apply per-entity-type actions. Falls back to `redact` when NLP adapter unavailable. Registered in action registry alongside `nlp_scrub`/`nlp_detect`. |

### Integrations (`src/integrations/`)

#### gPAS (`integrations/gpas/`)

| Module | Role |
|---|---|
| `client.py` | Public API: `gpas_pseudonymize_batch`, `gpas_depseudonymize_batch`. Checks cache before calling transport. |
| `transport.py` | HTTP retry loop, URL resolution, cache helpers (`_cache_get`, `_cache_set`), domain listing. |
| `circuit_breaker.py` | `_CircuitBreaker` state machine: CLOSED → OPEN → HALF-OPEN. Singleton per process. Threshold/timeout configurable via env. |
| `protocol.py` | FHIR Parameters request builders + response parsers for `$pseudonymize` / `$depseudonymize`. |
| `adapter.py` | `GpasPseudonymizerAdapter` — implements `PseudonymizerPort`; wraps `gpas_pseudonymize_batch`. |

#### FHIR client (`integrations/fhir/`)

| Module | Role |
|---|---|
| `client.py` | Barrel file — re-exports from all sub-modules for backward compatibility. |
| `_transport.py` | HTTP helpers: `_write_json`, `_read_json`, ID validation/sanitization, resource type regex. |
| `reader.py` | Paginated fetch (`fetch_resources`), `$everything`, bulk export status polling. |
| `writer.py` | `upload_resources` (batch Bundle upload), `_infer_upload_tiers` (Bellman-Ford topological sort), `_compute_id_map`, `_rewrite_references`, `_strip_manifest_tags`. |
| `bulk.py` | Async bulk export helpers: FHIR `$export` operation, NDJSON file download. |

#### NLP (`integrations/nlp/`)

| Module | Role |
|---|---|
| `detector.py` | Presidio NER. Deterministic `[[TYPE_N]]` token substitution. XHTML-safe output. |
| `adapter.py` | `LocalPresidioAdapter` / `RemoteNlpAdapter`. Selected at first NLP call based on `NLP_SERVICE_URL`. Both support `detect_batch()` for Pass 1.5 batch processing. |
| `remote_detector.py` | HTTP client for NLP microservice. `detect_batch_remote()` for batch. Fail-closed: returns `[NLP_UNAVAILABLE]` placeholder on any failure (no unscrubbed text leaks). |

#### PostgreSQL (`integrations/postgres/`)

| Module | Role |
|---|---|
| `pool.py` | Shared `psycopg2.ThreadedConnectionPool` singleton. One pool shared across all stores (jobs, subscriptions, config, staging) to avoid multiple independent pools. |
| `job_store.py` | `PostgresJobStore` — drop-in replacement for `SqliteJobStore`. `FOR UPDATE SKIP LOCKED` for contention-free multi-worker claims. `NOTIFY`/`LISTEN` for instant worker wake-up. |
| `config_store.py` | PostgreSQL-backed config profile metadata index. Drop-in replacement for the SQLite `ConfigStore`. |
| `subscription_store.py` | `PostgresSubscriptionStore` — FHIR R4 Subscription persistence. Drop-in replacement for `SqliteSubscriptionStore`. |

#### Staging (`integrations/staging/`)

| Module | Role |
|---|---|
| `store.py` | Two-phase staging store. Intermediate PostgreSQL table decouples Phase 1 (FHIR fetch) from Phase 2 (de-identify + write NDJSON). `ON CONFLICT DO NOTHING` for automatic dedup. `SELECT ... FOR UPDATE SKIP LOCKED` for future parallel workers. |

### Utils (`src/utils/`)

| Module | Role |
|---|---|
| `cache.py` | `CacheBackend` Protocol. `LocalLruCache` (default, 50K entries, 10% eviction on overflow). `RedisCache` (L2, 1 h TTL, errors swallowed gracefully). |
| `fhirpath.py` | FHIRPath traversal helpers: `find_nodes`, `error`, `not_implemented`. |
| `crypto.py` | RSA encrypt/decrypt. `bounded_random` uses `secrets.randbelow()` (CSPRNG). Path-traversal guard on key file paths. |
| `metrics.py` | Prometheus counters + histograms: `medanon_requests_total`, `medanon_gpas_*`, `medanon_fhir_*`. |
| `circuit_breaker.py` | Reusable three-state circuit breaker: CLOSED (normal) -> OPEN (fail-fast) -> HALF_OPEN (probe). Thread-safe. Configurable failure threshold, recovery timeout, and sliding window. |
| `audit.py` | Centralized audit logging. Events written as structured JSON to `medanon.audit` logger, optionally appended to a Redis Stream (`medanon:audit`), and optionally to a rotating local file. `query()` reads back recent events. |

---

## gPAS component detail

gPAS (Generic Pseudonym Administration Service) from University Medicine Greifswald provides audited, reversible pseudonymization via the TTP-FHIR protocol.

**Why gPAS instead of local hashing?**
- Pseudonyms are reversible by authorized TTP users — required for adverse event investigation and follow-up studies
- Pseudonym mappings are persistent across exports — the same patient always gets the same pseudonym
- The TTP model provides organizational separation: the clinical team and the research team never both have the key

**Domain lifecycle:**
1. Domain created via gPAS web UI or API → stored in PostgreSQL + loaded into JVM `domainLocks HashMap`
2. `$pseudonymizeAllowCreate` called with original IDs → gPAS creates pseudonyms if not exist
3. Pseudonyms returned and cached (LRU + Redis)
4. To decode: `$depseudonymize` (requires TTP admin credentials)

**Why circuit breaker?** gPAS is a critical dependency. Without a circuit breaker, a gPAS outage causes every processing request to wait for the full HTTP timeout (30 s by default) before failing, rapidly exhausting the anonymizer's thread pool. The circuit breaker detects the outage after 5 failures and fails subsequent requests immediately, keeping the anonymizer responsive for non-gPAS requests.

---

## Redis component detail

Redis serves two independent roles, both activated by setting `MEDANON_REDIS_URL`:

**1. Job queue:** Jobs submitted to `/v1/jobs/*` are pushed to a Redis list. Workers use `BLPOP` — they sleep until a job arrives, then wake immediately. This is more efficient than the 2-second poll loop used with SQLite. Multiple anonymizer replicas share the same queue — a job submitted to replica A can be executed by replica B.

**2. gPAS pseudonym cache (L2):** Cross-replica sharing of gPAS results. Keys: `medanon:gpas:["pseudonymize", url, domain, op, original_id]`. TTL: 1 hour. Redis errors are swallowed — the cache falls back to a direct gPAS call gracefully.

**Why separate from L1?** L1 is an in-process LRU dict (per replica). If replica A has already pseudonymized patient `123`, replica B would still call gPAS for it without L2. Redis L2 shares results across replicas, reducing gPAS load during parallel bulk exports where multiple replicas process different resource type pages simultaneously.

---

## Nginx load balancer components

Three nginx instances handle routing, load balancing, and horizontal scaling:

### UI nginx (`client/nginx.conf`)

Serves the React SPA and proxies API/FHIR requests:

| Location | Target | Config |
|----------|--------|--------|
| `/` | SPA static files | `try_files $uri $uri/ /index.html` |
| `/api/*` | `medanon:8000` | `least_conn`, keepalive 16, 120s timeout |
| `/fhir/*` | `hapi-fhir:8080` | Dynamic DNS, 300s timeout |
| `/fhir-target/*` | `hapi-fhir-target:8080` | Dynamic DNS, 300s timeout |
| `/healthz` | nginx | Returns `200 OK` for Docker healthcheck |

**Key features:**
- `resolver 127.0.0.11` — uses Docker's embedded DNS for dynamic service discovery
- Keepalive connections to anonymizer upstream (avoids per-request TCP overhead)
- Security headers: X-Frame-Options, X-Content-Type-Options, Referrer-Policy
- Static asset caching with `Cache-Control: public, immutable` for `/assets/`

### gPAS LB (`services/gpas/lb/nginx.conf`)

Round-robin load balancer for gPAS WildFly replicas:

| Location | Target | Config |
|----------|--------|--------|
| `/ping` | nginx | Returns `200 "pong"` (no upstream) |
| `/*` | `gpas:8080` replicas | Round-robin, 180s read timeout |

**Key features:**
- `/ping` health endpoint lets Docker healthcheck succeed while WildFly initializes (~90s cold start)
- 180s read timeout accommodates slow gPAS bulk operations
- Docker DNS re-resolution on each connect for `--scale gpas=N` support
- `client_max_body_size 32m` for large FHIR bundles

### NLP LB (`services/nlp/nginx.conf`)

Least-connections load balancer for NLP Presidio replicas:

| Location | Target | Config |
|----------|--------|--------|
| `/health` | nginx | Returns `200 OK` (no upstream) |
| `/*` | `nlp:8200` replicas | `least_conn`, 120s read timeout |

**Key features:**
- `least_conn` balancing — optimal for CPU-intensive, variable-duration NLP inference
- 120s read timeout matches anonymizer's NLP batch deadline
- `client_max_body_size 10m` for batch detection payloads

**Why least_conn for NLP but round-robin for gPAS?**

NLP inference is CPU-bound and request durations vary significantly (50ms for a short text, 5s for a batch of 1000 narratives). `least_conn` routes new requests to the replica with the fewest active connections, ensuring even load distribution.

gPAS is I/O-bound (PostgreSQL lookups) with relatively uniform request durations, so round-robin is sufficient and avoids the overhead of connection counting.

---

## `packages/medanon-core/`

Zero-dependency shared library installed into the anonymizer (and optionally other services). Contains:
- `medanon_core.domain.Job` / `JobStatus` — async job dataclass and lifecycle enum (`pending → running → done / failed / cancelled`)
- Exception types: `JobStoreUnavailable`, `JobNotFound`, `JobNotComplete`, `JobResultMissing`
- `medanon_core.analytics.risk` — k-anonymity, l-diversity, and derived re-identification risk scores (prosecutor, journalist, marketer). Operates on de-identified Patient + Condition NDJSON output. Returns `risk_level` summary: `low` | `medium` | `high` | `critical`.

Zero dependencies means it can be installed into any service without pulling in FastAPI, Presidio, or other heavy packages.
