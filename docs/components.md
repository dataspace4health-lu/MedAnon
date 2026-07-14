# Component Catalog

Technical reference for all services and modules. For architecture diagrams and design decisions see [architecture.md](architecture.md). For data flow traces see [data-flow.md](data-flow.md). For onboarding start at [introduction.md](introduction.md).

---

## 2.1 Infrastructure & Shared Configuration

### Docker services

**Always-on (14 services):**

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | `8000` | FastAPI de-identification engine |
| `worker` | `medanon:latest` | `9091` (metrics) | Dedicated async job worker |
| `ui` | `medanon-ui:latest` | `8501` | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | none (isolated) | Source HAPI FHIR R4 — identified data |
| `hapi-db` | `postgres:16-alpine` | internal | PostgreSQL backing source HAPI |
| `fhir-target` | `hapiproject/hapi:v7.6.0` | `8082` | Target HAPI FHIR R4 — de-identified data |
| `hapi-target-db` | `postgres:16-alpine` | internal | PostgreSQL backing target HAPI |
| `gateway` | `traefik:v3` | `8080` (gPAS), `8200` (NLP) | API gateway — Docker-provider service discovery; carries `gpas-lb` / `nlp-lb` network aliases |
| `gpas` | WildFly 38 + gPAS | via gateway | Reversible pseudonymization (TTP); scaled with `--scale gpas=N` |
| `gpas-db` | `postgres:16-alpine` | internal | gPAS pseudonym store |
| `app-db` | `postgres:16-alpine` | internal | Jobs, configs, subscriptions, staging |
| `redis` | `redis:7-alpine` | internal | Job queue (Redis Streams) + gPAS L2 cache |
| `analytics` | `medanon-analytics:latest` | `8100` | Risk analysis + synthetic data |
| `nlp` | `medanon-nlp:latest` | via gateway | Presidio NLP microservice (~800 MB image); scaled with `--scale nlp=N` |

**Opt-in profiles (started with `--profile <name>`):**

| Container | Profile | Host Port | Role |
|---|---|---|---|
| `gpas-db-replica` | `ha` | internal | PostgreSQL streaming replica for gPAS HA |
| `minio` | `s3` | `9000`, `9001` | S3-compatible object storage for job results |
| `ollama` | `ai` | internal | Local LLM inference for AI agents |

**Scaling (Traefik discovers replicas via Docker labels — no config reload):**
```bash
docker compose up -d --scale gpas=3   # 3 gPAS replicas (round-robin via gateway)
docker compose up -d --scale nlp=4    # 4 NLP replicas (round-robin via gateway)
```

### Networks

Two Docker bridge networks enforce physical isolation between identified and de-identified data:

| Network | Members | Purpose |
|---|---|---|
| `processing-net` | All services (anonymizer, worker, ui, target FHIR, gPAS, NLP, analytics, Redis, PostgreSQL) | Main application network |
| `source-net` | `fhir-server`, `hapi-db`, `anonymizer`, `worker` | Isolated network for identified data. Only anonymizer and worker bridge both networks. |

The source FHIR server has no published host port — it is accessible only through anonymizer proxy endpoints. This prevents accidental direct access to identified patient data from the UI, analytics, or any other service.

### Edge & routing

Two edge components handle ingress, routing, and horizontal scaling:

#### UI nginx (`client/nginx.conf`) — port 8501

Serves the React SPA and proxies API / FHIR requests:

| Location | Target | Strategy |
|---|---|---|
| `/` | SPA static files | `try_files` → `index.html` fallback |
| `/api/*` | `medanon:8000` | `least_conn`, keepalive 16, 120s timeout |
| `/fhir/*` | `hapi-fhir:8080` | Dynamic DNS (`resolver 127.0.0.11`), 300s timeout |
| `/fhir-target/*` | `hapi-fhir-target:8080` | Dynamic DNS, 300s timeout |
| `/healthz` | nginx | Returns `200 OK` for Docker healthcheck |

Security headers added: `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`. Static assets served with `Cache-Control: public, immutable`.

**Why dynamic DNS?** Using a variable `$upstream` with `resolver 127.0.0.11` forces nginx to re-resolve the container hostname on each request. Without this, nginx caches the IP at startup and returns 502 after container restarts (even though the DNS record updates immediately).

#### Traefik gateway (`gateway` container) — ports 8080 (gPAS), 8200 (NLP)

The `gateway` service joins `processing-net` under the `gpas-lb` and `nlp-lb` aliases so existing `GPAS_URL=http://gpas-lb:80/...` and `NLP_SERVICE_URL=http://nlp-lb:8200` values work without `.env` changes. `services/gpas/lb/nginx.conf` and `services/nlp/nginx.conf` are kept as reference only.

| Entry point | Routes to | Strategy |
|---|---|---|
| `gpas` (`:80`, host `8080`) — `/gpas-web`, `/gras-web` | `gpas:8080` | Sticky sessions (`gpas_session` cookie) — keeps JSF ViewState bound to one replica |
| `gpas` (`:80`, host `8080`) — `/` | `gpas:8080` | Round-robin |
| `nlp` (`:8200`, host `8200`) — `/` | `nlp:8200` | Round-robin |
| `metrics` (`:8082`, internal) | Traefik Prometheus exporter | Not host-published |

