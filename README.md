# MedAnon

A rule-driven FHIR R4 de-identification engine that transforms patient data for research, compliance, and data sharing.

---

## Overview

MedAnon accepts FHIR resources (JSON, NDJSON, XML), applies the `match → action` rules you author in a YAML config, and returns de-identified output. You write the rules; the bundled GDPR, HIPAA Safe Harbor, and research profiles are starting examples, not the product.

**Why it exists:** Clinical data must be de-identified before secondary use. MedAnon automates this with auditable, reversible, compliance-mapped transformations instead of one-off scripts.

**Key capabilities:**
- Author your own FHIRPath `match → action` rules, with AI assistance to draft and explain them
- Multi-format intake: FHIR JSON / NDJSON / XML (plus CDA, DICOM, HL7 v2 via the formats package)
- Reversible pseudonymization via gPAS TTP
- NLP free-text scrubbing (Presidio + spaCy), fail-closed
- Async bulk export for large cohorts
- Privacy × utility × quality scoring
- EHDS / TEHDAS2 D7.2 governance: data permits, permit-scoped pseudonyms, opt-out registers, Five-Safes disclosure control, data-minimisation and cumulative-exposure assessment, anonymous Transformation and Synthetic Data Passports, statistical-format (aggregate + DP) release, and HealthDCAT-AP dataset discovery

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
curl -s -X POST "http://localhost:8000/v1/process?config_profile=gdpr" \
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

Source FHIR server (no host port; isolated network, reached only via anonymizer)
Target FHIR server (:8082): de-identified output
```

**Four-stage pipeline per batch** (`pipeline/processor.py`). Stages 2a and 2b run on disjoint paths concurrently:

| Stage | What happens |
|---|---|
| 1. match | FHIRPath rule matching + action dispatch (redact, hash, generalize, ...) |
| 2a. phi_detection | Batch NLP entity detection then token replacement (concurrent with 2b) |
| 2b. pseudonymize | Batch gPAS pseudonym lookup (concurrent with 2a) |
| 3. finalize | gPAS write-back + cross-resource reference rewrite + optional manifest tag |

---

## API Overview

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/v1/process` | De-identify a single resource or Bundle |
| `POST` | `/v1/process/ndjson` | Stream de-identify NDJSON |
| `POST` | `/v1/jobs/bulk-export` | Start async bulk export job |
| `GET` | `/v1/jobs/{id}` | Poll job status |
| `GET` | `/v1/jobs/{id}/result` | Download NDJSON result |
| `POST` | `/v1/score` | Score de-identified output |
| `GET` | `/v1/configs` | List config profiles |
| `POST` | `/v1/ai/generate-config` | Generate config from description (AI) |
| `POST` | `/v1/minimise/assess` | Data-minimisation report (identifier classification) |
| `POST` | `/v1/export/decision` | Five-Safes disclosure decision (APPROVE / REFER / REFUSE) |
| `POST` | `/v1/export/statistical` | Aggregate release with small-cell suppression and/or DP |
| `GET`/`POST` | `/v1/permits` | Data-permit governance lifecycle (admin) |
| `GET` | `/v1/reports` | Durable Transformation Passports |
| `GET` | `/health` | Liveness check |
| `GET` | `/ready` | Readiness check (probes all upstreams) |

Select a built-in profile per request with `?config_profile=<name>`: `gdpr`, `hipaa`, `gpas`, `research`, `structural`, `value-masking`, `minimal` (or `auto`). User-defined profiles created via `POST /v1/configs` are selected by their own name.

Full reference: [website/docs/reference/api.md](website/docs/reference/api.md)

---

## Configuration

Copy `.env.example` to `.env`. Secrets stay in `.env` and are never committed.

| Variable | Required | Description |
|---|---|---|
| `MEDANON_API_KEY` | Production | API auth key; blank = open mode (dev only) |
| `MEDANON_HASH_KEY` | Production | HMAC key for pseudonymization: `openssl rand -hex 32` |
| `GPAS_URL` | gPAS profile | gPAS gateway URL |
| `GPAS_DOMAIN` | gPAS profile | Pseudonymization domain name |
| `GPAS_BASIC_PASS` | gPAS profile | gPAS password |
| `MEDANON_REDIS_PASSWORD` | Production | Redis auth password |
| `MEDANON_APP_DB_PASSWORD` | Production | PostgreSQL app database password |
| `MEDANON_AI_ENABLED` | AI features | `true` to enable AI agents |
| `MEDANON_AI_PROVIDER` | AI features | LLM model ID (default `ollama/llama3.1`) |
| `MEDANON_AI_API_BASE` | AI features | LLM endpoint (default `http://ollama:11434`) |
| `MEDANON_AUTH_PROVIDER` | Auth | `auto` (default) / `apikey` / `oidc` / `none` |
| `OIDC_ISSUER` | OIDC auth | Realm issuer URL, must match the token `iss` exactly |
| `MEDANON_SCORING_ENABLED` | Scoring | `true` to auto-score and persist run history |
| `MEDANON_REGULATED_MODE` | EHDS release | `true` tightens all fail-soft defaults into hard requirements (see website/docs/explanation/security-model.md) |
| `MEDANON_OPTOUT_FILE` | Opt-out | Path to a newline-delimited opt-out register (EHDS Art 71) |

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
make setup         # create .venv, install anonymizer deps + pinned dev tools
make install-hooks # gate `git push` on `make ci-local`  ← do this once

