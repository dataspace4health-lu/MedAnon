# MedAnon — AI Assistant Context (CLAUDE.md)

This file provides context for AI coding assistants working on MedAnon.
Read it before making any changes.

---

## What this project is

MedAnon is a rule-driven FHIR R4 de-identification and pseudonymization toolkit.
It accepts healthcare data in FHIR (JSON, NDJSON, XML), HL7v2, and DICOM,
applies configurable match-action rules from YAML profiles, and returns
privacy-safe output. The system is used as a privacy layer between an
identified FHIR source and downstream consumers (research, dataspaces, analytics).

Core components:
- **`services/anonymizer/`** — FastAPI backend (Python 3.12)
- **`client/`** — React SPA (TypeScript + Vite + Shadcn/ui)
- **`packages/medanon-core/`** — zero-dependency shared domain types
- **`helm/medanon/`** — Kubernetes Helm chart
- **`docs/`** — architecture, user manual, deployment guide

---

## Build & test commands

### Python backend

```bash
# Create virtualenv and install all dependencies
make setup

# Run the full test suite (requires virtualenv)
make test

# Run with coverage
make test-cov

# Lint (ruff)
make lint

# Auto-format
make format

# Run specific tests without Docker (fast feedback loop)
cd services/anonymizer
PYTHONPATH=src \
MEDANON_HASH_ALLOW_PLAIN=true \
MEDANON_CONFIG_DIR=config \
MEDANON_RATE_LIMIT_ENABLED=false \
  python -m pytest \
    tests/test_action_dispatcher.py \
    tests/test_cache.py \
    tests/test_circuit_breaker.py \
    tests/test_crypto.py \
    tests/test_jobs.py \
    tests/test_post_processor.py \
    tests/test_proxy_clients.py \
    tests/test_scoring.py \
    tests/test_where_evaluator.py \
    -q --tb=short
```

**Important:** `medanon-core` must be installed before running tests:
```bash
pip install -e packages/medanon-core/
```

**Required for test client:** `httpx` is a test-only dep not in `requirements.txt`:
```bash
pip install pytest pytest-asyncio pytest-cov httpx
```

Tests that require Docker (fhirpathpy has compatibility limitations with newer Python versions, skip locally):
- `test_fhir_bulk.py`, `test_processor.py`, `test_deidentify.py`, `test_pseudonymize.py`

Tests that require a running Redis (`MEDANON_TEST_REDIS_URL`):
- `test_jobs_redis.py`

Tests that require pydicom:
- `test_dicom.py`

### React frontend

```bash
cd client
npm ci
npx tsc --noEmit      # type-check
npx eslint .          # lint
npm run build         # production build (outputs to client/dist/)
npm run dev           # dev server (hot reload on :5173)
```

### Docker (full stack)

```bash
cp .env.example .env   # fill in secrets
make build             # build anonymizer + UI images
make up                # start all services
make down              # stop
make logs              # tail logs
make dev               # hot-reload dev mode
```

Verify a running stack:
```bash
curl http://localhost:8000/health    # {"status":"ok"}
curl http://localhost:8000/ready     # {"ready":true}
# UI at http://localhost:8501
```

---

## Project layout