**Discovery:** Docker provider with read-only socket mount, `exposedbydefault=false`. Traefik labels live on `gpas` and `nlp` services; new replicas appear automatically when scaled.

**Why sticky sessions only for gPAS web UI?** The gPAS WildFly application uses JSF, which keeps the ViewState in server-side session memory. Round-robin across replicas would break the UI mid-form. The REST API (`$pseudonymizeAllowCreate`) is stateless and uses round-robin.

### PostgreSQL databases

Four independent PostgreSQL 16 instances — each has its own lifecycle, backup schedule, and resource limits:

| Instance | Container | Schema owner | What it stores |
|---|---|---|---|
| **App-db** | `app-db` | `medanon` | Jobs, config profiles, subscriptions, two-phase staging, processing runs |
| **gPAS-db** | `gpas-db` | gPAS / WildFly | Pseudonym mappings (managed exclusively by gPAS, not directly accessible) |
| **Source HAPI** | `hapi-db` | HAPI | Identified clinical FHIR resources |
| **Target HAPI** | `hapi-target-db` | HAPI | De-identified FHIR resources |

The app-db schema (`medanon`) is initialized by `services/anonymizer/sql/init.sql`:

```
medanon.jobs                — async job records
medanon.staged_resources    — two-phase staging for bulk operations
medanon.configs             — user-defined config profiles
medanon.subscriptions       — FHIR R4 Subscriptions
medanon.processing_runs     — scoring history
```

### Redis

Redis 7 serves three independent roles, isolated on separate logical databases:

| DB | Role | Activated by |
|----|------|-------------|
| `0` | Anonymizer job queue **and** gPAS L2 cache | `MEDANON_REDIS_URL` (anonymizer + worker) |
| `1` | Reserved for future cross-service queues | — |
| `2` | NLP L2 detection cache | `NLP_REDIS_URL` (defaulted in compose) |

**Job queue (DB 0):** Jobs submitted to `/v1/jobs/*` are pushed to a Redis Stream (`medanon:job_stream`). Workers consume via `XREADGROUP` against the `workers` consumer group — blocking read with at-least-once delivery semantics. If a worker crashes after popping a job but before ACKing, the message stays in the Pending Entry List (PEL) and is reclaimed by `XAUTOCLAIM`. Multiple anonymizer replicas share the same queue; a job submitted to replica A can be executed by replica B.

**gPAS pseudonym cache (L2, DB 0):** Cross-replica sharing of gPAS results. Key format: `medanon:gpas:["pseudonymize", url, domain, op, original_id]`. TTL: 1 hour. Redis errors are swallowed — the cache falls back to a direct gPAS call gracefully, so a Redis outage degrades performance but does not break de-identification.

**NLP detection cache (L2, DB 2):** Cross-replica sharing of Presidio detection results. Key format: `medanon:nlp:detect:<lang>:<threshold>:<entities-hash>:<text-sha256>`. Default TTL: 7 days (`NLP_REDIS_TTL_SEC=604800`). Survives NLP container restarts — eliminates the cold-cache penalty after deploys. Implemented in `services/nlp/src/cache.py`. Redis errors are swallowed (soft-fail to L1 + compute path).

**Why three layers?** L1 (in-process LRU per replica) is fastest but not shared. L2 (Redis) shares results across replicas and survives restarts. For a fleet of N anonymizer + M NLP replicas processing parallel bulk exports, L2 collapses N×M cold-start costs into a single warm-up.

### Shared configuration

All services read configuration from environment variables, set in `.env` (Docker Compose) or Helm values (Kubernetes). Never store secrets in config files.

**Core env vars:**

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_HASH_KEY` | (required in prod) | HMAC-SHA3-256 key for `cryptohash` action. Without this, plain SHA3-256 is used — reversible via rainbow tables. |
| `MEDANON_API_KEY` | (blank = open) | API authentication key. Leave blank for local dev only. |
| `MEDANON_REDIS_URL` | — | Enables Redis job store + gPAS L2 cache. Format: `redis://:password@redis:6379/0`. |
| `NLP_REDIS_URL` | `redis://:…@redis:6379/2` (in compose) | Enables the NLP L2 detection cache on Redis DB 2. Leave empty to disable. |
| `NLP_REDIS_TTL_SEC` | `604800` (7 days) | TTL for NLP L2 cache entries. |
| `MEDANON_APP_DB_URL` | — | PostgreSQL URL for app state (auto-constructed in docker-compose). |
| `GPAS_URL` | — | gPAS server URL. When set, auto-selects `config_gpas.yaml` profile. |
| `NLP_SERVICE_URL` | `http://nlp-lb:8200` | NLP microservice URL (hardcoded in docker-compose, override for external NLP). |
| `ANALYTICS_SERVICE_URL` | `http://analytics:8100` | Analytics microservice URL. |
| `LOG_LEVEL` | `INFO` | `DEBUG` may log resource content containing PHI — use `INFO` in production. |
| `MEDANON_MANIFEST_ENABLED` | `false` | Attach transformation manifest to `meta.tag`. Required for GDPR Art. 30 accountability. |

