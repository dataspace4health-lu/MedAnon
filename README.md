# MedAnon

Rule-driven FHIR de-identification and pseudonymization toolkit. Accepts FHIR resources in JSON, NDJSON, or XML, applies a configurable set of match-action rules, and returns transformed output in any supported format.

- **REST API** — submit individual resources or Bundles over HTTP
- **CLI** — process files locally or batch-fetch from a FHIR server
- **gPAS integration** — reversible pseudonymization via a TTP gateway
- **NLP scrubbing** — Presidio entity detection for free-text PHI
- **Formats** — JSON, NDJSON (streaming), XML 


---

**Security Notice:**
Always use environment variables for all secrets (passwords, tokens, API keys, etc.). Never commit real secrets to version control. For production, rotate secrets regularly and use a secure secret manager if possible.

---

See [docs/user-manual.md](docs/user-manual.md) for API and CLI reference. See [docs/architecture.md](docs/architecture.md) for system design and data flow. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for production deployment.

---

## Quick Start

### Docker (recommended)

```bash
cp .env.example .env      # copy the template and fill in secrets
make build                # build the anonymizer image (~10 min on first build)
make up                   # start all four containers
```

Verify:

```bash
curl http://localhost:8000/health          # {"status":"ok"}
curl http://localhost:8081/fhir/metadata   # HAPI FHIR CapabilityStatement
curl http://localhost:8080/gpas-web/       # gPAS web UI (admin@ths / ttp-tools)
```

Process a test resource:

```bash
curl -s -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Patient","id":"p-001","name":[{"family":"Mustermann","given":["Max"]}],"birthDate":"1980-05-12"}' \
  | python3 -m json.tool
```

### Local (no Docker)

```bash
make setup                               # create .venv and install deps
cd services/anonymizer
uvicorn src.api.main:app --reload        # REST API on :8000
python3 -m src.cli.main process input.json output.json --config config/config.yaml   # CLI
```

---

## Services

| Container | Image | Host port | Purpose |
|---|---|---|---|
| `medanon-ui` | Built from `client/Dockerfile` | `8501` | React browser UI (nginx + SPA) |
| `medanon` | Built from `services/anonymizer/Dockerfile` | `8000` | FHIR de-identification engine (FastAPI) |
| `hapi-fhir` | `hapiproject/hapi:latest` | `8081` | HAPI FHIR R4 server (in-memory H2) |
| `gpas-wildfly` | `mosaicgreifswald/wildfly:38` | `8080` | gPAS TTP pseudonymization service |
| `gpas-mysql` | `mysql:8` | internal | MySQL backend for gPAS |

All containers communicate over the internal `fhir-net` Docker bridge network.

---

## Deployment

### Docker Compose

```bash
make up           # start full stack
make dev          # start with hot-reload and source mount (dev mode)
make down         # stop and remove containers
make build        # rebuild images after code or config changes
make logs         # tail logs
```

### Kubernetes (Helm)

```bash
# Build and push images to your registry
docker build --target prod -t registry.example.com/medanon:latest services/anonymizer/
docker push registry.example.com/medanon:latest
make helm-build-gpas REGISTRY=registry.example.com
docker push registry.example.com/medanon-gpas:latest

# Dry-run (no cluster needed)
make helm-template

# Install
make helm-install \
  REGISTRY=registry.example.com \
  GPAS_URL=http://medanon-gpas:8080/ttp-fhir/fhir/gpas \
  FHIR_SOURCE_URL=http://medanon-fhir-server:8080/fhir

kubectl get pods          # verify all pods Running
helm uninstall medanon    # remove
```

See [helm/medanon/values.yaml](helm/medanon/values.yaml) for all configuration options.

---

## Config Profiles

Choose a profile based on whether you have a live gPAS server. There is no automatic fallback — switch profiles explicitly.

| Profile | When to use | Pseudonymization |
|---|---|---|
| `config_gpas.yaml` | Production — live gPAS available | `gpas_pseudonymize` — reversible via TTP |
| `config_gdpr_eu.yaml` | GDPR, no external server | `cryptohash` — HMAC-SHA3-256 keyed |
| `config.yaml` | Local dev / offline | `cryptohash` — plain SHA3-256 (no key) |

The API auto-selects `config_gpas.yaml` when `GPAS_URL` is set; otherwise it uses `config.yaml`.

---

