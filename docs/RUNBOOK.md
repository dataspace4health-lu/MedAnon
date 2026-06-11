# MedAnon — Operations Runbook

## Daily operations

### Start / stop

```bash
make up       # start full stack (preflight + docker compose + smoke verify)
make dev      # start with hot-reload (source mounted, HAPI uses in-memory H2)
make down     # stop containers; volumes preserved
make logs     # tail all container logs
make verify   # smoke-test a running stack
```

**gPAS (WildFly) takes ~90 seconds on first boot.** Wait until `docker compose ps` shows all services as `healthy` before processing data.

### Opt-in profiles

```bash
docker compose --profile ha up    # gPAS PostgreSQL read replica (HA)
docker compose --profile s3 up    # MinIO S3 object storage for job results
docker compose --profile ai up    # Ollama local LLM for AI agent endpoints
```

The NLP microservice (`nlp`) and analytics service (`analytics`) are **always-on** — they start with `make up`. `nlp-lb` is a Traefik gateway network alias, not a separate container.

### Check health

```bash
docker compose ps                          # all containers, health status
curl -s http://localhost:8000/health       # {"status":"ok"} — liveness (fast, no external calls)
curl -s http://localhost:8000/ready        # {"ready":true} — readiness (probes FHIR, gPAS, NLP)
curl -s http://localhost:8200/health       # NLP microservice liveness
curl -s http://localhost:9091/ready        # worker readiness probe
docker compose exec anonymizer tail -f /output/audit.log  # structured JSON audit log
```

`/health` vs `/ready`: Health is a lightweight liveness check used by Docker's healthcheck. Ready probes FHIR, gPAS, and NLP with a 5 s timeout each — use it to confirm the stack is actually operational, not just started.

---

## Make commands

| Command | Description |
|---|---|
| `make up` | Start full Docker stack |
| `make dev` | Start with hot-reload |
| `make down` | Stop containers |
| `make build` | Rebuild anonymizer + UI images |
| `make logs` | Tail container logs |
| `make verify` | Smoke-test running stack |
| `make init-domains` | Create gPAS pseudonymization domain |
| `make setup` | Create Python venv, install deps, download spaCy model |
| `make test` | Run full pytest suite |
| `make test-cov` | Run tests with coverage |
| `make lint` | ruff check anonymizer source |
| `make format` | ruff format anonymizer source |
| `make batch` | Run batch pipeline + generate analytics |
| `make fetch` | Fetch FHIR resources, anonymize, write NDJSON |
| `make helm-lint` | Validate Helm chart (no cluster needed) |
| `make helm-template` | Dry-run rendered K8s YAML |
| `make helm-install` | Install/upgrade on active cluster |
| `make helm-uninstall` | Remove Helm release |

---

## Config profile selection

Auto-selection: `GPAS_URL` set → `config_gpas.yaml`; otherwise → `config.yaml`.

Override per-request: `?config_profile=<name>` — values: `auto`, `minimal`, `gpas`, `gdpr`, `hipaa`, `research`, `structural`, `value-masking`.

See [policies.md](policies.md) for full compliance details per profile.

---

## Monitoring

### Prometheus metrics

```bash
curl -s http://localhost:8000/metrics      # anonymizer metrics
curl -s http://localhost:9091/metrics      # worker metrics
```

Key metrics exposed:
- `medanon_requests_total{method,path,status}` — request counts
- `medanon_request_duration_seconds{path}` — latency histogram
- `medanon_gpas_calls_total{operation,cached}` — gPAS call rate + cache hit rate
- `medanon_gpas_latency_seconds` — gPAS round-trip latency
- `medanon_fhir_calls_total{operation,server}` — FHIR client call counts
- `medanon_nlp_calls_total{cached}` — NLP microservice call count and cache hits
- `medanon_jobs_total{status}` — job completion counts (worker metrics port 9091)
- `medanon_circuit_breaker_state{name}` — 0=closed, 1=half_open, 2=open per integration
- `medanon_circuit_breaker_trips_total{name}` — CLOSED→OPEN transitions
- `medanon_proxy_retries_total{upstream,reason}` — outbound HTTP retry rate (`reason`: `http_5xx`, `http_429`, `connection`)
- `medanon_bulkhead_acquired_total{upstream}` / `medanon_bulkhead_rejected_total{upstream}` — per-upstream concurrency saturation; sustained `rejected` rate signals a need to raise `BULKHEAD_<UPSTREAM>_MAX_CONCURRENT` or scale the upstream
- `medanon_fhirpath_cache_{hits,misses,size,maxsize}{cache}` — sampled at scrape time; cache types: `compile`, `classify`, `where_plan`, `candidates`. A high miss rate at `size == maxsize` means the cache is thrashing — raise `FHIRPATH_CACHE_SIZE`

