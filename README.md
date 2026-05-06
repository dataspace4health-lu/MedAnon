# MedAnon

A rule-driven FHIR R4 de-identification engine that transforms patient data for research, compliance, and data sharing.

---

## Overview

MedAnon accepts FHIR resources (JSON, NDJSON, XML), applies configurable match-action rules, and returns de-identified output. It supports GDPR, HIPAA Safe Harbor, and IRB research profiles out of the box.

**Why it exists:** Clinical data must be de-identified before secondary use. MedAnon automates this with auditable, reversible, and compliance-mapped transformations — without manual scripting.

**Key capabilities:**
- Reversible pseudonymization via gPAS TTP
- NLP-based free-text scrubbing (Presidio + spaCy)
- Async bulk export for large cohorts
- Privacy × utility × quality scoring
- AI-assisted config generation (Phase 4)

---

## Quick Start

**Prerequisites:** Docker Engine 24+, Docker Compose v2, 12 GB RAM

```bash
# 1. Configure
cp .env.example .env          # fill in secrets (see comments inside)

# 2. Build and start
make build                    # build anonymizer + UI images (~10 min first run)
make up                       # start all services (~90 s for gPAS to initialize)
make init-domains             # create gPAS pseudonym domain (first run only)

# 3. Verify
curl http://localhost:8000/health    # → {"status":"ok"}
curl http://localhost:8000/ready     # → {"ready":true, ...}
open http://localhost:8501           # browser UI
```

**De-identify a resource:**

```bash
curl -s -X POST http://localhost:8000/process?config_profile=gdpr \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{"resourceType":"Patient","id":"p1","name":[{"family":"Müller"}],"birthDate":"1951-08-14"}' \
  | python3 -m json.tool
```

---

## Architecture

MedAnon is a microservice stack. The anonymizer is the central service; all others support it.

```
Browser
  └── UI (nginx :8501)
        └── Anonymizer API (:8000) ──── Worker (:9091 metrics)
              ├── NLP microservice (:8200)   Presidio + spaCy
              ├── gPAS TTP (:8080)           reversible pseudonyms
              ├── Analytics (:8100)          risk + synthetic data
              ├── PostgreSQL (app-db)        jobs, configs, staging
              └── Redis                      job queue + cache

Source FHIR server (no host port — isolated network, accessed only via anonymizer)
Target FHIR server (:8082) — de-identified output
```

**4-pass pipeline per resource:**

| Pass | What happens |
|---|---|
| Pass 1 | FHIRPath rule matching + action dispatch (redact, hash, generalize…) |
| Pass 1.5 | Batch NLP entity detection → token replacement |
| Pass 2 | Batch gPAS pseudonymization |
| Pass 3+4 | Cross-resource reference rewriting + manifest tagging |

---

## API Overview

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/process` | De-identify a single resource or Bundle |
| `POST` | `/process/ndjson` | Stream de-identify NDJSON |
| `POST` | `/v1/jobs/bulk-export` | Start async bulk export job |
| `GET` | `/v1/jobs/{id}` | Poll job status |
| `GET` | `/v1/jobs/{id}/result` | Download NDJSON result |
| `POST` | `/v1/score` | Score de-identified output |
| `GET` | `/v1/configs` | List config profiles |
| `POST` | `/v1/ai/generate-config` | Generate config from description (AI) |
| `GET` | `/health` | Liveness check |
| `GET` | `/ready` | Readiness check (probes all upstreams) |

Select a profile per request: `?config_profile=gdpr` — values: `gdpr`, `hipaa`, `gpas`, `research`, `structural`, `value-masking`, `minimal`.

Full reference: [docs/api-reference.md](docs/api-reference.md)

---

## Configuration

Copy `.env.example` to `.env`. All secrets stay in `.env` — never committed.

| Variable | Required | Description |
|---|---|---|
| `MEDANON_API_KEY` | Production | API auth key — blank = open mode (dev only) |
| `MEDANON_HASH_KEY` | Production | HMAC key for pseudonymization: `openssl rand -hex 32` |
| `GPAS_URL` | gPAS profile | gPAS gateway URL |
| `GPAS_DOMAIN` | gPAS profile | Pseudonymization domain name |
| `GPAS_BASIC_PASS` | gPAS profile | gPAS password |
| `MEDANON_REDIS_PASSWORD` | Production | Redis auth password |
| `MEDANON_APP_DB_PASSWORD` | Production | PostgreSQL app database password |
| `MEDANON_AI_ENABLED` | AI features | `true` to enable AI agents |
| `MEDANON_AI_MODEL` | AI features | LLM model ID (e.g. `ollama/llama3.2`) |
| `MEDANON_SCORING_ENABLED` | Scoring | `true` to auto-score and persist run history |

---

## Dependencies

| Service | Technology | Purpose |
|---|---|---|
| Anonymizer | Python 3.12, FastAPI | De-identification engine + REST API |
| Worker | Same image as anonymizer | Async job executor |
| NLP | Presidio + spaCy `en_core_web_lg` | Free-text PHI detection |
| gPAS | WildFly 38, PostgreSQL 16 | Reversible TTP pseudonymization |
| UI | React 19, TypeScript, Vite, Shadcn/ui | Browser interface |
| Database | PostgreSQL 16 | Jobs, configs, subscriptions, staging |
| Cache / Queue | Redis 7 | gPAS L2 cache + async job queue |
| Analytics | Python, scikit-learn | k-anonymity, synthetic data |

---

## Development

```bash
# Local setup (no Docker)
make setup         # create .venv and install anonymizer deps
make lint          # ruff check
make format        # ruff format