## Key Configuration

All secrets live in `.env` (gitignored). Copy `.env.example` to get started.

| Variable | Required | Description |
|---|---|---|
| `GPAS_URL` | for gPAS profile | gPAS TTP-FHIR gateway, e.g. `http://10.0.0.1:8080/ttp-fhir/fhir/gpas` |
| `GPAS_DOMAIN` | for gPAS profile | Pseudonymization domain name, e.g. `TESTING` |
| `GPAS_BASIC_USER` | for gPAS auth | `user@ths` |
| `GPAS_BASIC_PASS` | for gPAS auth | `ttp-tools` |
| `MEDANON_HASH_KEY` | for HMAC hashing | Hex string — rotate to invalidate all pseudonyms |
| `MEDANON_RSA_PUBLIC_KEY` | for encrypt action | Path to PEM public key, e.g. `keys/id_rsa.pub` |
| `MEDANON_RSA_PRIVATE_KEY` | for decrypt action | Path to PEM private key — never commit |
| `FHIR_SOURCE_URL` | for server fetch | HAPI FHIR base URL |
| `MEDANON_CORS_ORIGINS` | no | Comma-separated allowed CORS origins |

### gPAS Authentication

gPAS uses built-in form-based auth (gRAS). Login at `http://<host>:8080/gpas-web/` with:

| Username | Password | Role |
|---|---|---|
| `admin@ths` | `ttp-tools` | Admin — domain and project management |
| `user@ths` | `ttp-tools` | Standard — pseudonymization only |

The `@ths` domain suffix is required. Credentials are seeded by `services/gpas/sqls/02_init_database_gras_for_gpas.sql` on first MySQL initialization.

### RSA Keys

To generate a new keypair:

```bash
openssl genrsa -out services/anonymizer/keys/id_rsa 4096
openssl rsa -in services/anonymizer/keys/id_rsa -pubout -out services/anonymizer/keys/id_rsa.pub
```

The private key is excluded from version control via `.gitignore`.

---

## Tests

```bash
cd services/anonymizer
python3 -m pytest tests/test_io_formats.py -q   # runs locally, no Docker
python3 -m pytest tests/test_risk.py -q         # risk metrics, no Docker
python3 -m pytest tests/ -q                     # full suite (requires Docker)
python3 -m pytest tests/ --cov=src --cov-report=term-missing
```

**Constraints:**
- `test_io_formats.py`, `test_risk.py` — run locally without Docker
- `test_processor.py`, `test_deidentify.py`, `test_pseudonymize.py` — require Docker (`fhirpathpy` is incompatible with Python 3.13)
- `test_nlp_detect.py` — requires `en_core_web_lg` spaCy model

---

## Project Layout

```
client/                  React browser UI (Vite + TypeScript + Shadcn/ui)
│   ├── src/                 Source code (pages, components, API layer)
│   ├── nginx.conf           Reverse proxy config
│   └── Dockerfile           Multi-stage build (node → nginx)
services/
├── anonymizer/          FastAPI app + CLI (Python 3.13)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config/              config.yaml, config_gpas.yaml, config_gdpr_eu.yaml
│   ├── src/
│   │   ├── api/main.py      REST API entry point
│   │   ├── cli/main.py      CLI entry point
│   │   ├── pipeline/        config loader, FHIRPath processor, I/O formats
│   │   ├── actions/         redact, cryptohash, encrypt, perturb, substitute, generalize
│   │   ├── analytics/       risk.py — k-anonymity, l-diversity, re-ID risk metrics
│   │   └── integrations/
│   │       ├── gpas/        gPAS HTTP client + dispatcher
│   │       ├── nlp/         Presidio NLP PHI detector
│   │       └── fhir/        FHIR REST client ($everything, paginated fetch)
│   └── tests/               pytest suite (31+ tests)
├── fhir-server/         HAPI FHIR configuration
│   └── config/application.yaml
└── gpas/                gPAS deployment files
    ├── Dockerfile       custom image for Kubernetes
    ├── envs/            WildFly env files
    ├── sqls/            MySQL init scripts
    ├── jboss/           WildFly CLI configuration
    └── deployments/     WAR/EAR application files
helm/medanon/            Kubernetes Helm chart
docs/                    Architecture, user manual, deployment guide
scripts/                 Batch processing and data import scripts
data/                    Sample FHIR resources
```