### SLO targets (default)

| SLO | Target | Metric |
|---|---|---|
| anonymizer p95 `/process` latency | < 500 ms | `medanon_request_duration_seconds{path="/v1/process"}` |
| gPAS p95 round-trip | < 200 ms | `medanon_gpas_latency_seconds` |
| Bulkhead rejection rate | < 0.1 % of acquired | ratio of `bulkhead_rejected` / `bulkhead_acquired` |
| Worker job success | > 99 % | `medanon_worker_jobs_total{status="done"}` / total |

All pods have Prometheus scrape annotations (`prometheus.io/scrape: "true"`) in the Helm charts.

### Health vs readiness

- `/health` — process liveness; cheap; always 200 unless the FastAPI app died.
- `/ready` — dependency check (Redis + Postgres + gPAS canary); 503 until ready.
- Helm charts use a `startupProbe` against `/ready` with up to 5 minutes (60 × 5 s) before liveness kicks in. This accommodates gPAS WildFly cold-start (~90 s) without restart-looping the pod.
- For Compose, the `redis` healthcheck must be `healthy` before anonymizer + worker start; gPAS uses a 10-minute `start_period` for the same reason.

### Redis durability

When `MEDANON_REDIS_URL` is set, the anonymizer logs at startup whether AOF (append-only file) persistence is enabled. AOF should be on in production — RDB snapshots alone may be up to 60 s stale, which can lose queued jobs across an unexpected restart.

```bash
# Verify AOF is enabled in your redis.conf
docker compose exec redis redis-cli config get appendonly
# 1) "appendonly"
# 2) "yes"
```

Set `MEDANON_REQUIRE_REDIS_AOF=true` to make the anonymizer refuse to start when AOF is disabled — recommended for production.

### Job store durability guard (C11)

The dedicated `worker` container shares `/output` with the API container, so the SQLite job store at `/output/jobs.db` is **never safe** in this configuration — SQLite WAL across container boundaries can corrupt or double-claim jobs.

The runtime enforces this via `pipeline/jobs/store_factory.assert_durable_store_or_exit()`:

- The `worker` container ALWAYS exits with status 2 if neither `MEDANON_REDIS_URL` nor `MEDANON_APP_DB_URL` is reachable.
- The API container exits with status 2 in the same situation **only when** `MEDANON_REQUIRE_DURABLE_STORE=true`. By default it logs a warning and falls back to SQLite (single-container dev mode).
- Override the guard for single-container local development with `MEDANON_ALLOW_SQLITE_FALLBACK=true`.

Recommended production posture: set `MEDANON_REDIS_URL` AND `MEDANON_APP_DB_URL` AND `MEDANON_REQUIRE_DURABLE_STORE=true`.

### NLP cache diagnostics

The NLP microservice keeps two cache tiers:

- **L1** — in-process LRU (~20k entries, per replica). Lost on container restart.
- **L2** — Redis DB 2 (key prefix `medanon:nlp:detect:`, TTL `NLP_REDIS_TTL_SEC`, default 7 days). Shared across NLP replicas, survives restarts.

```bash
# Confirm L2 is wired (logs at startup)
docker compose logs nlp 2>&1 | grep -E 'nlp_l2_cache_(initialized|disabled|init_failed)'

# Inspect L2 key count and a sample key
docker compose exec redis redis-cli -a "$MEDANON_REDIS_PASSWORD" -n 2 \
  --no-auth-warning DBSIZE
docker compose exec redis redis-cli -a "$MEDANON_REDIS_PASSWORD" -n 2 \
  --no-auth-warning --scan --pattern 'medanon:nlp:detect:*' | head

# Force a cold path (clear L2 + restart NLP replicas)
docker compose exec redis redis-cli -a "$MEDANON_REDIS_PASSWORD" -n 2 FLUSHDB
docker compose restart nlp
```

