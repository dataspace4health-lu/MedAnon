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
docker compose --profile analytics up   # analytics microservice
docker compose --profile nlp up         # Presidio NLP microservice (~800 MB)
docker compose --profile ha up          # gPAS MySQL read replica
```

### Check health

```bash
docker compose ps                          # all containers, health status
curl -s http://localhost:8000/health       # {"status":"ok"} — liveness (fast, no external calls)
curl -s http://localhost:8000/ready        # {"ready":true} — readiness (probes FHIR + gPAS)
docker compose exec anonymizer tail -f /output/audit.log  # structured JSON audit log
```

`/health` vs `/ready`: Health is a lightweight liveness check used by Docker's healthcheck. Ready probes FHIR and gPAS with a 5 s timeout each — use it to confirm the stack is actually operational, not just started.

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

Override per-request: `?config_profile=<name>` — values: `auto`, `minimal`, `gpas`, `gdpr`, `hipaa`, `research`, `structural`.

See [policies.md](policies.md) for full compliance details per profile.

---

## Monitoring

### Prometheus metrics

```bash
curl -s http://localhost:8000/metrics
```

Key metrics exposed:
- `medanon_requests_total{method,path,status}` — request counts
- `medanon_request_duration_seconds{path}` — latency histogram
- `medanon_gpas_calls_total{operation,cached}` — gPAS call rate + cache hit rate
- `medanon_gpas_latency_seconds` — gPAS round-trip latency
- `medanon_fhir_calls_total{operation,server}` — FHIR client call counts

All pods have Prometheus scrape annotations (`prometheus.io/scrape: "true"`) in the Helm charts.

### Audit log

The anonymizer writes a structured JSON audit log to `/output/audit.log` (mapped to `./output/audit.log` on the host). Each line records: timestamp, HTTP method, path, status code, request ID, auth subject, auth method. **PHI is never logged.**

```bash
docker compose exec anonymizer tail -f /output/audit.log | python3 -m json.tool
```

---

## Backup

### gPAS MySQL (pseudonym mappings — critical)

Loss of the gPAS database means pseudonym-to-original mappings are unrecoverable. Back up before any destructive operation.

```bash
# Backup
docker run --rm \
  -v gpas-db-data:/data \
  -v $(pwd)/backup:/backup \
  busybox tar czf /backup/gpas-db-$(date +%Y%m%d).tar.gz -C /data .

# Restore (stack must be down)
docker compose down
docker run --rm \
  -v gpas-db-data:/data \
  -v $(pwd)/backup:/backup \
  busybox sh -c "rm -rf /data/* && tar xzf /backup/gpas-db-YYYYMMDD.tar.gz -C /data"
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
| `GPAS_MYSQL_ROOT_PASSWORD` | `docker compose down -v` to recreate MySQL | **Destroys all pseudonym mappings** — back up first |
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

Domain not created yet. Run `make init-domains` or create it via `http://localhost:8080/gpas-web/`. **Never insert domains directly into MySQL** — gPAS maintains an in-memory `domainLocks HashMap` that is only populated via its own API. Direct SQL inserts bypass this and cause "domain not found" errors at runtime even though the row exists in the DB.

### gPAS circuit breaker open

**Symptom:** "gPAS circuit breaker is OPEN" in logs/response.

gPAS failed 5+ times within 60 s (default thresholds). The circuit opens to fail-fast subsequent calls instead of waiting for timeouts. It recovers automatically: after 30 s, one probe call is attempted. If successful, the circuit closes.

```bash
docker compose restart gpas    # force recovery if the issue is resolved
```

Tune thresholds: `GPAS_CB_FAILURE_THRESHOLD`, `GPAS_CB_RECOVERY_TIMEOUT_SEC`, `GPAS_CB_WINDOW_SEC`.

### gPAS keeps restarting

MySQL is still initializing (schema creation on first boot takes 15–30 s):

```bash
docker compose ps gpas-db             # wait for "healthy"
docker compose logs gpas-db --tail 20 # check init progress
docker compose restart gpas           # restart once gpas-db is healthy
```

### 503 on job endpoints (`/v1/jobs/*`)

Job store not initialized. Check:
- `MEDANON_REDIS_URL` is correct and Redis is reachable: `docker compose ps redis`
- SQLite fallback: `MEDANON_JOB_DB` path is writable inside the container

Root cause of past bug: `from pipeline.jobs.store import _job_store` captured `None` at import time. `init_job_store()` wrote to `pipeline.jobs.store._job_store` but the re-exported name stayed `None`. Fixed by reading directly from the authoritative module. If you see this error, ensure you're on a version after this fix.

### Bulk export is slow

Tune these variables in `.env`:

```bash
MEDANON_BATCH_SIZE=300          # resources per gPAS batch (smaller = more frequent gPAS calls)
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

Increase the memory limit in `docker-compose.yml` (anonymizer: 6 GB, HAPI: 3 GB, gPAS: 6 GB).

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
- [ ] `GPAS_MYSQL_ROOT_PASSWORD` rotated from default
- [ ] `HAPI_DB_PASSWORD` and `HAPI_TARGET_DB_PASSWORD` set
- [ ] No secrets in git (check `.env` is gitignored)

### Network and TLS
- [ ] TLS termination at reverse proxy
- [ ] `MEDANON_CORS_ORIGINS` restricted to known origins
- [ ] gPAS web UI (8080) not publicly accessible

### Logging and monitoring
- [ ] `LOG_LEVEL=INFO` (DEBUG may expose PHI)
- [ ] `MEDANON_MANIFEST_ENABLED=true` (GDPR Art. 30 accountability)
- [ ] Audit log volume mounted
- [ ] Prometheus scraping configured

### gPAS
- [ ] Domain created via web UI or `make init-domains` (not direct SQL)
- [ ] `GPAS_DOMAIN` matches exactly
- [ ] gPAS MySQL backed up before first production run
- [ ] `curl http://localhost:8080/ttp-fhir/fhir/gpas/metadata` returns 200

### Validation
- [ ] `/ready` returns `{"ready": true}`
- [ ] End-to-end: POST a sample Patient to `/process`, verify output
- [ ] `/analyse/risk` run on de-identified output before data sharing
- [ ] Appropriate config profile selected — see [policies.md](policies.md)