Full variable reference: [DEPLOYMENT.md § Environment variables](DEPLOYMENT.md) and `.env.example`.

---

## 2.2 Pseudonymization Service (gPAS + PostgreSQL)

gPAS (Generic Pseudonym Administration Service) is a trusted third-party (TTP) service developed by the University Medicine Greifswald. It provides **audited, reversible pseudonymization** via the TTP-FHIR protocol over PostgreSQL storage.

### Why gPAS instead of local hashing

| Approach | Problem |
|---|---|
| Local SHA3-256 hash | Irreversible — adverse event investigation and follow-up linkage become impossible |
| HMAC hash (keyed) | Reversible only by whoever holds the key — no TTP separation |
| gPAS TTP | Reversible by an authorized third party, completely separate from the clinical and research teams |

The TTP model provides organizational separation: the clinical team does not have the pseudonym key, and the research team does not have the patient identifiers. Only the TTP (gPAS operator) can perform reverse lookups, and only under controlled procedures.

### How pseudonymization works

```
anonymizer (pseudonymize stage)
      │
      │ POST $pseudonymizeAllowCreate
      │ Parameters { [id1, id2, id3, ...] }
      ▼
gateway:8080 (Traefik round-robin, network alias gpas-lb)
      │
      ▼
gpas:8080 (WildFly 38)
      │  checks cache → queries gpas-db
      ▼
gpas-db:5432 (PostgreSQL 16)
      │
      ▼ Parameters { [psn1, psn2, psn3, ...] }
      │
anonymizer
      │ L1 cache (local LRU, 50K entries)
      │ L2 cache (Redis, 1h TTL)
      │ write pseudonyms back to resource fields
```

A single batch HTTP call handles all values from a batch of up to 1,000 resources. If all values are L1/L2 cache hits, the gPAS HTTP call is skipped entirely.

### Circuit breaker

gPAS is a critical external dependency. Without a circuit breaker, a gPAS outage causes each processing request to wait for the full 30-second timeout before failing, exhausting the anonymizer thread pool.

```
CLOSED (normal operation)
    │ 5 failures within 60s
    ▼
OPEN (fail-fast — no gPAS calls for 30s)
    │ 30s elapsed
    ▼
HALF-OPEN (1 probe call)
    │ success → CLOSED
    │ failure → OPEN (timer resets)
```

**Configuration:** `GPAS_CB_FAILURE_THRESHOLD` (5), `GPAS_CB_RECOVERY_TIMEOUT_SEC` (30), `GPAS_CB_WINDOW_SEC` (60).

**Effect:** A gPAS outage fails requests fast and keeps the anonymizer responsive for non-gPAS operations (profiles that use only hashing or redaction continue working).

### Domain lifecycle

gPAS organizes pseudonyms into domains. Each domain has its own pseudonym namespace and generator algorithm.

1. **Create domain** via gPAS web UI (`http://localhost:8080/gpas-web/`) or `make init-domains`
2. Domain stored in `gpas-db` + loaded into JVM `domainLocks HashMap`
3. `$pseudonymizeAllowCreate` — creates pseudonym if not exists, returns existing if already created
4. To reverse: `$depseudonymize` — requires TTP admin credentials

**Critical:** Never create domains by inserting rows directly into PostgreSQL. gPAS maintains a `domainLocks HashMap` in JVM memory populated only when domains are created through the gPAS API. Direct SQL inserts appear to work at the DB level but cause "domain not found" errors at runtime when pseudonymization is attempted.

### Python modules (`integrations/gpas/`)

| Module | Role |
|---|---|
| `client.py` | Public API: `gpas_pseudonymize_batch`, `gpas_depseudonymize_batch`. Checks L1 + L2 cache before calling transport. |
| `transport.py` | HTTP retry loop (2 retries, exponential backoff), URL resolution, cache helpers (`_cache_get`, `_cache_set`), domain listing. |
| `circuit_breaker.py` | Three-state circuit breaker (CLOSED → OPEN → HALF_OPEN). Singleton per process. |
| `protocol.py` | FHIR Parameters request builders + response parsers for `$pseudonymize` / `$depseudonymize`. |
| `adapter.py` | `GpasPseudonymizerAdapter` — implements `PseudonymizerPort`. Wraps `gpas_pseudonymize_batch`. |

### Depseudonymization

`gpas_depseudonymize` action reverses a pseudonym back to its original value. Requires TTP admin credentials (`GPAS_BASIC_USER`, `GPAS_BASIC_PASS` with elevated role). Used for adverse event investigation workflows.

```yaml
rules:
  - name: "reverse patient id"
    match: "Patient.id"
    action: "gpas_depseudonymize"
```

---

## 2.3 De-identification Engine

The anonymizer is the core service. It exposes a FastAPI REST API, runs the 4-stage de-identification pipeline, manages the async job system, scores outputs, and hosts the AI agents.