# Run tests
make test          # full pytest suite
make test-cov      # with coverage report

# Hot-reload stack (source mounted into container)
make dev
```

**Test notes:**
- Most tests run locally without Docker
- `test_processor.py`, `test_deidentify.py`, `test_pseudonymize.py` require Python 3.12 (`fhirpathpy` incompatibility with 3.13)
- `test_agents.py` requires AI provider env vars

---

## Deployment

**Docker Compose (staging / single-server):**

```bash
make up                              # start all services
docker compose --profile ha up       # + gPAS PostgreSQL read replica
docker compose --profile s3 up       # + MinIO S3 for job results
docker compose --profile ai up       # + Ollama for AI agents
```

**Kubernetes (Helm):**

```bash
make helm-lint        # validate chart (no cluster needed)
make helm-template    # dry-run rendered YAML
make helm-install     # install/upgrade on active cluster
```

Use `helm/k3s-values.yaml` for single-node K3s deployments.

Full guide: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/ready` returns false | gPAS still initializing | Wait 90 s; `docker compose ps` until all `healthy` |
| `Unknown Domain` on pseudonymization | gPAS domain not created | `make init-domains` (never insert domain via SQL) |
| Text fields contain `[NLP_UNAVAILABLE]` | NLP microservice down | `docker compose ps nlp nlp-lb`; check `docker compose logs nlp` |
| `503` on `/v1/jobs/*` | Job store not initialized | Check `MEDANON_APP_DB_URL` / `MEDANON_REDIS_URL` in `.env` |
| `gPAS circuit breaker OPEN` | gPAS failed 5+ times | `docker compose restart gpas`; circuit self-recovers after 30 s |
| `413 Request Too Large` | Body exceeds limit | Increase `MEDANON_MAX_BODY_BYTES` in `.env` |

More: [docs/RUNBOOK.md](docs/RUNBOOK.md)

---

## Documentation

| Document | Description |
|---|---|
| [docs/INDEX.md](docs/INDEX.md) | Full documentation index — start here |
| [docs/architecture.md](docs/architecture.md) | System design, pipeline, config profiles |
| [docs/data-flow.md](docs/data-flow.md) | Request traces, network layout, NLP/gPAS/AI flows |
| [docs/api-reference.md](docs/api-reference.md) | All REST endpoints with request/response examples |
| [docs/security.md](docs/security.md) | Auth, encryption, GDPR/HIPAA compliance mapping |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Dev / staging / production deployment guide |
| [docs/user-manual.md](docs/user-manual.md) | UI walkthrough, CLI, config file format |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Monitoring, backup, secret rotation, troubleshooting |
| [docs/scoring-system.md](docs/scoring-system.md) | Privacy × utility × quality scoring model |
| [docs/policies.md](docs/policies.md) | Profile selection and compliance mapping |