**Cold-cache symptom:** First bulk export after `make up` runs ~4× slower than subsequent runs because both L1 and L2 are empty and every text snippet must hit Presidio + spaCy. Once L2 warms, subsequent NLP container restarts re-hydrate L1 lazily from L2 — restart latency disappears.

### Audit log

The anonymizer emits structured JSON audit events to three destinations simultaneously:

1. **stdout** (always) — captured by Docker/K8s log driver; forward to ELK, Loki, Splunk
2. **Redis Stream** `medanon:audit` (when `MEDANON_REDIS_URL` set) — queryable via `GET /v1/audit`
3. **Rotating file** (when `MEDANON_AUDIT_LOG_FILE` set) — 10 MB/file, 5 backups

Each entry records: timestamp, HTTP method, path, status code, request ID, auth subject, auth method. **PHI is never logged.**

```bash
# Follow the audit log in the container
docker compose exec anonymizer tail -f /output/audit.log | python3 -m json.tool

# Query via API (requires MEDANON_REDIS_URL)
curl -s http://localhost:8000/v1/audit?count=50 -H "X-API-Key: $ADMIN_KEY"
```

---

## Backup

### gPAS PostgreSQL (pseudonym mappings — critical)

Loss of the gPAS database means pseudonym-to-original mappings are unrecoverable. Back up before any destructive operation.

```bash
# Backup
docker exec gpas-postgres pg_dump -U gpas_user -d gpas -F c -f /tmp/gpas_backup.dump
docker cp gpas-postgres:/tmp/gpas_backup.dump ./backup/gpas-db-$(date +%Y%m%d).dump

# Restore (stack must be down)
docker compose down
docker compose up -d gpas-db
docker cp ./backup/gpas-db-YYYYMMDD.dump gpas-postgres:/tmp/restore.dump
docker exec gpas-postgres pg_restore -U gpas_user -d gpas --clean /tmp/restore.dump
docker compose up -d
```

### HAPI FHIR data

With the default H2 in-memory database, FHIR data is lost on every container restart (intentional for dev). For production, the `hapi-postgres` and `hapi-target-postgres` containers provide persistent storage. Back up with `pg_dump`.

### Config and keys

```bash
cp -r services/anonymizer/config/ backup/config-$(date +%Y%m%d)/
cp services/anonymizer/keys/id_rsa* backup/keys-$(date +%Y%m%d)/
```

---

## Secret rotation

| Secret | How to rotate | Impact |
|---|---|---|
| `MEDANON_HASH_KEY` | Update `.env`, restart anonymizer | All existing cryptohash pseudonyms change — old output cannot be re-linked to new |
| RSA private key | Generate new keypair, update `.env` paths | Old encrypted values become unreadable; keep old key for historical data |
| `GPAS_BASIC_PASS` | Update `.env` + `CALL changePassword('user@ths','new');` in gRAS | Existing gPAS sessions invalidated |
| `GPAS_DB_PASSWORD` | `docker compose down -v` to recreate PostgreSQL volume | **Destroys all pseudonym mappings** — back up first |
| `MEDANON_API_KEY` | Update `.env`, restart anonymizer | All API clients must update their key |

---

## Troubleshooting

### gPAS connection failure

**Symptom:** `/ready` returns `false`; requests with `gpas_pseudonymize` return 500.

```bash
docker compose ps gpas                             # check health status
curl -v http://localhost:8080/ttp-fhir/fhir/gpas/metadata   # direct connectivity test
docker compose logs gpas --tail 50                 # check for deployment errors
```

Verify env: `GPAS_URL`, `GPAS_DOMAIN`, `GPAS_BASIC_USER`, `GPAS_BASIC_PASS` in `.env`.

### gPAS "Unknown Domain"

Domain not created yet. Run `make init-domains` or create it via `http://localhost:8080/gpas-web/`. **Never insert domains directly into PostgreSQL** — gPAS maintains an in-memory `domainLocks HashMap` that is only populated via its own API. Direct SQL inserts bypass this and cause "domain not found" errors at runtime even though the row exists in the DB.

### gPAS circuit breaker open

**Symptom:** "gPAS circuit breaker is OPEN" in logs/response.

