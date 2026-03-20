# MedAnon Operations Runbook

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Starting the Stack](#2-starting-the-stack)
3. [Config Profile Selection](#3-config-profile-selection)
4. [Processing Workflows](#4-processing-workflows)
5. [Verifying Output Quality](#5-verifying-output-quality)
6. [Risk Assessment](#6-risk-assessment)
7. [Monitoring](#7-monitoring)
8. [Backup and Recovery](#8-backup-and-recovery)
9. [Secret Rotation](#9-secret-rotation)
10. [Troubleshooting](#10-troubleshooting)
11. [Make Commands Reference](#11-make-commands-reference)
12. [Security Checklist](#12-security-checklist)

---

## 1. Prerequisites

| Requirement | Minimum Version | Notes |
|---|---|---|
| Docker Engine | 24+ | Required for full stack |
| Docker Compose | v2 | `docker compose` (not `docker-compose`) |
| Python | 3.11+ | Local dev / CLI tools only |
| GNU Make | 4+ | For make targets |
| RAM | 8 GB | Comfortable: 12 GB+ for full stack |
| Disk | 10 GB | Docker images ~4 GB + gPAS MySQL data |
| CPU | 2 cores | 4+ recommended for production |

---

## 2. Starting the Stack

### Step 1: Configure

```bash
git clone <repo-url> && cd privacy-toolkit
cp .env.example .env
```

Edit `.env` and replace every `REPLACE_WITH_...` placeholder:

```bash
# Generate secrets
openssl rand -hex 32       # for MEDANON_HASH_KEY
openssl rand -base64 24    # for GPAS_BASIC_PASS, GPAS_MYSQL_ROOT_PASSWORD, KEYCLOAK_ADMIN_PASSWORD
openssl rand -base64 24    # for KEYCLOAK_CLIENT_SECRET, FHIR_PROXY/GPAS_PROXY secrets
```

### Step 2: Build and Start

```bash
make build       # build anonymizer + UI images
make up          # start all eight services
```

### Step 3: Wait for Healthy Status

gPAS (WildFly) takes up to 90 seconds to deploy. Monitor:

```bash
docker compose ps     # all should show "healthy"
```

### Step 4: Verify

```bash
# Anonymizer
curl -s http://localhost:8000/health | python3 -m json.tool
# {"status": "ok"}

curl -s http://localhost:8000/ready | python3 -m json.tool
# {"ready": true}

# HAPI FHIR (through proxy)
curl -s http://localhost:4180/fhir/metadata | head -5

# gPAS
curl -s http://localhost:8082/ttp-fhir/fhir/gpas/metadata | head -5

# Keycloak
curl -s http://localhost:8180/health/ready

# Streamlit UI
curl -s http://localhost:8501/_stcore/health
```

### Step 5: Initialize gPAS Domain

```bash
make init-domains
```

Or manually at `http://localhost:8082/gpas-web/` (login: `admin@ths`).

### Development Mode

Hot-reload with source mounted into the container:

```bash
make dev
```

This applies `docker-compose.dev.yml` overrides:
- Anonymizer: source directory mounted live, uvicorn `--reload`
- HAPI FHIR: in-memory H2 (data resets on restart)
- gPAS: management console exposed on `127.0.0.1:9990`

---

## 3. Config Profile Selection

Choose the profile that matches your use case:

| Use Case | Config File | ID Strategy | Date Strategy |
|---|---|---|---|
| Local dev / testing | `config.yaml` | cryptohash (SHA3-256) | regex scrubbing |
| Production (reversible pseudonyms) | `config_gpas.yaml` | gPAS pseudonymize | generalize + NLP |
| EU patient data (GDPR) | `config_gdpr_eu.yaml` | HMAC pseudonymization | year-only |
| US patient data (HIPAA) | `config_hipaa_safe_harbor.yaml` | cryptohash | year-only, 18 PHI categories |
| Research under IRB | `config_research_pseudonymous.yaml` | cryptohash (longitudinal) | year-month |

The active config is auto-selected:
- `GPAS_URL` set -> `config_gpas.yaml`
- `GPAS_URL` not set -> `config.yaml`

Override in CLI: `--config config/<profile>.yaml`

---

## 4. Processing Workflows

### Option A: Streamlit UI (Recommended for Interactive Use)

1. Open `http://localhost:8501`
2. Log in with Keycloak credentials (if configured)
3. Navigate to the appropriate page:

| Task | Page |
|---|---|
| Search and de-identify a patient | Patient Browser |
| Search by diagnosis | Condition Browser |
| Paste and de-identify a single resource | Process Resource |
| Upload and process a file | Batch Processing |
| Measure risk of de-identified data | Risk Assessment |
| Generate synthetic patients | Synthetic Data |
| Check service health | Status Dashboard |

### Option B: REST API (Scripted / Automated)

```bash
# Single resource
curl -s -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <jwt>" \
  -d '{"resourceType":"Patient","id":"p1","name":[{"family":"Smith"}],"birthDate":"1985-03-12"}'

# NDJSON batch (streaming)
curl -s -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/x-ndjson" \
  -H "X-API-Key: <key>" \
  --data-binary @input.ndjson -o output.ndjson

# JSON Bundle
curl -s -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/json" \
  --data-binary @bundle.json -o output.ndjson

# Fetch from FHIR server, de-identify, stream back
curl -s -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{"server_url":"http://localhost:4180/fhir","resource_types":["Patient","Observation"]}'

# Risk assessment
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @output.ndjson | python3 -m json.tool
```

### Option C: CLI (Bulk Processing)

```bash
cd services/anonymizer

# Process a local file
python3 -m cli.main process input.ndjson output.ndjson --config config/config_hipaa_safe_harbor.yaml

# Fetch from server and de-identify
python3 -m cli.main fetch --server http://localhost:4180/fhir \
  --resource-type Patient,Observation --output output/all.ndjson \
  --config config/config_gpas.yaml

# Full batch pipeline
make batch
```

---

## 5. Verifying Output Quality

After de-identification, run the analytics tool to compare input vs. output:

```bash
cd services/anonymizer

python3 tools/analyze_results.py \
  --input  tests/data/TestBase/Patient.000.ndjson \
  --output /tmp/Patient.deid.ndjson \
  --operator "Your Name" \
  --config config/config_hipaa_safe_harbor.yaml \
  --report-json /tmp/evidence.json \
  --report-md   /tmp/evidence.md

cat /tmp/evidence.md
```

**What to check:**
- `ids_changed` should equal `comparable_resource_ids` (all IDs transformed)
- `reference_changes` > 0 if `rewrite_references: true` is set
- `token_counts` should list NLP entity tokens (PERSON, GPE, etc.) if NLP rules are active
- `config_sha256` provides an audit-proof fingerprint of the exact config used

---

## 6. Risk Assessment

Always run a risk assessment after de-identification:

```bash
# Patient resources only (k-anonymity)
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @output.ndjson | python3 -m json.tool

# Patient + Condition resources (k-anonymity + l-diversity)
cat Patient.deid.ndjson Condition.deid.ndjson | \
  curl -s -X POST http://localhost:8000/analyse/risk \
    -H "Content-Type: application/x-ndjson" \
    --data-binary @- | python3 -m json.tool
```

### Interpreting Results

| Risk Level | min_k | Action |
|---|---|---|
| `low` | >= 5 | No action required. Meets basic k-anonymity. |
| `medium` | 3 or 4 | Consider broader date generalization or additional suppression. |
| `high` | 2 | Suppress records in pair groups or apply stronger generalization. |
| `critical` | 1 | Unique records exist. Do not release data without remediation. |

### Remediation Steps

1. **Increase generalization**: Switch from `date_year_month` to `date_year`, or use broader zip prefixes.
2. **Suppress small groups**: Remove patients who form singleton or pair groups.
3. **Combine datasets**: Merge with additional cohorts to increase group sizes.
4. **Use synthetic data**: Generate synthetic patients via `/generate/synthetic` as a replacement.

---

## 7. Monitoring

### Prometheus Metrics

All services expose metrics for Prometheus scraping:

| Service | Metrics Endpoint | Annotations |
|---|---|---|
| Anonymizer | `http://anonymizer:8000/metrics` | Request count, latency, gPAS/FHIR call stats |
| HAPI FHIR | `http://fhir-server:8080/actuator/prometheus` | JVM, HTTP, DB pool |
| gPAS | `http://gpas:8080/metrics` | WildFly metrics |

Helm charts include Prometheus scrape annotations on all pods.

### Health Checks

```bash
# Quick health check for all services
docker compose ps

# Detailed anonymizer readiness
curl -s http://localhost:8000/ready | python3 -m json.tool

# Individual container health
docker inspect --format='{{.State.Health.Status}}' anonymizer
docker inspect --format='{{.State.Health.Status}}' fhir-server
docker inspect --format='{{.State.Health.Status}}' gpas
docker inspect --format='{{.State.Health.Status}}' gpas-db
docker inspect --format='{{.State.Health.Status}}' keycloak
```

### Audit Log

Structured JSON audit log at `/output/audit.log` (inside the anonymizer container):

```bash
docker compose exec anonymizer tail -f /output/audit.log
```

Each line records: timestamp, HTTP method, path, status code, request ID, authenticated subject, auth method. PHI is never logged.

### Container Logs

```bash
make logs                           # tail all container logs
docker compose logs anonymizer      # single service
docker compose logs gpas --tail 50  # last 50 lines
```

---

## 8. Backup and Recovery

### gPAS MySQL (Pseudonym Mappings)

**This is the most critical data.** If lost, pseudonym-to-original mappings are unrecoverable.

```bash
# Backup
docker run --rm \
  -v gpas-db-data:/data \
  -v $(pwd)/backup:/backup \
  busybox tar czf /backup/gpas-db-$(date +%Y%m%d).tar.gz -C /data .

# Restore
docker compose down
docker run --rm \
  -v gpas-db-data:/data \
  -v $(pwd)/backup:/backup \
  busybox sh -c "rm -rf /data/* && tar xzf /backup/gpas-db-YYYYMMDD.tar.gz -C /data"
docker compose up -d
```

### HAPI FHIR Data

With the default H2 in-memory database, data is lost on every container restart. For persistence:

1. Switch to PostgreSQL (see DEPLOYMENT.md)
2. Back up the PostgreSQL database using standard `pg_dump` tools

### Keycloak Data

```bash
docker run --rm \
  -v keycloak-data:/data \
  -v $(pwd)/backup:/backup \
  busybox tar czf /backup/keycloak-$(date +%Y%m%d).tar.gz -C /data .
```

### Config and Keys

```bash
# Back up the config directory
cp -r services/anonymizer/config/ backup/config-$(date +%Y%m%d)/

# Back up RSA keys (if used)
cp services/anonymizer/keys/id_rsa* backup/keys-$(date +%Y%m%d)/
```

---

## 9. Secret Rotation

| Secret | How to Rotate | Impact |
|---|---|---|
| `MEDANON_HASH_KEY` | Update in `.env`, restart anonymizer | All existing cryptohash pseudonyms become invalid. Rotate intentionally. |
| `MEDANON_RSA_PRIVATE_KEY` | Generate new keypair, update paths in `.env` | Old encrypted values become unreadable. Keep old key for historical data. |
| `GPAS_BASIC_PASS` | Update in `.env` + run SQL `CALL changePassword('user','new-pass');` in gRAS, restart anonymizer | Existing gPAS sessions invalidated. |
| `GPAS_MYSQL_ROOT_PASSWORD` | Requires `docker compose down -v` to recreate MySQL volume | Destroys all pseudonym mappings. Back up first. |
| `KEYCLOAK_ADMIN_PASSWORD` | Update in `.env`, restart keycloak | Admin console password changes. |
| `KEYCLOAK_CLIENT_SECRET` | Update in Keycloak admin console + `.env`, restart anonymizer | Service account tokens invalidated. |
| `MEDANON_API_KEY` | Update in `.env`, restart anonymizer | All existing API key users must update their key. |

---

## 10. Troubleshooting

### gPAS Connection Failure

**Symptom:** `/ready` returns `{"ready": false}`, requests with `gpas_pseudonymize` return 500.

1. Check container health: `docker compose ps gpas`
2. Verify env vars: `GPAS_URL`, `GPAS_BASIC_USER`, `GPAS_BASIC_PASS`
3. Test connectivity: `curl -v http://localhost:8082/ttp-fhir/fhir/gpas/metadata`
4. Check domain exists: log into gPAS web UI and verify
5. Review logs: `docker compose logs gpas`

### gPAS Circuit Breaker Open

**Symptom:** "gPAS circuit breaker is OPEN" error.

1. gPAS has failed 5+ times in 60 seconds (default thresholds)
2. Wait 30 seconds for automatic recovery probe
3. Or fix the underlying gPAS issue and restart: `docker compose restart gpas`
4. Adjust: `GPAS_CB_FAILURE_THRESHOLD`, `GPAS_CB_RECOVERY_TIMEOUT_SEC`, `GPAS_CB_WINDOW_SEC`

### gPAS "Unknown Domain"

The domain does not exist. Run `make init-domains` or create it manually in the gPAS web UI.

### gPAS Container Keeps Restarting

MySQL is still initializing (first boot creates schemas):

```bash
docker compose ps gpas-db                    # wait for "healthy"
docker compose logs gpas-db | tail -20       # check init progress
docker compose restart gpas                  # restart once MySQL is ready
```

### spaCy Model Missing

**Symptom:** `nlp_detect` errors, `/ready` shows NLP failure.

```bash
docker compose exec anonymizer python3 -m spacy download en_core_web_lg
# Or: make build   (model is included in the prod image)
```

### Port Conflicts

**Symptom:** `docker compose up` fails with "address already in use".

```bash
lsof -i :8000    # find conflicting process
```

Edit `.env` to change port mappings (`ANONYMIZER_PORT`, `UI_PORT`, `KEYCLOAK_PORT`, etc.).

### Body Size Limit (413 Error)

Split large files or increase the limit:

```bash
# .env
MEDANON_MAX_BODY_BYTES=20971520    # 20 MB
```

### Keycloak Login Failures

1. Verify Keycloak is running: `curl http://localhost:8180/health/ready`
2. Check realm exists: `http://localhost:8180/admin/` -> Realms -> `medanon`
3. Verify client secrets match between `.env` and Keycloak admin console
4. For proxy issues, check: `docker compose logs fhir-proxy` / `docker compose logs gpas-proxy`

### Out of Memory (OOM) Kills

Check which container was killed:

```bash
docker compose ps                                   # look for "Exited" status
docker inspect --format='{{.State.OOMKilled}}' <container>
```

Current memory limits:

| Service | Limit |
|---|---|
| Anonymizer | 2 GB |
| HAPI FHIR | 3 GB |
| gPAS | 6 GB (JVM: -Xmx4G) |
| MySQL | 4 GB (InnoDB: 512 MB) |
| Keycloak | 1 GB (JVM: -Xmx512m) |
| UI | 512 MB |
| Proxies | 128 MB each |

If a service is consistently OOM-killed, increase its memory limit in `docker-compose.yml`.

---

## 11. Make Commands Reference

| Command | Description |
|---|---|
| `make up` | Start full Docker stack (8 services) |
| `make dev` | Start with hot-reload (source mounted into container) |
| `make down` | Stop and remove containers (volumes preserved) |
| `make build` | Rebuild Docker images (anonymizer + UI) |
| `make logs` | Tail all container logs |
| `make setup` | Create Python venv, install deps, download spaCy model |
| `make lint` | Run ruff linter on anonymizer source |
| `make format` | Run ruff formatter on anonymizer source |
| `make batch` | Run batch_process.sh + generate analytics report |
| `make init-domains` | Create gPAS pseudonymization domain |

### Testing

```bash
cd services/anonymizer

# Full local test suite (no Docker needed)
python3 -m pytest tests/test_api.py tests/test_auth.py tests/test_io_formats.py tests/test_config_profiles.py -q

# Full suite with coverage
python3 -m pytest tests/ --cov=src --cov-report=term-missing

# Single test
python3 -m pytest tests/test_api.py::test_health -q
```

---

## 12. Security Checklist

Before any non-local deployment:

- [ ] Set a strong `MEDANON_HASH_KEY` (`openssl rand -hex 32`)
- [ ] Set `MEDANON_API_KEY` or configure Keycloak (`KEYCLOAK_URL`)
- [ ] Set strong passwords for `GPAS_BASIC_PASS`, `GPAS_MYSQL_ROOT_PASSWORD`, `KEYCLOAK_ADMIN_PASSWORD`
- [ ] Replace all `KEYCLOAK_CLIENT_SECRET` and proxy cookie secrets
- [ ] Change default Keycloak user passwords (admin/analyst/viewer)
- [ ] Enable TLS termination in front of the stack (nginx / Caddy / Traefik)
- [ ] Restrict network access: only expose ports 8501 (UI) and 8000 (API) through the proxy
- [ ] Set `LOG_LEVEL=INFO` (DEBUG logs may expose PHI)
- [ ] Enable `MEDANON_MANIFEST_ENABLED=true` for GDPR accountability
- [ ] Generate RSA keys if using encrypt/decrypt actions
- [ ] Run risk assessment on every batch output before sharing data
- [ ] Archive the evidence report alongside every de-identified dataset
- [ ] Review Helm chart (`helm/`) security settings for Kubernetes deployments
- [ ] Set up Prometheus monitoring for all services
- [ ] Configure audit log rotation (`MEDANON_AUDIT_LOG_MAX_BYTES`, `MEDANON_AUDIT_LOG_BACKUP_COUNT`)
