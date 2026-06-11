# MedAnon — Deployment Guide

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Docker Engine | 24.x | `docker --version` |
| Docker Compose plugin | v2.x | `docker compose version` (not `docker-compose`) |
| RAM | 8 GB | 12 GB+ comfortable; anonymizer 3 GB + NLP 1.5 GB + gPAS 2.5 GB + HAPI 3 GB + buffers |
| Disk | 10 GB | Images ~4 GB + PostgreSQL data volumes |
| CPU | 2 cores | 4+ recommended for concurrent jobs |

---

## Docker Compose — Initial Setup

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and replace every `REPLACE_WITH_...` value. Generate secrets:

```bash
openssl rand -hex 32        # → MEDANON_HASH_KEY (HMAC key for cryptohash action)
openssl rand -base64 24     # → GPAS_BASIC_PASS, GPAS_DB_PASSWORD, HAPI_DB_PASSWORD, MEDANON_REDIS_PASSWORD
```

**Key variables to set for production:**

| Variable | Why it matters |
|---|---|
| `MEDANON_HASH_KEY` | Without this, cryptohash uses plain SHA3-256 — reversible via rainbow tables. Never skip in production. |
| `GPAS_BASIC_PASS` | Default password in gPAS SQL seed file. Must be changed. |
| `GPAS_DB_PASSWORD` | PostgreSQL password for gPAS database. |
| `MEDANON_REDIS_PASSWORD` | Redis authentication password. Required in production. |
| `HAPI_DB_PASSWORD` | PostgreSQL password for source FHIR server. |
| `HAPI_TARGET_DB_PASSWORD` | PostgreSQL password for target (de-identified) FHIR server. |
| `EXTERNAL_HOST` | IP or hostname browsers use to reach this server. Used in CORS origins and HAPI server address. |
| `MEDANON_API_KEY` | Leave blank for dev (open mode). Set for any non-local deployment. |
| `MEDANON_REQUIRE_DURABLE_STORE` | Set `true` in production. Forces the API to refuse the SQLite job-store fallback when neither Redis nor PostgreSQL is reachable. The dedicated `worker` container always refuses SQLite regardless. |
| `MEDANON_REQUIRE_REDIS_AOF` | Set `true` in production. Refuses to start when Redis AOF persistence is disabled (RDB snapshots alone may lose up to 60 s of queued jobs). |
| `MEDANON_RATE_JOBS_SUBMIT` | slowapi rate limit applied to all `POST /v1/jobs/*` submission endpoints. Default `30/minute` \u2014 raise/lower per tenant size. |

### 2. Build images

```bash
make build
```

Builds `medanon:latest` (FastAPI anonymizer) and `medanon-ui:latest` (React/nginx). gPAS and HAPI FHIR use upstream images pulled automatically.

The anonymizer Dockerfile uses `services/anonymizer/` as build context. Four build stages:
- `base` — Python 3.12 + deps (no spaCy; NLP runs as the NLP microservice)
- `prod` — production target (used by default)
- `dev` — adds uvicorn `--reload`
- `sdv` — adds SDV synthetic data engine (~2 GB)

### 3. Start the stack

```bash
make up
```

Starts all 14 always-on services in dependency order. **gPAS (WildFly) takes ~90 seconds on first boot** — it deploys the TTP-FHIR WAR file and initializes the PostgreSQL schema.

```bash
docker compose ps    # wait until all show "healthy"
```

### 4. Initialize gPAS domain

```bash
make init-domains
```

Or manually via `http://localhost:8080/gpas-web/` (login: `admin@ths`):
1. Domains → New Domain
2. Name: value of `GPAS_DOMAIN` in `.env` (default: `TESTING`)
3. Generator: `ReedSolomonLagrange`, Alphabet: `Symbol31`
4. Save

**Critical:** Never create domains by inserting directly into PostgreSQL. gPAS maintains a `domainLocks HashMap` in JVM memory that is only populated when domains are created through its own REST API. Direct SQL inserts appear to work but cause "domain not found" errors at runtime when pseudonymization is attempted.

### 5. Verify

```bash
curl http://localhost:8000/health     # {"status":"ok"}
curl http://localhost:8000/ready      # {"ready": true, ...}
curl http://localhost:8082/fhir/metadata | head -3   # target server (de-identified)
open http://localhost:8501
```

`/health` is a fast liveness check. `/ready` probes FHIR and gPAS connectivity — if it returns `false`, check `docker compose logs` for the failing service. Note: source FHIR server has no host port published (security isolation) — access it only through the anonymizer proxy endpoints.