gPAS failed 5+ times within 60 s (default thresholds). The circuit opens to fail-fast subsequent calls instead of waiting for timeouts. It recovers automatically: after 30 s, one probe call is attempted. If successful, the circuit closes.

```bash
docker compose restart gpas    # force recovery if the issue is resolved
```

Tune thresholds: `GPAS_CB_FAILURE_THRESHOLD`, `GPAS_CB_RECOVERY_TIMEOUT_SEC`, `GPAS_CB_WINDOW_SEC`.

### gPAS keeps restarting

PostgreSQL is still initializing (schema creation on first boot takes 15–30 s):

```bash
docker compose ps gpas-db             # wait for "healthy"
docker compose logs gpas-db --tail 20 # check init progress
docker compose restart gpas           # restart once gpas-db is healthy
```

### NLP microservice unavailable

**Symptom:** Text fields contain `[NLP_UNAVAILABLE]` placeholders; `/ready` reports NLP check failed.

```bash
docker compose ps nlp gateway                   # check both are running
curl -s http://localhost:8200/health            # NLP route via Traefik gateway
docker compose logs nlp --tail 50               # check NLP for startup errors
docker compose logs gateway --tail 20           # check Traefik routing errors
```

NLP fails closed — PHI is replaced with `[NLP_UNAVAILABLE]` rather than leaking. Scale NLP replicas if latency is high:

```bash
docker compose up -d --scale nlp=3
```

### AI agent errors

**Symptom:** `GET /v1/ai/status` returns `enabled: false` or `provider_status: unavailable`.

```bash
# Check if AI is enabled
grep MEDANON_AI_ENABLED .env

# Check Ollama (if using --profile ai)
docker compose --profile ai ps ollama
docker compose --profile ai logs ollama --tail 30

# Verify model is pulled
docker exec medanon-ollama ollama list
docker exec medanon-ollama ollama pull llama3.2
```

If using an external LLM: verify `MEDANON_AI_API_BASE` and `MEDANON_AI_MODEL` in `.env`.

**IMPORTANT — PHI safety:** `MEDANON_AI_PII_PROVIDER` must always point to the local Ollama model, never to an external API. PII detection sends text that may contain PHI to this model.

### 503 on job endpoints (`/v1/jobs/*`)

Job store not initialized. Check in priority order:
1. `MEDANON_REDIS_URL` set and Redis reachable: `docker compose ps redis`
2. `MEDANON_APP_DB_URL` set and `app-db` reachable: `docker compose ps app-db`
3. SQLite fallback: `MEDANON_JOB_DB` path is writable inside the container (default `/output/jobs.db`) — only safe in single-container dev mode (see "Job store durability guard" above).

Backend selection order at startup: Redis → PostgreSQL (`app-db`) → SQLite. Centralised in `pipeline/jobs/store_factory.select_job_store()`.

### Worker not picking up jobs

```bash
docker compose ps worker                        # check health status
curl -s http://localhost:9091/ready             # worker readiness probe
docker compose logs worker --tail 50

# Check job queue depth (Redis backend)
docker exec medanon-redis redis-cli -a $REDIS_PASSWORD LLEN medanon:jobs

# Check job status directly
curl -s http://localhost:8000/v1/jobs?status=pending -H "X-API-Key: $MEDANON_API_KEY"
```

### Bulk export is slow

Tune these variables in `.env`:

```bash
MEDANON_BATCH_SIZE=1000         # resources per gPAS batch (default; raise only if gPAS is fast and memory allows)
FHIR_PAGE_SIZE=500              # resources per FHIR paginated fetch
MEDANON_FHIR_FETCH_PARALLEL=1   # keep at 1 — more threads compete for GIL without benefit
MEDANON_COHORT_PARALLEL=2       # parallel $everything calls (safe, I/O-bound)
MEDANON_JOB_WORKERS=10          # concurrent background jobs
```

**Why `FHIR_FETCH_PARALLEL=1`?** The bottleneck is gPAS (sequential HTTP per batch). Adding parallel FHIR fetch threads makes them compete for the GIL and queue lock while waiting for gPAS — this increases overhead without reducing total time. Observed: 4 parallel threads was slower than 1.

### FHIR upload failures (HAPI-1094 — referenced resource not found)

This error means a resource was uploaded before a resource it references. The uploader computes a topological sort based on `reference` fields. If you see this error in logs, it indicates a reference pattern the topological sort didn't catch. Check `docker compose logs anonymizer` for `tier map:` debug output.