```
packages/medanon-core/        Zero-dependency shared library
  medanon_core/
    domain.py                 Job, JobStatus, exception types
    ports.py                  Port interfaces (PseudonymizerPort, FhirClientPort, …)
    analytics/                risk.py, synthetic.py, synthetic_sdv.py

services/anonymizer/          FastAPI app + CLI
  src/
    api/
      main.py                 FastAPI entry point, middleware, startup
      auth.py                 API key auth + RBAC (admin/analyst/viewer)
      deps.py                 Rate limiter, SSRF guard, body-size cap
      routers/                HTTP route handlers (process, jobs, configs, …)
      services/               Business logic services (jobs, processing, health, …)
      schemas/                Pydantic models
    pipeline/
      processor.py            Orchestrator — composes all pipeline stages
      rule_matcher.py         FHIRPath rule index per resource type
      action_dispatcher.py    Pass 1: stateless actions + gPAS BatchWork collection
      gpas_orchestrator.py    Pass 2: single gPAS HTTP call per batch
      post_processor.py       Pass 3: reference rewriting + text ID replacement
      manifest.py             Audit trail in resource meta.tag
      deidentify.py           Unified action registry + NLP adapter lazy singleton
      io_formats.py           JSON / NDJSON / XML parse + serialize (defusedxml)
      config/
        loader.py             YAML config parser (${VAR:-default} interpolation)
        service.py            get_settings(profile) with @lru_cache
        store.py              SQLite config profile metadata store
      jobs/
        store.py              SqliteJobStore (WAL mode, thread-safe)
        worker.py             Async job executor (BLPOP / 2s poll)
      scoring/                Privacy, utility, quality scoring engine
      subscriptions/          FHIR subscription support
    actions/                  One file per action (redact, cryptohash, …)
    integrations/
      gpas/                   gPAS HTTP client + circuit breaker + cache
      fhir/                   FHIR REST client (reader, writer, bulk export)
      nlp/                    Presidio adapter (local + remote)
      analytics/              Proxy client for analytics microservice
      redis/                  Redis job store
      staging/                PostgreSQL staging for large bulk exports
      storage/                MinIO S3 result storage
      http_client.py          Shared urllib3 pool with retry + jitter
    utils/
      cache.py                LocalLruCache + RedisCache + TieredCache
      circuit_breaker.py      Three-state circuit breaker
      crypto.py               RSA encrypt/decrypt (pyca/cryptography)
      fhirpath.py             FHIRPath traversal helpers
      metrics.py              Prometheus counters + histograms
      audit.py                Structured audit log
  config/                     Bundled YAML profiles
  tests/                      pytest suite

client/
  src/
    App.tsx                   Router + lazy-loaded pages
    api/                      Typed API client modules (health, jobs, configs, …)
    pages/                    Route page components
    components/
      layout/                 AppLayout, AppSidebar, PageHeader
      shared/                 Reusable widgets
      ui/                     Shadcn/ui primitives
    hooks/                    Custom React hooks
    context/                  React context providers
```

---

## Key design decisions

### Two-pass pipeline

The processor runs in two passes:
1. **Pass 1 (action_dispatcher):** Apply stateless actions immediately. Collect gPAS values into `BatchWork` without calling gPAS.
2. **Pass 2 (gpas_orchestrator):** Send ONE HTTP request to gPAS for all collected values across all resources. Reduces gPAS round trips from O(N × rules) to O(1) per batch.

### Never use `from module import variable` for singletons

Module-level variables like `_job_store` in `pipeline/jobs/store.py` are initialised after import time. Always access them via the module:
```python
import pipeline.jobs.store as _store_mod
store = _store_mod._job_store  # correct: reads current value
```

Not:
```python
from pipeline.jobs.store import _job_store  # wrong: captures None at import time
```

### Port protocols for testability

The pipeline depends on `PseudonymizerPort`, `FhirClientPort`, and `NlpDetectorPort` interfaces (defined in `packages/medanon-core/medanon_core/ports.py`). Pass mocks in tests:
```python
pseudonymizer = unittest.mock.MagicMock(spec=PseudonymizerPort)
```

### Config profiles

Eight bundled profiles: `minimal`, `gpas`, `gdpr`, `hipaa`, `research`, `structural`, `value-masking`, `auto`.
`auto` selects `config_gpas.yaml` when `GPAS_URL` is set, else `config.yaml`.
User-defined profiles live in `/output/user-configs/<name>.yaml`.

### Secrets in environment variables only

All secrets (passwords, API keys, tokens) go in `.env` (gitignored). Never hardcode secrets. Use `os.environ.get("VAR", "")` with empty-string defaults for optional secrets.

### defusedxml for all XML parsing

All XML input must go through `defusedxml` to prevent XXE attacks. Never import `xml.etree.ElementTree` directly in production code.

### CSPRNG for all randomness

Use `secrets.randbelow()` (not `random`) in the `perturb` action and anywhere cryptographic randomness is needed.

---

## Config profiles (YAML)