---

## Development mode

```bash
make dev
```

Applies `docker-compose.dev.yml` overrides:
- Anonymizer: source mounted at `/code/src`, uvicorn `--reload` active
- HAPI FHIR: uses in-memory H2 (data resets on restart — intentional for dev)
- gPAS: management console exposed on `127.0.0.1:9990`

---

## Environments

Three deployment tiers are supported. Choose based on the stage of your workflow.

### Development (hot-reload, ephemeral data)

```bash
make dev
```

Applies `docker-compose.dev.yml` overrides on top of the base stack:

| Difference from production | Why |
|---|---|
| Anonymizer source mounted at `/code/src`, uvicorn `--reload` | Code changes apply instantly without rebuild |
| HAPI FHIR uses in-memory H2 database | Data resets on restart — intentional; no migration needed during iteration |
| gPAS management console exposed on `127.0.0.1:9990` | WildFly admin console accessible locally |
| `MEDANON_API_KEY` typically left blank | Open mode — all callers are granted admin. Never use in non-local environments. |
| NLP and analytics start alongside anonymizer | Same as production — both are always-on |

**Not suitable for:** real patient data, performance testing, any multi-user access.

### Staging (full Docker Compose, persistent volumes)

Staging uses the production image with real persistent volumes, but on an isolated machine or namespace with test data only. No `dev` overrides.

```bash
# 1. Use production image (not dev target)
make build

# 2. Configure environment — use generated secrets, not placeholders
cp .env.example .env
# Edit .env: set MEDANON_API_KEY, MEDANON_HASH_KEY, all DB passwords

# 3. Start full stack
make up

# 4. Initialize gPAS domain
make init-domains

# 5. Optionally enable opt-in profiles
docker compose --profile s3 up -d    # MinIO for job result storage
docker compose --profile ai up -d    # Ollama if testing AI agents
```

**Staging-specific settings to verify before release:**

```bash
MEDANON_API_KEY=<non-empty-staging-key>
MEDANON_MANIFEST_ENABLED=true
MEDANON_SCORING_ENABLED=true
LOG_LEVEL=INFO                       # Never DEBUG with real data
MEDANON_RESULT_TTL_SEC=86400         # Clean up job results after 24 hours
```

Run the full verification suite:

```bash
make verify        # Smoke tests all services
make test          # Full pytest suite (from services/anonymizer/)
```

**Differences from production:**
- No TLS termination (staging typically behind a VPN or internal network)
- Single gPAS replica (no `--profile ha`)
- `EXTERNAL_HOST` set to the staging server's IP or hostname

### Production (Kubernetes / Helm)

Production runs on Kubernetes using the Helm umbrella chart. Key differences from Docker Compose:

| Concern | Docker Compose (staging) | Kubernetes/Helm (production) |
|---|---|---|
| Scaling | `--scale nlp=N --scale gpas=N` | HPA on worker (1–5 replicas); manual scale for others |
| TLS | External reverse proxy | Ingress controller (nginx-ingress or cloud LB) with cert-manager |
| Secrets | `.env` file | Kubernetes Secrets (or external secret manager: Vault, AWS Secrets Manager) |
| Storage | Docker named volumes | PersistentVolumeClaims (retain policy) |
| Log aggregation | Docker log driver | K8s log driver → ELK/Loki/Splunk |
| Health checks | Docker HEALTHCHECK | Readiness and liveness probes in Deployment spec |
| gPAS HA | `--profile ha` (single replica) | StatefulSet + optional read replica PVC |

**Minimum production checklist:**

```bash
# 1. Build and push production images
docker build --target prod \
  -t registry.example.com/medanon:$(git describe --tags --abbrev=0) \
  services/anonymizer/
docker push registry.example.com/medanon:<tag>

docker build -t registry.example.com/medanon-ui:<tag> client/
docker push registry.example.com/medanon-ui:<tag>

# 2. Validate chart (no cluster needed)
make helm-lint
make helm-template

# 3. Install
helm upgrade --install medanon ./helm/medanon \
  --set global.registry=registry.example.com \
  --set anonymizer.image.tag=<tag> \
  --set anonymizer.secrets.MEDANON_HASH_KEY=<hex-key> \
  --set anonymizer.secrets.MEDANON_API_KEY=<key> \
  --set anonymizer.env.MEDANON_MANIFEST_ENABLED=true \
  --set anonymizer.env.MEDANON_SCORING_ENABLED=true \
  --set anonymizer.env.MEDANON_RESULT_TTL_SEC=86400 \
  --set gpas.secrets.WF_ADMIN_PASS=<password> \
  --set gpas.db.secrets.rootPassword=<password> \
  --namespace medanon --create-namespace

# 4. Initialize gPAS domain (first install only)
kubectl exec -n medanon deploy/medanon-gpas -- \
  curl -s -X POST http://localhost:8080/gpas/gpasService \
  ... # see make init-domains for full SOAP call
```