### REST API endpoints

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/health` | — | Liveness. Fast — no external calls. Used by Docker healthcheck. |
| GET | `/ready` | — | Readiness. Probes FHIR + gPAS with 5s timeout each. |
| GET | `/metrics` | — | Prometheus metrics |
| GET | `/docs` | — | Swagger UI |
| POST | `/process` | analyst | De-identify a single FHIR resource |
| POST | `/process/batch` | analyst | De-identify NDJSON / Bundle / XML |
| POST | `/process/ndjson` | analyst | NDJSON-only streaming endpoint |
| POST | `/process/raw` | analyst | Raw text scrubbing without FHIR parsing |
| POST | `/process/from-server` | analyst | Fetch from FHIR server + de-identify + stream |
| POST | `/process/everything` | analyst | Patient `$everything` de-identify |
| POST | `/process/and-upload` | analyst | Fetch + de-identify + upload to target |
| POST | `/process/round-trip` | analyst | Full round-trip: source → de-id → target |
| POST | `/v1/jobs/bulk-export` | analyst | Submit async bulk export (202 + `job_id`) |
| POST | `/v1/jobs/cohort` | analyst | Submit async cohort export |
| GET | `/v1/jobs` | analyst | List jobs (`?status=`, `?type=`, `?limit=`, `?offset=`) |
| GET | `/v1/jobs/{id}` | analyst | Poll job status |
| GET | `/v1/jobs/{id}/result` | analyst | Download completed job NDJSON |
| DELETE | `/v1/jobs/{id}` | admin | Cancel / delete job |
| POST | `/v1/score` | analyst | Score a single de-identified resource (ad-hoc) |
| POST | `/v1/jobs/{id}/score` | analyst | Trigger scoring for a completed job |
| GET | `/v1/jobs/{id}/score` | analyst | Retrieve cached composite score |
| GET | `/v1/jobs/{id}/score/report` | analyst | Download Markdown audit report |
| GET | `/v1/configs` | viewer | List all config profiles |
| POST | `/v1/configs` | admin | Create a user-defined config profile |
| PUT | `/v1/configs/{name}` | admin | Replace rules of a user-defined profile |
| DELETE | `/v1/configs/{name}` | admin | Delete a user-defined profile |
| POST | `/process/dicom` | analyst | De-identify a DICOM file |
| POST | `/process/dicom/batch` | analyst | De-identify multiple DICOM files |
| POST | `/process/hl7v2` | analyst | De-identify an HL7 v2 message |
| POST | `/process/hl7v2/batch` | analyst | De-identify a batch of HL7 v2 messages |
| GET | `/v1/processing-runs` | analyst | List processing runs with scoring stats |
| GET | `/v1/processing-runs/{id}` | analyst | Get a run with full score breakdown |
| DELETE | `/v1/processing-runs` | admin | Purge processing run history |
| GET | `/v1/ai/status` | analyst | AI provider health + circuit breaker state |
| POST | `/v1/ai/generate-config` | analyst | Generate config from natural-language description |
| POST | `/v1/ai/detect-pii` | analyst | Scan de-identified resources for PII leaks |
| POST | `/v1/ai/explain` | analyst | Explain config rules in plain language (SSE) |
| POST | `/v1/ai/compliance` | analyst | Regulatory gap analysis |
| GET | `/v1/audit/events` | admin | Query recent audit events |

RBAC: `admin` (all) → `analyst` (processing + jobs + analytics + AI) → `viewer` (read-only).  
Auth: `X-API-Key` header when `MEDANON_API_KEY` is set.  
Override config per-request: `?config_profile=<name>`.

### API layer (`src/api/`)

| Module | Role |
|---|---|
| `main.py` | FastAPI entry point. Startup: Redis cache + job store + NLP adapter + Prometheus middleware + CORS. |
| `auth.py` | API key auth + RBAC. `get_required_role()` resolves role for parameterized paths. `ENDPOINT_ROLE_PREFIXES` maps prefixes to required roles. |
| `deps.py` | Request middleware: rate limiter (slowapi), body-size cap (`MEDANON_MAX_BODY_BYTES`), SSRF guard (blocks RFC-1918 ranges in `server_url` params), `_get_url_from_request_or_env`. |
| `routers/process.py` | `POST /process`, `/process/ndjson`, `/process/raw`, `/process/batch` |
| `routers/fhir_server.py` | `POST /process/from-server`, `/everything`, `/and-upload`, `/round-trip` |
| `routers/fhir_bulk.py` | `POST /process/bulk-export`, `/cohort` |
| `routers/jobs.py` | `POST/GET/DELETE /v1/jobs/*` |
| `routers/scoring.py` | `/v1/score`, `/v1/jobs/{id}/score`, `/v1/jobs/{id}/score/report` |
| `routers/configs.py` | `/v1/configs` CRUD |
| `routers/analytics.py` | `POST /analyse/risk` — proxies to analytics service |
| `routers/synthetic.py` | `POST /generate/synthetic` — proxies to analytics service |
| `routers/dicom.py` | `/process/dicom`, `/process/dicom/batch` |
| `routers/hl7v2.py` | `/process/hl7v2`, `/process/hl7v2/batch` |
| `routers/fhir_subscriptions.py` | FHIR R4 Subscription CRUD (`POST/GET/PUT/DELETE /fhir/Subscription`) |
| `routers/smart.py` | `GET /.well-known/smart-configuration`, `POST /oauth2/introspect` |
| `routers/audit.py` | `GET /v1/audit/events` |
| `routers/agents.py` | `GET/POST /v1/ai/*` — AI agent endpoints with SSE streaming |
| `routers/processing_runs.py` | `GET/DELETE /v1/processing-runs/*` |
| `routers/permits.py` | `GET/POST /v1/permits`, lifecycle transitions (submit/approve/reject/revoke) - admin-only, 409 on illegal transition |
| `routers/minimise.py` | `POST /v1/minimise/assess` - data-minimisation report (evaluate-only) |
| `routers/disclosure.py` | `POST /v1/export/decision` - Five-Safes disclosure decision |
| `routers/exposure.py` | `POST /v1/exposure/assess` - cumulative-exposure / differencing risk over the release ledger |
| `routers/statistical.py` | `POST /v1/export/statistical` - aggregate release (small-cell suppression and/or DP) |
| `routers/catalog.py` | `POST /v1/catalog/descriptor` - HealthDCAT-AP JSON-LD dataset descriptor |
| `routers/reports.py` | `GET /v1/reports`, `/v1/reports/{job_id}` - durable Transformation Passports |
| `routers/connectors.py` | `/v1/source-connections`, `/v1/output-destinations` - encrypted dataspace connectors |
| `routers/settings.py` / `routers/runtime.py` | `/v1/settings` (admin instance defaults) + open `/v1/runtime-config` |
| `schemas/` | Pydantic models: `fhir_ops.py`, `fhir_bulk.py`, `jobs.py`, `processing.py`, `scoring.py`, `agents.py`, `processing_runs.py` |
| `services/` | Business logic: `jobs.py`, `processing.py`, `analytics.py`, `fhir_server.py`, `synthetic.py`, `health.py`, `scoring.py`, `dicom.py`, `hl7v2.py`, `subscriptions.py`, `agents.py`, `permits.py`, `reports.py`, `connectors.py`, `settings.py`, `exposure.py` |

### Pipeline (`src/pipeline/`)

| Module | Stage | Role |
|---|---|---|
| `processor.py` | Orchestrator | Composes all pipeline sub-modules. Entry points: `process_data`, `process_data_batch`, `process_data_stream`. |
| `rule_matcher.py` | Pre | Builds per-resource rule index from FHIRPath expressions. Cached per `(resource_type, rules_hash)`. |
| `action_dispatcher.py` | **match** | Executes stateless actions immediately (redact, hash, encrypt, generalize…). Defers NLP → `NlpWork`, gPAS → `BatchWork`. |
| `nlp_orchestrator.py` | **phi_detection** (concurrent with pseudonymize) | Extracts all deferred NLP texts, deduplicates, sends one batch HTTP call to `nlp-lb`, applies replacements per-resource with isolated token state. |
| `gpas_orchestrator.py` | **pseudonymize** (concurrent with phi_detection) | Deduplicates all gPAS values across resources, sends one batch HTTP call to gPAS, writes pseudonyms back. |
| `post_processor.py` | **finalize** | Rewrites FHIR bundle references after ID changes. Replaces pseudonym-changed IDs in free-text fields. Aho-Corasick automaton for O(N+M) text scan. |
| `manifest.py` | Post | Attaches per-rule transformation summary to `meta.tag` when `MEDANON_MANIFEST_ENABLED=true`. |
| `config.py` | Config | Parses YAML profile. `${VAR:-default}` env interpolation. Raises `ValueError` on invalid config. |
| `config/service.py` | Config | `get_settings(profile)` with `@lru_cache(maxsize=8)`. Single entry point; profile loaded once per process. |
| `config/store.py` | Config | Config profile metadata index (PostgreSQL-backed). Tracks name, description, is_system. |
| `deidentify.py` | Actions | Unified action registry. `nlp_detect_by_path` routes through `_get_nlp_adapter()` lazy singleton. `nlp_detect_act` provides entity-specific action routing. |
| `permit_context.py` | Governance | Contextvar propagation of the active permit. Keyed actions scope their key to the permit; gPAS domains are suffixed `__permit-{id}` (D7.2 §4.4). `require_permit_if_regulated()` fails closed in regulated mode. |
| `exclusion.py` | Governance | Opt-out register (EHDS Art 71). Drops opted-out subjects + linked resources before pseudonymisation; two-pass (identifier → resource id) so identifier-keyed opt-outs also remove linked resources. |
| `transformation_passport.py` | Governance | Builds the anonymous release passport (identification, permit, tools, achieved k/l/t, privacy-risk, disclosure verdict). |
| `cumulative_exposure.py` | Governance | Keyed one-way population fingerprints + differencing-risk assessment across prior releases (D7.2 §5.5.7). Pure; durable side in `release_ledger`. |
| `field_classification.py` | Governance | Deterministic direct/quasi/non identifier labels for FHIR paths. Authoritative source the Resource Explorer consumes; reuses `identifier_gate`'s HIPAA catalog. |

### Governance & EHDS (`src/pipeline/governance/`, `disclosure/`, `minimization/`)

| Module | Role |
|---|---|
| `governance/permit.py` | `Permit` domain model + `PermitStatus` state machine (draft/submitted/approved/rejected/revoked), `is_active(at)` window, `covers_path` scope, `InMemoryPermitStore`. Pure, no I/O. |
| `governance/healthdcat.py` | HealthDCAT-AP `dcat:Dataset` JSON-LD descriptor (D7.2 §4.3, EHDS Art 55/78), optionally enriched from a Transformation Passport. |
| `governance/tool_registry.py` | Approved-tool registry (`ApprovedTool` + `ToolStatus`); `assess_tools()` feeds the passport's tool assessment. |
| `disclosure/decision.py` | `assess_export_decision()` - transparent Five-Safes output-checking rules (residual identifiers, re-id risk, min-k, synthetic duplicates, unjustified vars, permit R6-R8). Returns most-restrictive REFUSE/REFER/RELEASE. Pure. |
| `minimization/report.py` | `assess_minimisation()` - direct/quasi classification, granularity recommendations, special-category (Art 9) flags, purpose-limitation via declared paths. |

### Actions (`src/actions/`)

Pure functions: input value → transformed value. No side effects.

| File | Action | Reversible | Notes |
|---|---|---|---|
| `redact.py` | `redact` | No | Replaces with `replacement` param (default `""`) |
| `cryptohash.py` | `cryptohash` | No | HMAC-SHA3-256 (keyed) or plain SHA3-256. CSPRNG-safe. Warns if no key set. |
| `encrypt.py` | `encrypt` | Yes (RSA key) | RSA public-key encryption |
| `decrypt.py` | `decrypt` | — | RSA private-key decryption |
| `perturb.py` | `perturb` | No | Bounded random noise via `secrets.randbelow()` (CSPRNG) |
| `substitute.py` | `substitute` | No | Replace with fixed `substitute_with` value |
| `generalize.py` | `generalize` | No | `date_year`, `date_year_month`, `zip_prefix`, `age_bracket`, `number_round`, `category` |
| `scrub_text.py` | `scrub_text` | No | 16 regex patterns for structured PHI (phone, SSN, email, NPI…) |
| `deidentify.py` | `nlp_detect_act` | Configurable | Entity-specific conditional NLP. Per-entity-type action routing. Falls back to `redact` when NLP unavailable. |

### Async job system (`src/pipeline/jobs/`)

| Module | Role |
|---|---|
| `store.py` | `SqliteJobStore` (WAL mode, thread-safe, 2s polling). Fallback when Redis and PostgreSQL are unavailable. |
| `worker.py` | Async job executor. `RedisJobStore`: Redis Streams (XREADGROUP). `PostgresJobStore`: LISTEN/NOTIFY. `SqliteJobStore`: polls every 2s. `max_concurrent` semaphore. Periodic Redis index orphan sweep. |
| `worker_main.py` | Standalone worker entrypoint. Prometheus metrics on port 9091. PostgresJobStore fallback between Redis and SQLite. |
| `executors.py` | Bulk-operation executors: `bulk-export`, `cohort`, `patient-export`, `bulk-import`, `reprocess`. Cross-chunk gPAS dedup via `seen_values`. |
| `checkpoint.py` | Saves processing position to PostgreSQL for crash recovery — a failed job can be resumed from the last successful page. |
| `staged_worker/` | Two-phase staged bulk-export (`_core.py` + `_executors.py`). Phase 1: fetch + stage to `medanon.staged_resources`. Phase 2: claim partitions (`FOR UPDATE SKIP LOCKED`), de-identify, write NDJSON. Decouples slow FHIR fetch from processing. |
| `summary.py` | Job progress summary: resource counts, bytes processed, error rate. |

**Job store backend selection at startup:**

```
MEDANON_REDIS_URL set    → RedisJobStore   (Streams + consumer groups, cross-replica, at-least-once)
MEDANON_APP_DB_URL set   → PostgresJobStore (LISTEN/NOTIFY, single-instance or multi-worker)
Neither                  → SqliteJobStore   (polling, local dev only)
```

### Scoring system (`src/pipeline/scoring/`)

| Module | Role |
|---|---|
| `engine.py` | Composite scorer: `privacy_norm × utility × quality`. Runs all three sub-scorers. |
| `privacy.py` | Privacy risk: k-anonymity, l-diversity, HIPAA 18-identifier check, text risk. Hard gate — fails the composite if risk exceeds threshold. |
| `utility.py` | Utility: field retention rate, date precision, clinical code coverage, structural completeness. |
| `quality.py` | Quality: FHIR structural validity, required fields, reference integrity, valid code values. |
| `audit.py` | Markdown audit report builder. Formats per-resource findings into human-readable compliance report. |
| `models.py` | Dataclasses: `ScoringResult`, `PrivacyScore`, `UtilityScore`, `QualityScore`, `AuditFinding`. |
| `constants.py` | Scoring weights, thresholds, action classifications (utility-preserving vs. destructive). |

See [scoring-system.md](scoring-system.md) for the full scoring model documentation.

### AI agents (`src/integrations/ai/`)

Phase 4 feature. Activated with `MEDANON_AI_ENABLED=true`. All agents degrade gracefully to static fallbacks when AI is unavailable.

| Module | Role |
|---|---|
| `provider.py` | `LLMProvider` singleton. `litellm` wrapper with circuit breaker, TTL response cache, SSE streaming. |
| `agents/config_generator.py` | Few-shot config generation: all 8 bundled YAML profiles injected directly into the prompt. Validates output through `Settings` loader. Falls back to keyword matching. |
| `agents/pii_detector.py` | 3-layer PII detection: regex → NER (Presidio) → LLM. **LLM layer must use a local provider** (`MEDANON_AI_PII_PROVIDER`) — PHI must not be sent to external APIs. |
| `agents/rule_explainer.py` | Plain-language rule explanation via SSE streaming. Static descriptions as fallback. |
| `agents/compliance.py` | Regulatory gap analysis vs HIPAA, GDPR, and other frameworks. Static HIPAA fallback. |

### NLP integration (`src/integrations/nlp/`)

| Module | Role |
|---|---|
| `adapter.py` | `RemoteNlpAdapter` — delegates all NLP inference to the NLP microservice. Supports `detect_batch()` for the phi_detection stage. |
| `remote_detector.py` | HTTP client for the NLP microservice. `detect_batch_remote()` for batch requests. **Fail-closed:** returns `[NLP_UNAVAILABLE]` on any failure — no PHI leaks via unscrubbed text. |
| `utils.py` | Pure-Python NLP utilities: `HEALTHCARE_ENTITIES`, `_tokenize`, `_scrub_xhtml_text_nodes`. No Presidio dependency — safe to import in any context. |

### Utilities (`src/utils/`)

| Module | Role |
|---|---|
| `cache.py` | `CacheBackend` Protocol. `LocalLruCache` (50K entries, 10% eviction on overflow). `RedisCache` (L2, 1h TTL, errors swallowed). |
| `circuit_breaker.py` | Reusable three-state circuit breaker (CLOSED → OPEN → HALF_OPEN). Thread-safe. Used by gPAS, NLP, and AI integrations. |
| `fhirpath.py` | FHIRPath traversal: `find_nodes`, `error`, `not_implemented`. LRU-cached per expression. |
| `crypto.py` | RSA encrypt/decrypt. `bounded_random` uses `secrets.randbelow()` (CSPRNG). Path-traversal guard on key file paths. `derive_permit_key()` (HKDF-SHA256) scopes keyed actions to a permit; `hash_key_id()` records rotation traceability. |
| `metrics.py` | Prometheus counters + histograms: `medanon_requests_total`, `medanon_gpas_*`, `medanon_fhir_*`. |
| `audit.py` | Centralized audit logging. Structured JSON to file + optional Redis Stream (`medanon:audit`). `query()` reads back events. PHI is never logged. |
| `thread_pool.py` | Process-wide bounded `ThreadPoolExecutor`. Shared by pipeline stage parallelism. `submit_with_context()` carries contextvars (permit + correlation id) into worker threads - raw `pool.submit` resets them. |
| `regulated.py` | `regulated_mode()` (`MEDANON_REGULATED_MODE`, read at call time) + helpers (`gate_identifier_mode()` etc.) that turn fail-soft defaults into hard requirements across the stack. |

---

## 2.4 Data Consumers

### FHIR client (`src/integrations/fhir/`)

| Module | Role |
|---|---|
| `client.py` | Barrel file — re-exports from all sub-modules for backward compatibility. |
| `_transport.py` | Shared HTTP transport: connection pool, retry (2x, exponential backoff), pagination, ID validation/sanitization. |
| `reader.py` | Paginated fetch (`fetch_resources`, page size `FHIR_PAGE_SIZE`), `$everything`, bulk export status polling. |
| `writer.py` | `upload_resources` — batch Bundle PUT. `_infer_upload_tiers` — Bellman-Ford topological sort to satisfy referential integrity. `_compute_id_map` + `_rewrite_references`. `_strip_manifest_tags`. |
| `bulk.py` | FHIR `$export` operation (async kick-off + poll), NDJSON file download. |
| `adapter.py` | `FhirServerAdapter` — unified interface wrapping reader + writer + bulk. |

**Upload ordering (topological sort):** HAPI rejects a resource if it references a resource that does not exist yet. The writer builds a reference dependency graph and uses Bellman-Ford relaxation (`tier[A] = 1 + max(tier[B] for B in deps[A])`) to assign upload tiers. Resources in tier 0 have no dependencies and upload first. Resources within the same tier batch into a single Bundle PUT.

### Analytics microservice (`services/analytics/`)

A separate Docker service (~200 MB image). Proxied by the anonymizer at `/analyse/risk` and `/generate/synthetic` when `ANALYTICS_SERVICE_URL` is set.

| Endpoint | Method | Description |
|---|---|---|
| `/v1/analyse/risk` | POST | k-anonymity + l-diversity + re-identification risk scoring on de-identified NDJSON |
| `/v1/generate/synthetic` | POST | Synthetic FHIR patient generation (stdlib or SDV) |
| `/metrics` | GET | Prometheus metrics for the analytics service |

**Why a separate service?** SDV (Synthetic Data Vault), an optional dependency for advanced synthetic data, adds ~2 GB to the Docker image. Isolating it prevents this from bloating the anonymizer image. The analytics service is independently scalable and can be disabled entirely.

**Analytics modules:**
- `src/risk.py` — k-anonymity, l-diversity, prosecutor/journalist/marketer attacker models
- `src/synthetic.py` — stdlib synthetic patient generation (no SDV dependency)
- `src/synthetic_sdv.py` — SDV-powered synthesis (conditional, relational, time-series)

### NLP microservice (`services/nlp/`)

Always-on (~800 MB Docker image, Presidio + spaCy `en_core_web_lg`).

| Endpoint | Method | Description |
|---|---|---|
| `/v1/detect` | POST | Detect PII entities in a single text |
| `/v1/detect/batch` | POST | Batch detect PII entities across multiple texts (one HTTP call) |
| `/metrics` | GET | Prometheus metrics |
| `/health` | GET | Answered by Traefik gateway — no upstream |

**Why isolated from the anonymizer?** The spaCy model alone is ~800 MB. Running it in-process would double memory consumption per anonymizer replica. The NLP microservice keeps this cost fixed regardless of anonymizer scaling, and can be independently replicated for CPU-intensive NLP workloads.

**NLP modules:**
- `src/detector.py` — Presidio NER facade; deterministic `[[TYPE_N]]` token substitution; XHTML-safe output
- `src/recognizers.py` — Custom Presidio recognizers for healthcare-specific patterns (MRN, NPI, DEA number)
- `src/tokenizer.py` — Token state tracking + XHTML text node scrubbing

### FHIR Subscriptions (`src/integrations/subscriptions/`)

FHIR R4 rest-hook subscriptions. When a configured criteria is matched (e.g. new Patient resource created on source FHIR), the anonymizer receives a notification and triggers de-identification automatically.

| Module | Role |
|---|---|
| `store.py` | `SqliteSubscriptionStore` — FHIR R4 Subscription persistence |
| `integrations/postgres/subscription_store.py` | `PostgresSubscriptionStore` — drop-in replacement |

Subscriptions are managed via `/fhir/Subscription` endpoints. The anonymizer acts as both the subscription client (registering with the source FHIR server) and the webhook receiver (processing incoming notifications).

### SMART on FHIR (`src/api/routers/smart.py`)

Provides SMART on FHIR capability discovery and token introspection for integrations that use OAuth2/SMART for access control.

| Endpoint | Description |
|---|---|
| `GET /.well-known/smart-configuration` | RFC 8414 capability discovery |
| `POST /oauth2/introspect` | RFC 7662 token introspection |

SMART on FHIR is the standard authorization framework for healthcare APIs. Supporting it allows the anonymizer to be integrated into SMART-compliant clinical systems and EHR launch flows.

### Staging layer (`src/integrations/staging/`)

The two-phase staging store decouples FHIR fetch (slow, I/O-bound) from de-identification (CPU + gPAS-bound) in bulk operations.

| Module | Role |
|---|---|
| `store.py` | Intermediate PostgreSQL table (`medanon.staged_resources`). `ON CONFLICT DO NOTHING` for automatic dedup. `SELECT ... FOR UPDATE SKIP LOCKED` for contention-free parallel workers. |

**Phase 1:** Worker fetches all resource types from source FHIR and writes de-identified resources to `staged_resources`.  
**Phase 2:** Worker reads from staging, applies topological sort, uploads to target FHIR in tier order.  

This decoupling means a FHIR server timeout in Phase 1 does not require re-uploading already-processed resources in Phase 2.

### PostgreSQL stores (`src/integrations/postgres/`)

| Module | Role |
|---|---|
| `pool.py` | Shared `psycopg2.ThreadedConnectionPool` singleton (5–25 connections). One pool shared across all PostgreSQL stores to avoid multiple independent pools. |
| `job_store.py` | `PostgresJobStore` — drop-in for `SqliteJobStore`. `FOR UPDATE SKIP LOCKED` for contention-free multi-worker job claims. `NOTIFY`/`LISTEN` for instant wake-up. |
| `config_store.py` | `PostgresConfigStore` — drop-in for SQLite config store. |
| `subscription_store.py` | `PostgresSubscriptionStore` — drop-in for SQLite subscription store. |
| `permit_store.py` | `PostgresPermitStore` - durable `medanon.permits` (Art 79 audit queries). In-memory default; one-line startup swap. |
| `passport_store.py` | `PostgresPassportStore` - durable Transformation Passports with an `assert_pii_safe()` structural guard that fails closed before writing. |
| `connector_stores.py` / `settings_store.py` | Encrypted dataspace connectors + deployment-wide instance settings. |
| `release_ledger.py` | Keyed release fingerprints for cumulative-exposure analysis (`cumulative_exposure`). Stores only one-way hashes, never reversible ids. |

### Inlined domain types (`src/domain/`)

Core domain types inlined into the anonymizer service (previously a shared `packages/medanon-core/` library — removed to simplify the Docker build context):

- `domain/jobs.py` — `Job`, `JobStatus` dataclass and lifecycle enum (`pending → running → done / failed / cancelled`)
- Exception types: `JobStoreUnavailable`, `JobNotFound`, `JobNotComplete`, `JobResultMissing`

---

## Architecture Overview

For the end-to-end architecture see [architecture.md](architecture.md) and
[data-flow.md](data-flow.md), which cover:

- Entry points: REST API, CLI, async worker
- Service layer: ProcessingService, JobService, FhirServerService, ScoringService, AgentService
- Processing pipeline internals (4-stage diagram)
- NLP microservice integration
- Scoring system flow