Each profile defines rules as a list of `{match, action, params}` objects:

```yaml
rules:
  - match:
      resource_type: Patient
      path: name[*].family
    action: cryptohash

  - match:
      resource_type: Patient
      path: birthDate
    action: generalize
    params:
      strategy: date_year_month
```

Available actions: `redact`, `cryptohash`, `encrypt`, `decrypt`, `perturb`, `substitute`, `generalize`, `scrub_text`, `nlp_detect`, `gpas_pseudonymize`.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GPAS_URL` | For gPAS profile | gPAS TTP-FHIR gateway URL |
| `GPAS_DOMAIN` | For gPAS profile | Pseudonymization domain name |
| `GPAS_BASIC_USER` | For gPAS auth | Username |
| `GPAS_BASIC_PASS` | For gPAS auth | Password |
| `MEDANON_HASH_KEY` | For HMAC | Hex string — rotate to invalidate pseudonyms |
| `MEDANON_HASH_ALLOW_PLAIN` | Tests only | `true` to allow unhashed SHA3 in dev |
| `MEDANON_API_KEY` | Optional | Enables API key auth when set |
| `MEDANON_CORS_ORIGINS` | Optional | Comma-separated allowed CORS origins |
| `MEDANON_CONFIG_DIR` | Optional | Path to bundled config YAML directory |
| `MEDANON_USER_CONFIG_DIR` | Optional | Path to user-defined config directory |
| `MEDANON_REDIS_URL` | Optional | Redis URL for L2 cache + job queue |
| `MEDANON_WORKER_ENABLED` | Optional | `true` to start the async job worker |
| `MEDANON_RATE_LIMIT_ENABLED` | Optional | `false` to disable rate limiting in tests |
| `FHIR_SOURCE_URL` | Optional | Source FHIR server base URL |
| `FHIR_TARGET_URL` | Optional | Target FHIR server base URL |
| `ANALYTICS_SERVICE_URL` | Optional | Analytics microservice URL |
| `NLP_SERVICE_URL` | Optional | NLP microservice URL |

---

## CI pipeline

The CI workflow (`.github/workflows/ci.yml`) runs on push to `main`/`Staging` and all PRs to `main`:

1. **Lint Python** — `ruff check` + `ruff format --check` on `services/anonymizer/src`
2. **Lint TypeScript** — `tsc --noEmit` + `eslint` in `client/`
3. **SAST Python** — Bandit security scan
4. **SAST TypeScript** — ESLint security plugin
5. **Dependency scan** — `pip-audit` (Python) + `npm audit` (Node)
6. **Unit tests** — pytest on the no-Docker test files (see list above)
7. **Docker build** — builds `medanon:ci` and `medanon-ui:ci`
8. **Trivy scan** — container vulnerability scan (CRITICAL + HIGH exit-code 1)

---

## Adding a new action

1. Create `services/anonymizer/src/actions/<name>.py` with a pure function `def <name>(value, params) -> value`
2. Register it in `services/anonymizer/src/pipeline/deidentify.py` in the `_ACTION_REGISTRY` dict
3. Add tests in `services/anonymizer/tests/test_action_dispatcher.py`
4. Document it in `docs/user-manual.md`

## Adding a new API endpoint

1. Add route handler in `services/anonymizer/src/api/routers/<router>.py`
2. Add Pydantic schemas in `services/anonymizer/src/api/schemas/`
3. Add business logic in `services/anonymizer/src/api/services/`
4. Register router in `services/anonymizer/src/api/main.py` if it's a new router file
5. Add RBAC role in `services/anonymizer/src/api/auth.py` → `ENDPOINT_ROLE_PREFIXES`
6. Write tests in `services/anonymizer/tests/`
7. Update `docs/user-manual.md` and `docs/components.md`

## Adding a new React page

1. Create `client/src/pages/<Name>Page.tsx`
2. Add lazy import + route in `client/src/App.tsx`
3. Add link in `client/src/components/layout/AppSidebar.tsx` (or `HomePage.tsx`)
4. Add typed API calls in `client/src/api/` if new backend endpoints are needed
