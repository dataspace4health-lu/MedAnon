---
title: "Infrastructure"
sidebar_position: 2
description: "Docker services, networks, routing, databases, Redis, and shared configuration."
---

# Infrastructure

## Docker Services

### Always-on services

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | `8000` | FastAPI de-identification engine |
| `worker` | `medanon:latest` | `9091` (metrics) | Dedicated async job worker |
| `ui` | `medanon-ui:latest` | `8501` | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | none (isolated) | Source HAPI FHIR R4, identified data |
| `hapi-db` | `postgres:16-alpine` | internal | PostgreSQL backing source HAPI |
| `fhir-target` | `hapiproject/hapi:v7.6.0` | `8082` | Target HAPI FHIR R4, de-identified data |
| `hapi-target-db` | `postgres:16-alpine` | internal | PostgreSQL backing target HAPI |
| `gateway` | `traefik:v3` | `8080` (gPAS), `8200` (NLP) | API gateway, Docker-provider service discovery; carries `gpas-lb` / `nlp-lb` network aliases |
| `gpas` | WildFly 38 + gPAS | via gateway | Reversible pseudonymization (TTP); scaled with `--scale gpas=N` |
| `gpas-db` | `postgres:16-alpine` | internal | gPAS pseudonym store |
| `app-db` | `postgres:16-alpine` | internal | Jobs, configs, subscriptions, staging |
| `redis` | `redis:7-alpine` | internal | Job queue (Redis Streams) + gPAS L2 cache |
| `analytics` | `medanon-analytics:latest` | `8100` | Risk analysis + synthetic data |
| `scoring` | `medanon-scoring:latest` | `8300` | Scoring microservice (privacy × utility × quality) |
| `nlp` | `medanon-nlp:latest` | via gateway | Presidio NLP microservice (~800 MB image); scaled with `--scale nlp=N` |

### Opt-in profiles

Started with `--profile <name>` appended to any `docker compose` command:

| Container | Profile | Host Port | Role |
|---|---|---|---|
| `gpas-db-replica` | `ha` | internal | PostgreSQL streaming replica for gPAS HA |
| `trust-gate` | `trust` | `8400` | Pre-privacy data-quality barrier; emits Quality Passport (PASS / CONDITIONAL_PASS / BLOCK) |
| `trust-gate-ui` | `trust` | `8401` | Trust Gate management UI |
| `fhir-validator` | `trust` | internal | HL7 FHIR validator sidecar used by Trust Gate for structural/profile conformance |
| `keycloak` + `keycloak-db` | `auth` | `8180` | Keycloak OIDC identity provider (realm `medanon`, client `medanon-ui`) |
| `minio` | `s3` | `9000`, `9001` | MinIO S3-compatible object storage for job results |
| `ollama` | `ai` | internal | Local LLM inference for AI agent endpoints |
| `sql-source-test-db` | `sqltest` | internal | PostgreSQL fixture for SQL/tabular source connector testing |
| `prometheus` / `grafana` / `cadvisor` / `jaeger` | `monitoring` | 9090 / 3000 / 8888 / 16686 | Prometheus metrics, Grafana dashboards, container metrics, Jaeger OTel traces |

### Scaling

Traefik discovers replicas automatically via Docker labels, no config reload needed:

```bash
docker compose up -d --scale gpas=3   # 3 gPAS replicas (round-robin via gateway)
docker compose up -d --scale nlp=4    # 4 NLP replicas (round-robin via gateway)
```

---

## Networks

Two Docker bridge networks enforce physical isolation between identified and de-identified data:

