# MedAnon — Deployment Guide

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Docker Engine | 24.x | `docker --version` |
| Docker Compose plugin | v2.x | `docker compose version` (not `docker-compose`) |
| RAM | 8 GB | 12 GB+ comfortable; 6 GB for anonymizer alone at peak bulk export |
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

### 2. Build images

```bash
make build
```

Builds `medanon:latest` (FastAPI anonymizer) and `medanon-ui:latest` (React/nginx). gPAS and HAPI FHIR use upstream images pulled automatically.

The anonymizer Dockerfile uses the **repo root** as build context (required to copy `packages/medanon-core/` before `services/anonymizer/`). Four build stages:
- `base` — Python 3.12 + deps + spaCy model
- `prod` — production target (used by default)
- `dev` — adds uvicorn `--reload`
- `sdv` — adds SDV synthetic data engine (~2 GB)

### 3. Start the stack

```bash
make up
```

Starts all 8 services in dependency order. **gPAS (WildFly) takes ~90 seconds on first boot** — it deploys the TTP-FHIR WAR file and initializes the PostgreSQL schema.

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

## Opt-in service profiles

```bash
docker compose --profile analytics up   # analytics microservice (risk + synthetic)
docker compose --profile nlp up         # NLP microservice (Presidio + spaCy, ~800 MB image)
docker compose --profile ha up          # gPAS PostgreSQL read replica (HA setup)
```

When `NLP_SERVICE_URL` is set, the anonymizer delegates `nlp_detect` actions to the NLP microservice instead of running Presidio in-process. This saves ~800 MB of anonymizer RAM.

When `ANALYTICS_SERVICE_URL` is set, `/analyse/risk` and `/generate/synthetic` proxy to the analytics service.

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

These are set in `docker-compose.yml` and tuned based on observed usage during bulk export (~20,000 resources):

| Service | RAM limit | CPU limit | Notes |
|---|---|---|---|
| `anonymizer` | 6 GB | 2.0 | Peak usage ~5.7 GB during large bulk export |
| `fhir-server` | 3 GB | 2.0 | JVM heap |
| `gpas` | 6 GB | 2.0 | WildFly JVM, -Xmx4G |
| `gpas-db` | 4 GB | 1.0 | PostgreSQL shared_buffers 512 MB |
| `ui` | 128 MB | 0.5 | nginx is lightweight |

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