**K3s (single-node / edge):**

```bash
# Import images into K3s containerd (separate from Docker daemon)
docker save medanon:<tag> | sudo k3s ctr images import -
docker save medanon-ui:<tag> | sudo k3s ctr images import -

# Use K3s-specific overrides (Traefik ingress + local-path storage)
helm upgrade --install medanon ./helm/medanon \
  -f helm/k3s-values.yaml \
  --namespace medanon --create-namespace
```

Note: K3s ships with Flannel which does not enforce `NetworkPolicy`. Use Cilium for regulated environments.

---

## Opt-in service profiles

```bash
docker compose --profile ha up    # gPAS PostgreSQL read replica (HA setup)
docker compose --profile s3 up    # MinIO S3 object storage for job results
docker compose --profile ai up    # Ollama local LLM for AI agent endpoints
```

The NLP and analytics microservices are now always-on — they start with the main `make up` command. `NLP_SERVICE_URL` is hardcoded to `http://nlp-lb:8200` in docker-compose.yml (the `nlp-lb` host name is a Traefik gateway alias). Override only to point at an external NLP deployment.

When `ANALYTICS_SERVICE_URL` is set, `/analyse/risk` and `/generate/synthetic` proxy to the analytics service (default: `http://analytics:8100`).

### NLP L2 Redis cache (cold-start mitigation)

The NLP service maintains an in-process LRU cache (L1, ~20k entries) plus an **optional Redis L2 cache** (key prefix `medanon:nlp:detect:`) shared across NLP replicas. The L2 cache survives container restarts and eliminates the cold-cache penalty observed on deploys (~4× slowdown on the first bulk export until L1 warms).

| Variable | Default (compose) | Purpose |
|----------|-------------------|---------|
| `NLP_REDIS_URL` | `redis://:${MEDANON_REDIS_PASSWORD}@redis:6379/2` | Connection URL — DB 2 isolates NLP from anonymizer L2 (DB 0). |
| `NLP_REDIS_TTL_SEC` | `604800` (7 days) | Entry TTL. |
| `NLP_REDIS_KEY_PREFIX` | `medanon:nlp:detect:` | Key namespace override. |

Leave `NLP_REDIS_URL` empty to disable L2 and run NLP with L1 only. Redis errors fail-soft: cache misses fall through to the compute path, never block detection.

---

## Secrets

### What to rotate and the impact

| Secret | How to rotate | Impact |
|---|---|---|
| `MEDANON_HASH_KEY` | Update `.env`, restart anonymizer | All existing cryptohash pseudonyms change — old de-identified data cannot be re-linked to new output |
| RSA private key | Generate new keypair, update `.env` paths | Old encrypted values become unreadable; keep the old key to decrypt historical data |
| `GPAS_BASIC_PASS` | Update `.env` + run SQL `CALL changePassword('user@ths','new-pass');` in gRAS, restart anonymizer | Existing gPAS sessions invalidated |
| `GPAS_DB_PASSWORD` | Requires `docker compose down -v` to recreate PostgreSQL volume | **Destroys all pseudonym mappings** — back up first |
| `MEDANON_API_KEY` | Update `.env`, restart anonymizer | All API clients must update their key |

### Files never to commit

`.gitignore` excludes: `.env`, `services/anonymizer/keys/id_rsa`, `output/`. Verify with `git status` before every commit.

---

## RSA key generation (for encrypt/decrypt action)

```bash
openssl genrsa -out services/anonymizer/keys/id_rsa 4096
openssl rsa -in services/anonymizer/keys/id_rsa -pubout \
  -out services/anonymizer/keys/id_rsa.pub
```

Set paths in `.env`:
```
MEDANON_RSA_PUBLIC_KEY=/code/keys/id_rsa.pub
MEDANON_RSA_PRIVATE_KEY=/code/keys/id_rsa
```

---

## Resource limits

Tuned based on observed peak usage during bulk export (~20,000 resources):

