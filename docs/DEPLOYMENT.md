# MedAnon — Deployment Guide

This document covers everything needed to deploy MedAnon in Docker Compose (local / single-node) and Kubernetes (Helm), including environment variables, secrets management, resource requirements, production hardening, and health verification.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Docker Compose Deployment](#2-docker-compose-deployment)
3. [Environment Variables Reference](#3-environment-variables-reference)
4. [Secrets Management](#4-secrets-management)
5. [Kubernetes (Helm) Deployment](#5-kubernetes-helm-deployment)
6. [Resource Requirements](#6-resource-requirements)
7. [Production Hardening](#7-production-hardening)
8. [Upgrading](#8-upgrading)
9. [Health Verification](#9-health-verification)
10. [Uninstalling](#10-uninstalling)

---

## 1. Prerequisites

### Docker Compose

| Requirement | Minimum | Notes |
|---|---|---|
| Docker Engine | 24.x | |
| Docker Compose plugin | v2.x | `docker compose` (not `docker-compose`) |
| RAM | 8 GB | gPAS (WildFly) needs ~2 GB; MySQL ~2 GB |
| Disk | 10 GB | Images ~4 GB; gPAS MySQL data volume |
| CPU | 2 cores | |

### Kubernetes (Helm)

| Requirement | Version |
|---|---|
| Kubernetes | ≥ 1.26 |
| Helm | ≥ 3.12 |
| Container registry | Any (GHCR, ECR, Docker Hub, etc.) |

---

## 2. Docker Compose Deployment

### Step 1 — Clone and configure

```bash
git clone <repo-url> medanon
cd medanon
cp .env.example .env
```

Edit `.env` and fill in all `REPLACE_WITH_...` placeholders (see [Environment Variables Reference](#3-environment-variables-reference)).

### Step 2 — Build images

```bash
make build
```

This builds two images:
- `medanon:latest` — the FastAPI anonymizer (from `services/anonymizer/Dockerfile`)
- `medanon-ui:latest` — the Streamlit UI (from `client/Dockerfile`)

gPAS and HAPI FHIR use official upstream images and are pulled automatically.

### Step 3 — Start the stack

```bash
make up
```

This runs `docker compose up -d`. Five containers start:

| Container | Image | Purpose |
|---|---|---|
| `medanon-ui` | `medanon-ui:latest` | Streamlit browser UI |
| `medanon` | `medanon:latest` | Anonymizer API |
| `hapi-fhir` | `hapiproject/hapi:latest` | HAPI FHIR R4 server |
| `gpas-wildfly` | `mosaicgreifswald/wildfly:38` | gPAS TTP pseudonymization |
| `gpas-mysql` | `mysql:8.0` | MySQL backend for gPAS |

Startup order is enforced via `depends_on` + healthchecks. gPAS takes the longest (~90 s for WildFly to deploy the WAR files).

### Step 4 — Create gPAS domain

```bash
make init-domains
```

Or manually via the web UI at `http://localhost:8080/gpas-web/` (log in as `admin@ths`):
- Navigate to **gPAS → Domains → New**
- Name: `TESTING` (or whatever you set as `GPAS_DOMAIN` in `.env`)
- Generator: `ReedSolomonLagrange`, Alphabet: `Symbol31`

> **Important:** Do not insert domains directly into MySQL. gPAS maintains an in-memory domain cache (`domainLocks HashMap`) that is only populated when gPAS itself creates the domain via its API.

### Step 5 — Verify

```bash
curl http://localhost:8000/health      # {"status":"ok"}
curl http://localhost:8081/fhir/metadata  # HAPI CapabilityStatement
curl http://localhost:8080/             # gPAS WildFly welcome page
open http://localhost:8501              # Streamlit UI
```

### Development Mode

Hot-reload mode mounts source code into the anonymizer container:

```bash
make dev
```

This applies `docker-compose.dev.yml` overrides:
- Anonymizer: source directory mounted live, uvicorn `--reload` enabled
- HAPI FHIR: in-memory H2 (data resets on restart)
- gPAS: WildFly management console exposed on `127.0.0.1:9990`

---

## 3. Environment Variables Reference

Copy `.env.example` to `.env`. All secrets must be replaced before any non-local deployment.

### Anonymizer

| Variable | Required | Default | Description |
|---|---|---|---|
| `MEDANON_HASH_KEY` | production | — | Hex key for HMAC-SHA3-256 hashing. Generate: `openssl rand -hex 32` |
| `MEDANON_RSA_PUBLIC_KEY` | for encrypt | `/code/keys/id_rsa.pub` | Path to RSA public key inside container |
| `MEDANON_RSA_PRIVATE_KEY` | for decrypt | `/code/keys/id_rsa` | Path to RSA private key inside container |
| `MEDANON_MAX_BODY_BYTES` | no | `10485760` | Max request body size (bytes) |
| `MEDANON_CORS_ORIGINS` | no | — | Comma-separated CORS origins |
| `MEDANON_API_KEY` | no | — | Shared API key. If set, all endpoints require `X-API-Key` header |
| `MEDANON_READY_TIMEOUT` | no | `5.0` | Readiness probe timeout (seconds) |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR` |
| `ANONYMIZER_PORT` | no | `8000` | Host port for the anonymizer |

### FHIR server

| Variable | Required | Default | Description |
|---|---|---|---|
| `FHIR_SOURCE_URL` | for fetch ops | `http://hapi-fhir:8080/fhir` | FHIR server base URL for fetch operations |
| `FHIR_SOURCE_TOKEN` | no | — | Bearer token for source FHIR server |
| `FHIR_TARGET_URL` | no | — | Target FHIR server for upload operations |
| `FHIR_TARGET_TOKEN` | no | — | Bearer token for target FHIR server |
| `HAPI_SERVER_ADDRESS` | no | `http://hapi-fhir:8080/fhir` | Public address used in HAPI Bundle links |
| `HAPI_PORT` | no | `8081` | Host port for HAPI FHIR |

### gPAS

| Variable | Required | Default | Description |
|---|---|---|---|
| `GPAS_URL` | for gPAS | — | gPAS TTP-FHIR gateway URL, e.g. `http://gpas:8080/ttp-fhir/fhir/gpas` |
| `GPAS_DOMAIN` | for gPAS | — | Pseudonymization domain, e.g. `TESTING` |
| `GPAS_OPERATION` | no | `pseudonymizeAllowCreate` | `pseudonymize`, `pseudonymizeAllowCreate`, or `dePseudonymize` |
| `GPAS_TIMEOUT_SEC` | no | `30` | HTTP timeout for gPAS calls |
| `GPAS_BASIC_USER` | for gPAS auth | — | gRAS username, e.g. `user@ths` |
| `GPAS_BASIC_PASS` | for gPAS auth | — | gRAS password. Generate: `openssl rand -base64 24` |
| `GPAS_TOKEN` | no | — | Bearer token (takes precedence over basic auth) |
| `GPAS_ADMIN_URL` | no | — | gPAS admin URL (for domain management) |
| `GPAS_CACHE_ENABLED` | no | `true` | Enable LRU pseudonym cache |
| `GPAS_RETRY_COUNT` | no | `2` | Number of retries on gPAS failure |
| `GPAS_RETRY_BACKOFF_SEC` | no | `0.2` | Initial backoff (seconds) for retry |
| `GPAS_PORT` | no | `8080` | Host port for gPAS |
| `GPAS_MYSQL_ROOT_PASSWORD` | yes | — | MySQL root password. Generate: `openssl rand -base64 24` |

### Streamlit UI

| Variable | Required | Default | Description |
|---|---|---|---|
| `UI_PORT` | no | `8501` | Host port for the Streamlit UI |

---

## 4. Secrets Management

### Generating secrets

```bash
# HMAC hash key
openssl rand -hex 32

# MySQL / gRAS passwords
openssl rand -base64 24

# RSA keypair for encrypt/decrypt action
openssl genrsa -out services/anonymizer/keys/id_rsa 4096
openssl rsa -in services/anonymizer/keys/id_rsa -pubout -out services/anonymizer/keys/id_rsa.pub
```

### What must never be committed

The `.gitignore` already excludes:
- `.env` — all secrets
- `services/anonymizer/keys/id_rsa` — RSA private key
- `output/` — de-identified data

Never commit any file containing real passwords, hash keys, or private keys.

### Rotating secrets

| Secret | Rotation effect |
|---|---|
| `MEDANON_HASH_KEY` | All existing cryptohash pseudonyms become invalid (different output). Rotate intentionally. |
| `MEDANON_RSA_PRIVATE_KEY` | Old encrypted values become unreadable. Keep the old key if you need to decrypt historical data. |
| `GPAS_BASIC_PASS` | Update both `.env` and the gRAS MySQL record: `CALL changePassword('user', 'new-pass');` |
| `GPAS_MYSQL_ROOT_PASSWORD` | Requires re-creating the MySQL volume (`docker compose down -v`) to take effect. |

---

## 5. Kubernetes (Helm) Deployment

The Helm umbrella chart at `helm/medanon/` deploys the anonymizer, HAPI FHIR, and gPAS as Kubernetes workloads. The Streamlit UI is not yet included in the Helm chart — deploy it separately or add a sub-chart.

### Step 1 — Build and push images

```bash
# Anonymizer
docker build --target prod \
  -t registry.example.com/medanon:latest \
  services/anonymizer/
docker push registry.example.com/medanon:latest

# gPAS (bundles WAR/EAR into image)
make helm-build-gpas REGISTRY=registry.example.com
docker push registry.example.com/medanon-gpas:latest
```

### Step 2 — Validate the chart (no cluster needed)

```bash
make helm-lint        # yamllint + Helm schema validation
make helm-template    # print all rendered Kubernetes YAML
```

### Step 3 — Install

```bash
make helm-install \
  REGISTRY=registry.example.com \
  GPAS_URL=http://medanon-gpas:8080/ttp-fhir/fhir/gpas \
  FHIR_SOURCE_URL=http://medanon-fhir-server:8080/fhir
```

Or directly with Helm for full control:

```bash
helm upgrade --install medanon ./helm/medanon \
  --set registry=registry.example.com \
  --set anonymizer.env.GPAS_URL=http://medanon-gpas:8080/ttp-fhir/fhir/gpas \
  --set anonymizer.env.MEDANON_HASH_KEY=<hex-key> \
  --set gpas.env.GPAS_BASIC_PASS=<password> \
  --namespace medanon \
  --create-namespace
```

### Step 4 — Verify

```bash
kubectl get pods -n medanon
kubectl get svc -n medanon

# Port-forward to test locally
kubectl port-forward svc/medanon-anonymizer 8000:8000 -n medanon
curl http://localhost:8000/health
```

### Chart structure

```
helm/
├── medanon/              Umbrella chart
│   ├── Chart.yaml
│   ├── values.yaml       Global defaults (registry, imagePullPolicy, ingress)
│   └── templates/
│       └── ingress.yaml  Optional ingress (set ingress.enabled: true)
└── charts/
    ├── anonymizer/       Anonymizer sub-chart (Deployment, Service, ConfigMap, Secret)
    ├── fhir-server/      HAPI FHIR sub-chart (Deployment, Service, ConfigMap)
    └── gpas/             gPAS sub-chart (Deployment, StatefulSet for MySQL, Services, Secrets)
```

### Kubernetes-specific notes

- **gPAS startup**: An initContainer (`busybox` + `nc -z`) waits for MySQL TCP before WildFly starts — mirrors Docker `depends_on: condition: service_healthy`.
- **gPAS readOnly filesystem**: Intentionally **not** enabled for gPAS/WildFly. WildFly writes to `standalone/log`, `standalone/tmp`, `/tmp` at runtime. Enabling `readOnlyRootFilesystem` requires per-image testing and emptyDir volume mounts.
- **Secret checksums**: Deployment annotations include `checksum/secret: sha256(secret.yaml)` — pods automatically roll when secrets change.
- **Network policies**: Each sub-chart ships a `NetworkPolicy` restricting ingress to only the expected callers.
- **Non-root containers**: Anonymizer runs as UID 1000 (`appuser`). gPAS runs as UID 1000 (`runAsNonRoot: true`).
- **MySQL persistence**: gPAS MySQL uses a `StatefulSet` with a `PersistentVolumeClaim`. Data survives pod restarts. To wipe: `kubectl delete pvc -l app.kubernetes.io/component=gpas-db -n medanon`.

---

## 6. Resource Requirements

Measured at idle after full startup. Scale up for production workloads.

| Container | CPU (idle) | CPU (limit) | RAM (idle) | RAM (limit) |
|---|---|---|---|---|
| `medanon-ui` | ~0.05 | 0.5 | ~150 MB | 512 MB |
| `medanon` | ~0.05 | 1.0 | ~300 MB | 1 GB |
| `hapi-fhir` | ~0.1 | 2.0 | ~1.2 GB | 3 GB |
| `gpas-wildfly` | ~0.1 | 2.0 | ~1.5 GB | 6 GB |
| `gpas-mysql` | ~0.1 | 1.0 | ~0.5 GB | 4 GB |
| **Total** | **~0.5** | **6.5** | **~3.7 GB** | **14.5 GB** |

Minimum host RAM for the full stack: **8 GB** (comfortable: 12 GB+).

---

## 7. Production Hardening

### Network

- All Docker Compose host port bindings are already on `127.0.0.1` (loopback only).
- For remote access, place a **reverse proxy** (nginx, Caddy, Traefik) in front and terminate TLS there.
- Expose only port 8501 (UI) and optionally 8000 (API) through the proxy. Do not expose 8080 (gPAS) or 8081 (HAPI) externally.

Example nginx snippet:

```nginx
server {
    listen 443 ssl;
    server_name medanon.example.com;

    ssl_certificate     /etc/ssl/certs/medanon.crt;
    ssl_certificate_key /etc/ssl/private/medanon.key;

    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header X-API-Key $http_x_api_key;
    }
}
```

### Authentication layers

| Layer | Mechanism | Status |
|---|---|---|
| Streamlit UI | `streamlit-authenticator` (planned) | Future |
| Anonymizer API | `MEDANON_API_KEY` → `X-API-Key` header | Configurable |
| gPAS FHIR API | gRAS basic auth | Enabled by default |
| HAPI FHIR | Spring Security (add to `application.yaml`) | Not configured |

Set `MEDANON_API_KEY` to a random 32-byte value for any non-local deployment:

```bash
export MEDANON_API_KEY=$(openssl rand -hex 16)
```

### HAPI FHIR persistence

The default H2 in-memory database **loses all data on container restart**. For production, switch to PostgreSQL:

```yaml
# services/fhir-server/config/application.yaml
spring:
  datasource:
    url: jdbc:postgresql://pg-host:5432/hapi
    username: hapi
    password: ${HAPI_DB_PASS}
    driverClassName: org.postgresql.Driver
```

Add the PostgreSQL container or external DB to `docker-compose.yml` and set `HAPI_DB_PASS` in `.env`.

### gPAS password rotation

Change the default `ttp-tools` password before any deployment that is reachable beyond localhost:

1. Update `GPAS_BASIC_PASS` in `.env`.
2. Connect to MySQL and run:
   ```sql
   USE gras;
   CALL changePassword('user', 'new-strong-password');
   CALL changePassword('admin', 'another-strong-password');
   ```
3. Restart the anonymizer to pick up the new credential:
   ```bash
   docker compose restart anonymizer
   ```

### Log level

Keep `LOG_LEVEL=INFO` in production. `DEBUG` mode logs full FHIR resource content which may expose PHI.

---

## 8. Upgrading

### Anonymizer / UI code changes

```bash
git pull
make build      # rebuild affected images
make up         # recreate containers with new images (zero-downtime on single-node if replicas > 1)
```

### gPAS

gPAS upgrades require:
1. Update `mosaicgreifswald/wildfly` tag in `docker-compose.yml`.
2. Update WAR/EAR files in `services/gpas/deployments/`.
3. Re-run `make build` and `make up`.

> Back up the `gpas-db-data` Docker volume before any gPAS upgrade — pseudonym mappings are stored there.

```bash
# Backup MySQL volume
docker run --rm \
  -v gpas-db-data:/data \
  -v $(pwd)/backup:/backup \
  busybox tar czf /backup/gpas-db-$(date +%Y%m%d).tar.gz -C /data .
```

### Helm (Kubernetes)

```bash
git pull
docker build --target prod -t registry.example.com/medanon:<new-tag> services/anonymizer/
docker push registry.example.com/medanon:<new-tag>

helm upgrade medanon ./helm/medanon \
  --set anonymizer.tag=<new-tag> \
  --namespace medanon
```

---

## 9. Health Verification

### Quick check (all services)

```bash
# Anonymizer
curl -s http://localhost:8000/health | python3 -m json.tool

# Anonymizer readiness (includes gPAS + FHIR connectivity)
curl -s http://localhost:8000/ready | python3 -m json.tool

# HAPI FHIR
curl -s http://localhost:8081/fhir/metadata | python3 -m json.tool | head -20

# gPAS
curl -s http://localhost:8080/ttp-fhir/fhir/gpas/metadata | python3 -m json.tool | head -10

# Streamlit UI
curl -s http://localhost:8501/_stcore/health
```

### Docker healthcheck status

```bash
docker compose ps      # shows health status for each container
docker inspect --format='{{.State.Health.Status}}' medanon
docker inspect --format='{{.State.Health.Status}}' hapi-fhir
docker inspect --format='{{.State.Health.Status}}' gpas-wildfly
docker inspect --format='{{.State.Health.Status}}' gpas-mysql
```

### End-to-end smoke test

```bash
# 1. De-identify a sample patient
curl -s -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Patient","id":"smoke-001","name":[{"family":"Test"}],"birthDate":"1990-01-01"}' \
  | python3 -m json.tool

# Expected: Patient returned, id replaced with pseudonym, name absent/redacted, birthDate generalized

# 2. Confirm the pseudonym was stored in gPAS
curl -s http://localhost:8080/gpas-web/   # login and check domain entries
```

---

## 10. Uninstalling

### Docker Compose — keep data

```bash
make down          # stops and removes containers; volumes are preserved
```

### Docker Compose — remove everything including data

```bash
docker compose down -v   # removes containers AND named volumes (gpas-db-data, hapi-data)
```

### Helm (Kubernetes)

```bash
helm uninstall medanon --namespace medanon
# PVCs are NOT deleted automatically — check before wiping:
kubectl get pvc -n medanon
kubectl delete pvc -n medanon --all   # only if you want to lose all data
kubectl delete namespace medanon
```
