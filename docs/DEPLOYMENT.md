# MedAnon Deployment Guide

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
| Docker Engine | 24.x | `docker --version` |
| Docker Compose plugin | v2.x | `docker compose version` (not `docker-compose`) |
| RAM | 8 GB | Comfortable: 12 GB+ |
| Disk | 10 GB | Images ~4 GB + MySQL data volume |
| CPU | 2 cores | 4+ recommended for production |

### Kubernetes (Helm)

| Requirement | Minimum |
|---|---|
| Kubernetes | 1.26+ |
| Helm | 3.12+ |
| Container registry | Any (GHCR, ECR, Docker Hub) |
| Cluster RAM | 12 GB allocatable |

---

## 2. Docker Compose Deployment

### Step 1: Clone and Configure

```bash
git clone <repo-url> medanon
cd medanon
cp .env.example .env
```

Edit `.env` and replace every `REPLACE_WITH_...` placeholder. See [Secrets Management](#4-secrets-management) for generating values.

### Step 2: Build Images

```bash
make build
```

Builds two local images:
- `medanon:latest` — FastAPI anonymizer (from `services/anonymizer/Dockerfile`)
- `medanon-ui:latest` — Streamlit UI (from `client/Dockerfile`)

gPAS, HAPI FHIR, Keycloak, and oauth2-proxy use upstream images pulled automatically.

### Step 3: Start the Stack

```bash
make up
```

Eight containers start in dependency order:

| # | Container | Image | Host Port | Startup Time |
|---|---|---|---|---|
| 1 | `gpas-db` | `mysql:8.0` | (internal) | ~15 s |
| 2 | `keycloak` | `quay.io/keycloak/keycloak:24.0.5` | 8180 | ~30 s |
| 3 | `fhir-server` | `hapiproject/hapi:v7.6.0` | (internal) | ~30 s |
| 4 | `gpas` | `mosaicgreifswald/wildfly:38` | (internal) | ~90 s |
| 5 | `fhir-proxy` | `oauth2-proxy:v7.6.0` | 4180 | ~5 s |
| 6 | `gpas-proxy` | `oauth2-proxy:v7.6.0` | 8082 | ~5 s |
| 7 | `anonymizer` | `medanon:latest` | 8000 | ~10 s |
| 8 | `ui` | `medanon-ui:latest` | 8501 | ~10 s |

### Step 4: Initialize gPAS Domain

```bash
make init-domains
```

Or manually via `http://localhost:8082/gpas-web/` (login: `admin@ths`):
1. Navigate to **gPAS -> Domains -> New**
2. Name: value of `GPAS_DOMAIN` in `.env` (e.g., `TESTING`)
3. Generator: `ReedSolomonLagrange`, Alphabet: `Symbol31`
4. Save

**Important:** Never insert domains directly into MySQL. gPAS maintains an in-memory cache that is only populated when domains are created through the API.

### Step 5: Verify

```bash
curl http://localhost:8000/health         # {"status":"ok"}
curl http://localhost:8000/ready          # {"ready": true}
curl http://localhost:4180/fhir/metadata  # HAPI FHIR CapabilityStatement (through proxy)
curl http://localhost:8180/health/ready   # Keycloak health
open http://localhost:8501                # Streamlit UI
```

### Development Mode

```bash
make dev
```

Applies `docker-compose.dev.yml` overrides:
- Anonymizer: source directory mounted live at `/code/src`, uvicorn `--reload` enabled
- HAPI FHIR: in-memory H2 database (data resets on restart)
- gPAS: WildFly management console exposed on `127.0.0.1:9990`
- gPAS: FHIR API exposed on host port 8080

---

## 3. Environment Variables Reference

### Anonymizer: Core

| Variable | Required | Default | Description |
|---|---|---|---|
| `MEDANON_CONFIG_DIR` | no | `/code/config` | YAML config file directory |
| `MEDANON_HASH_KEY` | production | — | HMAC-SHA3-256 key. Generate: `openssl rand -hex 32` |
| `MEDANON_RSA_PUBLIC_KEY` | for encrypt | `/code/keys/id_rsa.pub` | RSA public key path (inside container) |
| `MEDANON_RSA_PRIVATE_KEY` | for decrypt | `/code/keys/id_rsa` | RSA private key path (inside container) |
| `MEDANON_MAX_BODY_BYTES` | no | `10485760` | Max request body (bytes). Default: 10 MB |
| `MEDANON_API_KEY` | no | — | Legacy shared API key. If set, requires `X-API-Key` header |
| `MEDANON_CORS_ORIGINS` | no | — | Comma-separated CORS origins |
| `MEDANON_READY_TIMEOUT` | no | `5.0` | Readiness probe timeout (seconds) |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ANONYMIZER_PORT` | no | `8000` | Host port |

### Anonymizer: Features

| Variable | Required | Default | Description |
|---|---|---|---|
| `MEDANON_MANIFEST_ENABLED` | no | `false` | Attach transformation manifest to `meta.tag` (GDPR accountability) |
| `MEDANON_NLP_MODEL` | no | — | Expected NLP model (e.g. `en_core_web_lg`). Triggers NLP readiness check |
| `MEDANON_RATE_LIMIT_ENABLED` | no | `true` | Enable rate limiting |

### Anonymizer: Security and Audit

| Variable | Required | Default | Description |
|---|---|---|---|
| `MEDANON_KEY_ALLOWED_DIRS` | no | `/code/keys` | Colon-separated allowed RSA key directories |
| `MEDANON_AUDIT_LOG_FILE` | no | `/output/audit.log` | Audit log file path |
| `MEDANON_AUDIT_LOG_MAX_BYTES` | no | `10485760` | Audit log rotation size (10 MB) |
| `MEDANON_AUDIT_LOG_BACKUP_COUNT` | no | `5` | Number of rotated audit log backups |

### FHIR Server

| Variable | Required | Default | Description |
|---|---|---|---|
| `FHIR_SOURCE_URL` | for fetch ops | `http://hapi-fhir:8080/fhir` | Source FHIR server (Docker-internal) |
| `FHIR_SOURCE_TOKEN` | no | — | Bearer token for source FHIR |
| `FHIR_TARGET_URL` | no | — | Target FHIR server for upload operations |
| `FHIR_TARGET_TOKEN` | no | — | Bearer token for target FHIR |
| `HAPI_SERVER_ADDRESS` | no | `http://hapi-fhir:8080/fhir` | Public base URL for HAPI Bundle links |
| `HAPI_PORT` | no | `8081` | Host port for HAPI (dev mode only) |
| `FHIR_RETRY_COUNT` | no | `2` | Retries on transient errors |
| `FHIR_RETRY_BACKOFF_SEC` | no | `0.3` | Initial backoff (exponential) |

### gPAS: Core

| Variable | Required | Default | Description |
|---|---|---|---|
| `GPAS_URL` | for gPAS | — | gPAS TTP-FHIR gateway URL |
| `GPAS_DOMAIN` | for gPAS | — | Pseudonymization domain (e.g. `TESTING`) |
| `GPAS_OPERATION` | no | `pseudonymizeAllowCreate` | gPAS operation |
| `GPAS_TIMEOUT_SEC` | no | `30` | HTTP timeout |
| `GPAS_BASIC_USER` | for gPAS auth | — | gRAS username (e.g. `user@ths`) |
| `GPAS_BASIC_PASS` | for gPAS auth | — | gRAS password |
| `GPAS_TOKEN` | no | — | Bearer token (takes precedence over basic auth) |
| `GPAS_MYSQL_ROOT_PASSWORD` | yes | — | MySQL root password |

### gPAS: Performance

| Variable | Required | Default | Description |
|---|---|---|---|
| `GPAS_CACHE_ENABLED` | no | `true` | Enable LRU pseudonym cache |
| `GPAS_RETRY_COUNT` | no | `2` | Retries on failure |
| `GPAS_RETRY_BACKOFF_SEC` | no | `0.2` | Initial backoff |

### gPAS: Circuit Breaker

| Variable | Required | Default | Description |
|---|---|---|---|
| `GPAS_CB_FAILURE_THRESHOLD` | no | `5` | Failures before circuit opens |
| `GPAS_CB_RECOVERY_TIMEOUT_SEC` | no | `30` | Recovery probe timeout |
| `GPAS_CB_WINDOW_SEC` | no | `60` | Failure counting window |

### Keycloak OIDC

| Variable | Required | Default | Description |
|---|---|---|---|
| `KEYCLOAK_URL` | no | — | Keycloak server URL. Blank = auth disabled |
| `KEYCLOAK_REALM` | no | `medanon` | Keycloak realm name |
| `KEYCLOAK_PORT` | no | `8180` | Host port |
| `KEYCLOAK_ADMIN` | no | `admin` | Admin console username |
| `KEYCLOAK_ADMIN_PASSWORD` | yes | — | Admin console password |
| `KEYCLOAK_CLIENT_ID` | no | `medanon-api` | Confidential client for JWT introspection |
| `KEYCLOAK_CLIENT_SECRET` | yes | — | Client secret |
| `KEYCLOAK_UI_CLIENT_ID` | no | `medanon-ui` | Public PKCE client for Streamlit |
| `EXTERNAL_HOST` | no | `localhost` | Browser-facing hostname for OIDC redirects |

### OAuth2 Proxies

| Variable | Required | Default | Description |
|---|---|---|---|
| `FHIR_PROXY_CLIENT_ID` | no | `fhir-proxy-oauth` | FHIR proxy OIDC client |
| `FHIR_PROXY_CLIENT_SECRET` | yes | — | FHIR proxy client secret |
| `FHIR_PROXY_COOKIE_SECRET` | yes | — | 32-byte base64 cookie encryption key |
| `FHIR_PROXY_PORT` | no | `4180` | Host port |
| `GPAS_PROXY_CLIENT_ID` | no | `gpas-proxy-oauth` | gPAS proxy OIDC client |
| `GPAS_PROXY_CLIENT_SECRET` | yes | — | gPAS proxy client secret |
| `GPAS_PROXY_COOKIE_SECRET` | yes | — | 32-byte base64 cookie encryption key |
| `GPAS_PROXY_PORT` | no | `8082` | Host port |

### UI

| Variable | Required | Default | Description |
|---|---|---|---|
| `UI_PORT` | no | `8501` | Host port for Streamlit UI |

---

## 4. Secrets Management

### Generating Secrets

```bash
# HMAC hash key (64 hex chars)
openssl rand -hex 32

# Passwords (24-char base64)
openssl rand -base64 24

# Cookie secrets (32-byte base64)
openssl rand -base64 32

# RSA keypair (4096-bit)
openssl genrsa -out services/anonymizer/keys/id_rsa 4096
openssl rsa -in services/anonymizer/keys/id_rsa -pubout -out services/anonymizer/keys/id_rsa.pub
```

### Files That Must Never Be Committed

The `.gitignore` already excludes:
- `.env` (all secrets)
- `services/anonymizer/keys/id_rsa` (RSA private key)
- `output/` (de-identified data and audit logs)

### Secret Rotation

| Secret | Rotation Effect |
|---|---|
| `MEDANON_HASH_KEY` | All cryptohash pseudonyms change. Rotate intentionally. |
| `MEDANON_RSA_PRIVATE_KEY` | Old encrypted values become unreadable. Keep old key for historical data. |
| `GPAS_BASIC_PASS` | Update `.env` + MySQL: `CALL changePassword('user','new-pass');` |
| `GPAS_MYSQL_ROOT_PASSWORD` | Requires `docker compose down -v` (destroys data). Back up first. |
| `KEYCLOAK_CLIENT_SECRET` | Update in Keycloak admin + `.env`. Restart anonymizer. |

---

## 5. Kubernetes (Helm) Deployment

The umbrella chart at `helm/medanon/` deploys the anonymizer, HAPI FHIR, and gPAS. Keycloak and proxies are optional sub-charts.

### Step 1: Build and Push Images

```bash
# Anonymizer
docker build --target prod \
  -t registry.example.com/medanon:1.0.0 \
  services/anonymizer/
docker push registry.example.com/medanon:1.0.0

# UI
docker build -t registry.example.com/medanon-ui:1.0.0 client/
docker push registry.example.com/medanon-ui:1.0.0
```

### Step 2: Validate

```bash
make helm-lint        # yamllint + Helm schema validation
make helm-template    # render all Kubernetes YAML (dry run)
```

### Step 3: Install

```bash
helm upgrade --install medanon ./helm/medanon \
  --set global.registry=registry.example.com \
  --set anonymizer.env.GPAS_URL=http://medanon-gpas:8080/ttp-fhir/fhir/gpas \
  --set anonymizer.env.FHIR_SOURCE_URL=http://medanon-fhir-server:8080/fhir \
  --set anonymizer.secrets.MEDANON_HASH_KEY=<hex-key> \
  --set gpas.secrets.WF_ADMIN_PASS=<password> \
  --set gpas.db.secrets.rootPassword=<password> \
  --namespace medanon --create-namespace
```

Or use the Makefile:

```bash
make helm-install \
  REGISTRY=registry.example.com \
  GPAS_URL=http://medanon-gpas:8080/ttp-fhir/fhir/gpas
```

### Step 4: Verify

```bash
kubectl get pods -n medanon
kubectl get svc -n medanon

# Port-forward to test
kubectl port-forward svc/medanon-anonymizer 8000:8000 -n medanon
curl http://localhost:8000/health
```

### Chart Structure

```
helm/
├── medanon/                     Umbrella chart
│   ├── Chart.yaml
│   ├── values.yaml              Global defaults (registry, ingress, keycloak, proxies)
│   └── templates/
│       └── ingress.yaml         Optional ingress (set ingress.enabled: true)
└── charts/
    ├── anonymizer/              Deployment, Service, ConfigMap, Secret
    ├── fhir-server/             Deployment, Service, ConfigMap
    └── gpas/                    Deployment, StatefulSet (MySQL), Services, Secrets
```

### Kubernetes-Specific Notes

- **gPAS startup:** initContainer waits for MySQL TCP before WildFly starts.
- **Secret checksums:** Deployment annotations include `checksum/secret` so pods roll when secrets change.
- **Network policies:** Each sub-chart restricts ingress to expected callers only.
- **Non-root:** Anonymizer runs as UID 1000. gPAS runs with `runAsNonRoot: true`.
- **MySQL persistence:** StatefulSet with PVC. Data survives pod restarts. To wipe: `kubectl delete pvc -l app.kubernetes.io/component=gpas-db -n medanon`.
- **Ingress paths:** `/` -> anonymizer, `/fhir` -> HAPI FHIR, `/gpas-web` and `/ttp-fhir` -> gPAS.
- **Prometheus:** All pods have scrape annotations (`prometheus.io/scrape: "true"`).

### Optional Components

Enable in `values.yaml`:

```yaml
keycloak:
  enabled: true

fhirProxy:
  enabled: true

gpasProxy:
  enabled: true

ingress:
  enabled: true
  host: medanon.example.com
  tls:
    enabled: true
    secretName: medanon-tls
```

---

## 6. Resource Requirements

Measured at idle after full startup:

| Service | RAM (idle) | RAM (limit) | CPU (idle) | CPU (limit) |
|---|---|---|---|---|
| `ui` | ~150 MB | 512 MB | ~0.05 | 0.5 |
| `anonymizer` | ~300 MB | 2 GB | ~0.05 | 1.0 |
| `fhir-server` | ~1.2 GB | 3 GB | ~0.1 | 2.0 |
| `gpas` | ~1.5 GB | 6 GB | ~0.1 | 2.0 |
| `gpas-db` | ~500 MB | 4 GB | ~0.1 | 1.0 |
| `keycloak` | ~400 MB | 1 GB | ~0.05 | 1.0 |
| `fhir-proxy` | ~30 MB | 128 MB | ~0.01 | 0.25 |
| `gpas-proxy` | ~30 MB | 128 MB | ~0.01 | 0.25 |
| **Total** | **~4.1 GB** | **~16.8 GB** | **~0.5** | **8.0** |

**Minimum host RAM:** 8 GB. **Recommended:** 12 GB+.

If NLP (spaCy `en_core_web_lg`) is loaded in the anonymizer, add ~800 MB to the anonymizer idle figure.

---

## 7. Production Hardening

### TLS Termination

All Docker Compose host ports bind to `127.0.0.1` (loopback only). For remote access, place a reverse proxy in front:

```nginx
server {
    listen 443 ssl;
    server_name medanon.example.com;

    ssl_certificate     /etc/ssl/certs/medanon.crt;
    ssl_certificate_key /etc/ssl/private/medanon.key;

    # Streamlit UI
    location / {
        proxy_pass http://127.0.0.1:8501;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }

    # Anonymizer API
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header X-Request-ID $request_id;
    }

    # Keycloak
    location /auth/ {
        proxy_pass http://127.0.0.1:8180/;
    }
}
```

Expose only ports 8501 (UI) and 8000 (API) through the proxy. Never expose 8080 (gPAS) or HAPI FHIR externally.

### Authentication

| Layer | Mechanism |
|---|---|
| Streamlit UI | Keycloak OIDC with PKCE (S256) |
| Anonymizer API | Keycloak JWT (RS256) or legacy `X-API-Key` |
| HAPI FHIR (browser) | oauth2-proxy (`fhir-proxy`) |
| gPAS (browser) | oauth2-proxy (`gpas-proxy`) |
| gPAS API | gRAS basic auth |

For production:
1. Set `KEYCLOAK_URL` to enable OIDC
2. Change all default passwords (Keycloak users, gRAS, MySQL)
3. Set `MEDANON_API_KEY` for any CI/CD scripts

### GDPR Compliance Features

| Feature | Variable | Purpose |
|---|---|---|
| Transformation manifest | `MEDANON_MANIFEST_ENABLED=true` | GDPR Art. 30: track what transformations were applied |
| Audit logging | `MEDANON_AUDIT_LOG_*` | Who accessed what, when, with which auth method |
| Key path validation | `MEDANON_KEY_ALLOWED_DIRS` | Prevent path traversal to unauthorized key files |
| Circuit breaker | `GPAS_CB_*` | Prevent cascade failures |

### HAPI FHIR Persistence

The default H2 in-memory database loses all data on restart. For production, switch to PostgreSQL:

```yaml
# services/fhir-server/config/application.yaml
spring:
  datasource:
    url: jdbc:postgresql://pg-host:5432/hapi
    username: hapi
    password: ${HAPI_DB_PASS}
    driverClassName: org.postgresql.Driver
```

### Logging

Set `LOG_LEVEL=INFO` in production. `DEBUG` mode may log FHIR resource content containing PHI.

---

## 8. Upgrading

### Code Changes (Anonymizer / UI)

```bash
git pull
make build    # rebuild images
make up       # recreate containers with new images
```

### gPAS Version Upgrade

1. Back up MySQL volume (see RUNBOOK.md)
2. Update the WildFly image tag in `docker-compose.yml`
3. Update WAR/EAR files in `services/gpas/deployments/`
4. Run `make build && make up`

### Helm (Kubernetes)

```bash
docker build --target prod -t registry.example.com/medanon:<new-tag> services/anonymizer/
docker push registry.example.com/medanon:<new-tag>

helm upgrade medanon ./helm/medanon \
  --set anonymizer.tag=<new-tag> \
  --namespace medanon
```

---

## 9. Health Verification

### All Services at Once

```bash
docker compose ps    # health status for all containers
```

### Individual Checks

```bash
# Anonymizer
curl -s http://localhost:8000/health | python3 -m json.tool
curl -s http://localhost:8000/ready | python3 -m json.tool

# HAPI FHIR (through proxy)
curl -s http://localhost:4180/fhir/metadata | head -5

# gPAS (through proxy)
curl -s http://localhost:8082/ttp-fhir/fhir/gpas/metadata | head -5

# Keycloak
curl -s http://localhost:8180/health/ready

# Streamlit UI
curl -s http://localhost:8501/_stcore/health
```

### End-to-End Smoke Test

```bash
# De-identify a sample patient
curl -s -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Patient","id":"smoke-001","name":[{"family":"Test"}],"birthDate":"1990-01-01"}' \
  | python3 -m json.tool

# Expected: id replaced, name absent/redacted, birthDate generalized to year
```

---

## 10. Uninstalling

### Docker Compose: Keep Data

```bash
make down    # stops containers; Docker volumes preserved
```

### Docker Compose: Remove Everything

```bash
docker compose down -v    # removes containers AND named volumes (gpas-db-data, hapi-data, keycloak-data)
```

### Helm (Kubernetes)

```bash
helm uninstall medanon --namespace medanon

# PVCs are NOT deleted automatically:
kubectl get pvc -n medanon
kubectl delete pvc -n medanon --all    # only if you want to lose all data
kubectl delete namespace medanon
```