| Network | Members | Purpose |
|---|---|---|
| `processing-net` | All services | Main application network |
| `source-net` | `fhir-server`, `hapi-db`, `anonymizer`, `worker`, **`ui`** | Network for identified data, no host port. The `ui` container joins it too, and nginx proxies `/fhir/*` to `fhir-server` with no auth check, so this network is not an authentication boundary. See [Security Model § 1.4](../../explanation/security-model.md#14-edge-routes-that-bypass-authentication). |

The source FHIR server has **no published host port**, which keeps it off the host's network. It is not, however, reachable only through the anonymizer: the `ui` container also bridges `source-net`, and its nginx proxies `/fhir/*` straight to `fhir-server` unauthenticated.

---

## Edge & Routing

### UI nginx, port 8501

Serves the React SPA and proxies API and FHIR requests:

| Location | Target | Strategy |
|---|---|---|
| `/` | SPA static files | `try_files` → `index.html` fallback |
| `/api/*` | `medanon:8000` | `least_conn`, keepalive 16, 120s timeout |
| `/fhir/*` | `hapi-fhir:8080` | Dynamic DNS (`resolver 127.0.0.11`), 300s timeout |
| `/fhir-target/*` | `hapi-fhir-target:8080` | Dynamic DNS, 300s timeout |
| `/healthz` | nginx | Returns `200 OK` for Docker healthcheck |

Security headers added: `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`. Static assets served with `Cache-Control: public, immutable`.

:::note Why dynamic DNS?
Using a variable `$upstream` with `resolver 127.0.0.11` forces nginx to re-resolve the container hostname on each request. Without this, nginx caches the IP at startup and returns 502 after container restarts, even though DNS updates immediately.
:::

### Traefik gateway, ports 8080 (gPAS) and 8200 (NLP)

The gateway joins `processing-net` under the `gpas-lb` and `nlp-lb` aliases, so existing env values (`GPAS_URL=http://gpas-lb:80/...`, `NLP_SERVICE_URL=http://nlp-lb:8200`) work without changes.

| Entry point | Routes to | Strategy |
|---|---|---|
| `gpas` port `8080`, `/gpas-web`, `/gras-web` | `gpas:8080` | Sticky sessions (`gpas_session` cookie), keeps JSF ViewState bound to one replica |
| `gpas` port `8080`, `/` | `gpas:8080` | Round-robin |
| `nlp` port `8200`, `/` | `nlp:8200` | Round-robin |
| `metrics` port `8082`, internal | Traefik Prometheus exporter | Not host-published |

:::note Why sticky only for gPAS web UI?
The gPAS WildFly application uses JSF, which stores ViewState in server-side session memory. Round-robin would break the UI mid-form. The REST API (`$pseudonymizeAllowCreate`) is stateless and uses round-robin.
:::

---

## PostgreSQL Databases

Four independent PostgreSQL 16 instances, each with its own lifecycle, backup schedule, and resource limits:

| Instance | Container | Schema owner | What it stores |
|---|---|---|---|
| **App-db** | `app-db` | `medanon` | Jobs, config profiles, subscriptions, staging, processing runs |
| **gPAS-db** | `gpas-db` | gPAS / WildFly | Pseudonym mappings (managed exclusively by gPAS, not directly accessible) |
| **Source HAPI** | `hapi-db` | HAPI | Identified clinical FHIR resources |
| **Target HAPI** | `hapi-target-db` | HAPI | De-identified FHIR resources |

The `medanon` schema has no static SQL file - each Postgres-backed store creates its own tables in code via `ensure_schema()` on first use (`services/anonymizer/src/integrations/postgres/*.py`, plus `integrations/staging/store.py`):

```
medanon.jobs                    - async job records (job_store.py)
medanon.job_details             - per-job detail payloads (job_detail_store.py)
medanon.configs                 - user-defined config profiles (config_store.py)
medanon.subscriptions           - FHIR R4 Subscriptions (subscription_store.py)
medanon.processing_runs         - scoring history (processing_run_store.py)
medanon.api_keys                - per-client API keys, hashed (api_key_store.py)
medanon.staged_resources        - two-phase staging for bulk operations (staging/store.py)
medanon.staged_partitions       - staging partition tracking (staging/store.py)
medanon.dead_letter_partitions  - failed staging partitions (staging/store.py)
medanon.permits                 - EHDS data permits (permit_store.py)
medanon.transformation_passports - D7.2 transformation passports (passport_store.py)
medanon.source_connections      - saved input connectors (connector_stores.py)
medanon.output_destinations     - saved output connectors (connector_stores.py)
medanon.app_settings            - instance settings (settings_store.py)
medanon.release_ledger          - cumulative-exposure release history (release_ledger.py)
medanon.sql_connections         - SQL/tabular source connections (sql_connection_store.py)
medanon.trust_profiles          - saved Trust Gate profiles (trust_profile_store.py)
medanon.workflows / medanon.workflow_steps - saved workflow definitions (workflow_store.py)
```

---

## Redis

Redis 7 serves three independent roles on separate logical databases:

| DB | Role | Activated by |
|----|------|-------------|
| `0` | Job queue (Redis Streams) **and** gPAS L2 pseudonym cache | `MEDANON_REDIS_URL` |
| `1` | Reserved |, |
| `2` | NLP L2 detection cache | `NLP_REDIS_URL` (defaulted in compose) |

### Job queue (DB 0)

Jobs submitted to `/v1/jobs/*` are pushed to a Redis Stream (`medanon:job_stream`). Workers consume via `XREADGROUP` with at-least-once delivery semantics. If a worker crashes before ACKing, the message stays in the Pending Entry List (PEL) and is reclaimed by `XAUTOCLAIM`. Multiple anonymizer replicas share the same queue.

### gPAS pseudonym cache (L2, DB 0)

Cross-replica sharing of gPAS results. Key format: `medanon:gpas:["pseudonymize", url, domain, op, original_id]`. TTL: 1 hour. Redis errors are swallowed, a Redis outage degrades performance but does not break de-identification.

### NLP detection cache (L2, DB 2)

Cross-replica sharing of Presidio detection results. Key format: `medanon:nlp:detect:<lang>:<threshold>:<entities-hash>:<text-sha256>`. Default TTL: 7 days. Survives NLP container restarts, eliminating cold-cache penalties after deploys. Implemented in `services/nlp/src/cache.py`.

:::tip Why three cache layers?
L1 (in-process LRU per replica) is fastest but not shared. L2 (Redis) shares across replicas and survives restarts. For a fleet of N anonymizer + M NLP replicas running parallel bulk exports, L2 collapses N×M cold-start costs into a single warm-up.
:::

---

## Shared Configuration

All services read configuration from environment variables, set in `.env` (Docker Compose) or Helm values (Kubernetes).

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_HASH_KEY` | (required in prod) | HMAC-SHA3-256 key for `cryptohash`. Without this, plain SHA3-256 is used, reversible via rainbow tables. |
| `MEDANON_API_KEY` | (blank = open) | API authentication key. Leave blank for local dev only. |
| `MEDANON_REDIS_URL` |, | Enables Redis job store + gPAS L2 cache. Format: `redis://:password@redis:6379/0`. |
| `NLP_REDIS_URL` | `redis://:…@redis:6379/2` (in compose) | Enables NLP L2 detection cache on Redis DB 2. |
| `NLP_REDIS_TTL_SEC` | `604800` (7 days) | TTL for NLP L2 cache entries. |
| `MEDANON_APP_DB_URL` |, | PostgreSQL URL for app state. |
| `GPAS_URL` |, | gPAS server URL. When set, auto-selects `config_gpas.yaml` profile. |
| `NLP_SERVICE_URL` | `http://nlp-lb:8200` | NLP microservice URL. |
| `ANALYTICS_SERVICE_URL` | `http://analytics:8100` | Analytics microservice URL. |
| `LOG_LEVEL` | `INFO` | `DEBUG` may log resource content containing PHI, use `INFO` in production. |
| `MEDANON_MANIFEST_ENABLED` | `false` | Attach transformation manifest to `meta.tag`. Required for GDPR Art. 30 accountability. |

Full variable reference: [Configuration](../configuration.md).