make lint          # ruff check
make format        # ruff format (all service trees)

# Run tests
make test          # full pytest suite
make test-cov      # with coverage report

make ci-local      # everything CI would run, plus the tests CI cannot see
```

**`make ci-local` is the real gate.** `**/tests/` is gitignored, so the suite is
not published and the CI `test` job skips on the public repository. Run
`make ci-local` before every push (`make install-hooks` wires it to a pre-push
hook). It runs: `ruff check`, `ruff format --check` across every service tree,
env drift (`scripts/check_env.py`), the env catalogue freshness check, Trust Gate
id sync, the scoring/analytics sync comparison, and both pytest suites.

**Tooling is pinned** in `requirements-dev.txt` and configured by the root
`ruff.toml`. Both existed as neither before: CI installed `ruff` unpinned, so
`ruff format --check` compared today's formatter against a tree formatted by an
older one.

**Tracked scripts.** `scripts/` is ignored by default (see `.gitignore`), because
most of it is local scratch. Anything the Makefile or CI invokes must be
re-included with a `!scripts/<name>` exception, or `make <target>` and the
workflow break on a fresh clone. Currently tracked: `check_env.py`,
`sync_shared_code.sh` (checks the NLP `HEALTHCARE_ENTITIES` catalogue, the one
copy shared between the anonymizer and the NLP microservice),
`batch_fetch.sh`, `batch_process.sh`, `init_gpas_domains.sh`,
`verify_deployment.sh`, `backup_gpas.sh`, `import_testbase.sh`,
`import_testbase100.sh`, `test_patient.sh`.

**Test notes:**
- Run the suite from `services/anonymizer/`; it works locally without Docker.
- Run it with `MEDANON_MANIFEST_ENABLED=true` to exercise the configuration
  docker-compose actually ships (it activates the output barrier's structural
  check). `make ci-local` does this for you.
- FHIRPath-dependent tests (`test_golden.py`, `test_postgres_stores.py`, `test_processing_runs.py`, `test_staging_store.py`, `test_executor_tabular.py`, `test_scoring_sparse_fp.py`) lazy-import `fhirpathpy`, which pulls in `typing.io` (removed in Python 3.13). Each self-bootstraps a shim, so they run on both 3.12 (the Docker image) and a 3.13 local venv.
- AI tests (`test_agents.py`, `test_ai_local_guard.py`) skip when no AI provider is configured.

---

## Deployment

**Docker Compose (staging / single-server):**

```bash
make up                              # start all services
docker compose --profile ha up       # + gPAS PostgreSQL read replica
```

**Kubernetes (Helm):**

```bash
make helm-lint        # validate chart (no cluster needed)
make helm-template    # dry-run rendered YAML
make helm-install     # install/upgrade on active cluster
```

Use `helm/k3s-values.yaml` for single-node K3s deployments.

Full guide: [website/docs/how-to/deploy-docker.md](website/docs/how-to/deploy-docker.md)

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/ready` returns false | gPAS still initializing | Wait 90 s; `docker compose ps` until all `healthy` |
| `Unknown Domain` on pseudonymization | gPAS domain not created | `make init-domains` (never insert domain via SQL) |
| Text fields contain `[NLP_UNAVAILABLE]` | NLP microservice down | `docker compose ps nlp gateway`; check `docker compose logs nlp` |
| `503` on `/v1/jobs/*` | Job store not initialized | Check `MEDANON_APP_DB_URL` / `MEDANON_REDIS_URL` in `.env` |
| `gPAS circuit breaker OPEN` | gPAS failed 5+ times | `docker compose restart gpas`; circuit self-recovers after 30 s |
| `413 Request Too Large` | Body exceeds limit | Increase `MEDANON_MAX_BODY_BYTES` in `.env` |

More: [website/docs/how-to/operations-runbook.md](website/docs/how-to/operations-runbook.md)

---

## Documentation

The [Docusaurus site](website/) (`cd website && npm start`) under `website/docs/`
is the canonical documentation: tutorials, how-to guides, reference, and
explanation (architecture, data flow, security model, scoring). `docs/` in the
repo root keeps only what has no site equivalent, for contributors:

| Document | Description |
|---|---|
| [docs/INDEX.md](docs/INDEX.md) | Contributor index; start here |
| [docs/reference/env-vars.md](docs/reference/env-vars.md) | Generated environment-variable catalogue, CI-gated against drift |
| [docs/trust-gate/](docs/trust-gate/) | Trust Gate deep-dive guides (architecture, data-quality process, references) |
| [docs/quality-evaluation-methodology.md](docs/quality-evaluation-methodology.md) | Trust Gate measurement methodology |

---

## License

See [LICENSE](./LICENSE) for the full license text.