### FHIR gender rejection (HAPI-1821)

**Symptom:** `Patient` or `Practitioner` resources rejected: "not a valid code for `http://hl7.org/fhir/ValueSet/administrative-gender`".

FHIR R4 binds `Patient.gender` and `Practitioner.gender` to the `AdministrativeGender` value set (`male | female | other | unknown`). The `config_structure_preserving.yaml` profile uses `substitute_with: "unknown"` for gender fields — this is correct. If you use a custom profile that substitutes `[REDACTED]`, HAPI will reject it. Always use a valid code value.

### 413 Request Too Large

```bash
MEDANON_MAX_BODY_BYTES=20971520    # 20 MB
```

### OOM killed container

```bash
docker inspect --format='{{.State.OOMKilled}}' <container>
```

Current memory limits (tuned for 16 GB host): anonymizer 3 GB, worker 2 GB, gPAS 2.5 GB, gpas-postgres 2 GB. Increase in `docker-compose.yml` only if you observe OOM kills during bulk export.

### Port conflicts

```bash
lsof -i :8000    # find conflicting process
```

Override port mappings in `.env`: `ANONYMIZER_PORT`, `UI_PORT`, `HAPI_PORT`, `GPAS_PORT`.

### Docker healthcheck failure: "container has no healthcheck configured"

If `depends_on: condition: service_healthy` is configured, the target container must have a healthcheck defined. Setting `healthcheck: disable: true` breaks the dependency chain. The anonymizer healthcheck must target `/health` (not `/ready` — `/ready` calls FHIR and gPAS and may time out during startup).

---

## Go-live checklist

### Secrets and auth
- [ ] `MEDANON_HASH_KEY` set (`openssl rand -hex 32`)
- [ ] `MEDANON_API_KEY` set for authenticated access
- [ ] `GPAS_BASIC_PASS` rotated from default
- [ ] `GPAS_DB_PASSWORD` rotated from default (PostgreSQL — not MySQL)
- [ ] `MEDANON_REDIS_PASSWORD` set
- [ ] `HAPI_DB_PASSWORD` and `HAPI_TARGET_DB_PASSWORD` set
- [ ] No secrets in git (`git status` — verify `.env` is gitignored)

### Network and TLS
- [ ] TLS termination at reverse proxy (nginx/Caddy/Traefik) or Ingress controller
- [ ] `MEDANON_CORS_ORIGINS` restricted to known origins
- [ ] gPAS web UI (port 8080) not publicly accessible
- [ ] Source FHIR server has no external port — verify `docker compose ps fhir-server` shows no host port

### Logging, monitoring, and compliance
- [ ] `LOG_LEVEL=INFO` (DEBUG may log resource content containing PHI)
- [ ] `MEDANON_MANIFEST_ENABLED=true` (GDPR Art. 30 accountability)
- [ ] `MEDANON_SCORING_ENABLED=true` (enable processing run history and scoring)
- [ ] `MEDANON_RESULT_TTL_SEC=86400` (clean up job results after 24 h)
- [ ] Audit log volume mounted and forwarded to log aggregator
- [ ] Prometheus scraping configured; worker metrics port 9091 scraped

### NLP and AI
- [ ] NLP microservice healthy: `curl http://localhost:8200/health`
- [ ] If AI enabled: `MEDANON_AI_PII_PROVIDER` points to local Ollama (not external API)
- [ ] If AI enabled: Ollama model pulled (`ollama list` shows the configured model)

### gPAS
- [ ] Domain created via web UI or `make init-domains` (never direct SQL)
- [ ] `GPAS_DOMAIN` matches exactly
- [ ] gPAS PostgreSQL backed up before first production run
- [ ] `curl http://localhost:8080/ttp-fhir/fhir/gpas/metadata` returns 200

### Validation
- [ ] `curl http://localhost:8000/ready` returns `{"ready": true}` for all components
- [ ] End-to-end: POST a sample Patient to `/process`, verify output is de-identified
- [ ] Score the output: `POST /v1/score` returns `privacy.gate: PASS`
- [ ] `/v1/analyse/risk` run on de-identified output before data sharing
- [ ] Appropriate config profile selected — see [policies.md](policies.md)
