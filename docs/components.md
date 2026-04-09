# MedAnon — Component Reference

Technical reference for all services and modules. For architecture diagrams and design decisions see [architecture.md](architecture.md). For data flow traces see [data-flow.md](data-flow.md).

---

## Services

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | 8000 | FastAPI de-identification engine |
| `ui` | `medanon-ui:latest` | 8501 | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | 8081 | Source HAPI FHIR R4 (identified data) |
| `fhir-server-target` | `hapiproject/hapi:v7.6.0` | 8082 | Target HAPI FHIR R4 (de-identified data) |
| `gpas` | WildFly + gPAS | 8080 | Reversible pseudonymization (TTP) |
| `gpas-db` | `mysql:8.0` | internal | gPAS pseudonym store |
| `redis` | `redis:7` | internal | Job queue (BLPOP) + gPAS L2 cache |

Opt-in (not started by default):

| Container | Profile flag | Host Port | Role |
|---|---|---|---|
| `analytics` | `--profile analytics` | 8100 | Risk analysis + synthetic data |
| `nlp` | `--profile nlp` | 8200 | Presidio NLP (~800 MB, saves anonymizer RAM) |
| `gpas-db-replica` | `--profile ha` | internal | MySQL read replica for gPAS HA |

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
| `schemas/` | Pydantic models: `fhir_ops.py`, `jobs.py`, `processing.py`, `responses.py` |
| `services/` | Business logic: `jobs.py`, `processing.py`, `analytics.py`, `fhir_server.py`, `synthetic.py`, `health.py` |

**Important:** `api/services/jobs.py` reads the job store via `import pipeline.jobs.store as _store_mod; _store_mod._job_store` — directly from the authoritative module. This avoids the Python import trap where `from module import variable` captures the value at import time (which is `None` before `init_job_store()` runs).

### Pipeline layer (`src/pipeline/`)

| Module | Role |
|---|---|
| `processor.py` | Orchestrator (~150 lines). Composes rule_matcher + action_dispatcher + gpas_orchestrator + post_processor + manifest. |
| `rule_matcher.py` | `_build_rule_index` — FHIRPath evaluation per resource type. Results cached per `(resource_type, rules_hash)`. |
| `action_dispatcher.py` | Pass 1: per-rule action dispatch. Stateless actions applied immediately. gPAS-bound values accumulated into `BatchWork` for batch processing. |
| `gpas_orchestrator.py` | Pass 2: sends one gPAS HTTP request per batch. Writes results back. |
| `post_processor.py` | `_rewrite_references` — updates cross-resource `reference` strings after ID changes. `_rewrite_text_ids` — replaces changed IDs in text fields. |
| `manifest.py` | `_attach_manifest` — writes per-rule transformation summary to `meta.tag`. Stripped before FHIR upload (display field would exceed HAPI's `tag_display varchar(200)` column). |
| `config.py` | Parses YAML profile. `${VAR:-default}` env interpolation. Raises `ValueError` on invalid config (never `sys.exit`). |
| `config_service.py` | `get_settings(profile)` with `@lru_cache(maxsize=8)` — single entry point. |
| `deidentify.py` | Unified action registry. `nlp_detect_by_path` routes through `_get_nlp_adapter()` lazy singleton — avoids importing Presidio unless NLP is used. |
| `jobs/store.py` | `SqliteJobStore` (WAL mode, thread-safe). `list_jobs(status, job_type, limit, offset)`. |
| `jobs/__init__.py` | Re-exports `SqliteJobStore`, `JobStore`, `Job`, `JobStatus`, `init_job_store`. Does NOT re-export `_job_store` (would capture `None` at import time). |
| `worker.py` | Async job executor. `RedisJobStore`: BLPOP-based (wakes immediately on new job). `SqliteJobStore`: polls every 2 s. `max_concurrent` semaphore limits parallel jobs. |

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
| `adapter.py` | `LocalPresidioAdapter` / `RemoteNlpAdapter`. Selected at first NLP call based on `NLP_SERVICE_URL`. |
| `remote_detector.py` | HTTP client for NLP microservice. Falls back to original text on any failure. |

### Utils (`src/utils/`)

| Module | Role |
|---|---|
| `cache.py` | `CacheBackend` Protocol. `LocalLruCache` (default, 50K entries, 10% eviction on overflow). `RedisCache` (L2, 1 h TTL, errors swallowed gracefully). |
| `fhirpath.py` | FHIRPath traversal helpers: `find_nodes`, `error`, `not_implemented`. |
| `crypto.py` | RSA encrypt/decrypt. `bounded_random` uses `secrets.randbelow()` (CSPRNG). Path-traversal guard on key file paths. |
| `metrics.py` | Prometheus counters + histograms: `medanon_requests_total`, `medanon_gpas_*`, `medanon_fhir_*`. |

---

## gPAS component detail

gPAS (Generic Pseudonym Administration Service) from University Medicine Greifswald provides audited, reversible pseudonymization via the TTP-FHIR protocol.

**Why gPAS instead of local hashing?**
- Pseudonyms are reversible by authorized TTP users — required for adverse event investigation and follow-up studies
- Pseudonym mappings are persistent across exports — the same patient always gets the same pseudonym
- The TTP model provides organizational separation: the clinical team and the research team never both have the key

**Domain lifecycle:**
1. Domain created via gPAS web UI or API → stored in MySQL + loaded into JVM `domainLocks HashMap`
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

## `packages/medanon-core/`

Zero-dependency shared library installed into the anonymizer (and optionally other services). Contains:
- `medanon_core.domain.Job` / `JobStatus` — async job dataclass and lifecycle enum (`pending → running → done / failed / cancelled`)
- Exception types: `JobStoreUnavailable`, `JobNotFound`, `JobNotComplete`, `JobResultMissing`

Zero dependencies means it can be installed into any service without pulling in FastAPI, Presidio, or other heavy packages.