| Service | RAM limit | CPU limit | Notes |
|---|---|---|---|
| `anonymizer` | 3 GB | 2.0 | Reduced from 6 GB; NLP no longer runs in-process |
| `worker` | 2 GB | 1.0 | Dedicated job worker — one job at a time |
| `fhir-server` | 3 GB | 2.0 | JVM heap |
| `gpas` | 2.5 GB | 1.0 | WildFly JVM: Xms128M Xmx1536M, G1GC |
| `gpas-db` | 2 GB | 1.0 | PostgreSQL shared_buffers 512 MB |
| `app-db` | 1 GB | 0.5 | Jobs, configs, staging |
| `redis` | 512 MB | 0.5 | Cache + job queue |
| `ui` | 128 MB | 0.5 | nginx is lightweight |

Peak system memory: ~13.4 GB on a 16 GB host during large bulk export with NLP and analytics active.

---

## Production hardening

### TLS

Docker Compose binds ports to the host interface configured via `EXTERNAL_HOST`. Place a reverse proxy (nginx, Caddy, Traefik) in front for TLS:

```nginx
server {
    listen 443 ssl;
    server_name medanon.example.com;
    ssl_certificate /etc/ssl/certs/medanon.crt;
    ssl_certificate_key /etc/ssl/private/medanon.key;

    location / { proxy_pass http://127.0.0.1:8501; }
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header X-Request-ID $request_id;
    }
}
```

Never expose port 8080 (gPAS web UI) externally.

### HAPI FHIR persistence

Default H2 database loses all data on container restart. Switch to PostgreSQL (already wired in via `hapi-postgres` and `hapi-target-postgres` containers) — set `HAPI_SERVER_ADDRESS` correctly and configure `helm/charts/fhir-server/files/application.yaml` if needed.

### Audit and compliance

```bash
MEDANON_MANIFEST_ENABLED=true    # GDPR Art. 30: tag each resource with applied rules
MEDANON_AUDIT_LOG_FILE=/output/audit.log  # structured JSON audit log (no PHI logged)
LOG_LEVEL=INFO                   # DEBUG may log resource content containing PHI
```

---

## Kubernetes (Helm)

### Build and push

```bash
docker build --target prod -t registry.example.com/medanon:1.0.0 services/anonymizer/
docker push registry.example.com/medanon:1.0.0

docker build -t registry.example.com/medanon-ui:1.0.0 client/
docker push registry.example.com/medanon-ui:1.0.0
```

### Validate and install

```bash
make helm-lint        # yamllint + Helm schema validation (no cluster needed)
make helm-template    # dry-run rendered YAML

helm upgrade --install medanon ./helm/medanon \
  --set global.registry=registry.example.com \
  --set anonymizer.secrets.MEDANON_HASH_KEY=<hex-key> \
  --set gpas.secrets.WF_ADMIN_PASS=<password> \
  --set gpas.db.secrets.rootPassword=<password> \
  --namespace medanon --create-namespace
```

### Chart structure

```
helm/
├── medanon/           Umbrella chart (5 sub-charts)
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       └── ingress.yaml
└── charts/
    ├── anonymizer/    Deployment + Service + ConfigMap + Secret
    ├── fhir-server/   Deployment + Service + ConfigMap
    ├── gpas/          Deployment + StatefulSet (PostgreSQL) + Services + Secrets
    ├── ui/            Deployment + Service + ConfigMap (nginx) + NetworkPolicy
    └── analytics/     Optional (condition: analytics.enabled)
```

### K3s (single-node / edge)

Use `helm/k3s-values.yaml` — configures Traefik ingress and `local-path` storage class. K3s has its own containerd image store separate from Docker:

```bash
docker save medanon:latest | sudo k3s ctr images import -
docker save medanon-ui:latest | sudo k3s ctr images import -
```

K3s uses Flannel by default, which does not enforce `NetworkPolicy`. For production or regulated environments use Cilium instead.

---

## Upgrading

```bash
git pull
make build
make up    # recreates containers with new images; volumes preserved
```

For Helm:
```bash
docker build --target prod -t registry.example.com/medanon:<new-tag> services/anonymizer/
docker push registry.example.com/medanon:<new-tag>
helm upgrade medanon ./helm/medanon --set anonymizer.tag=<new-tag> -n medanon
```

---

## Uninstall

```bash
make down                      # stop, keep volumes
docker compose down -v         # stop + delete all volumes (loses gPAS pseudonym mappings + FHIR data)

helm uninstall medanon -n medanon
kubectl delete pvc -n medanon --all   # Helm does NOT delete PVCs automatically
kubectl delete namespace medanon
```
