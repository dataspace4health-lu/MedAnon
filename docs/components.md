# SPE FHIR BlackBox — Component Catalog

This document is the technical reference for every component in the MedAnon stack. It covers how each service is deployed, configured, and connected — and documents the contracts between them: API endpoints, environment variables, config profiles, auth rules, and data flows.



## Table of Contents

- [00 — Infrastructure & Shared Configuration](#00--infrastructure--shared-configuration)
  - [00.1 Environment Variables Reference](#001-environment-variables-reference)
  - [00.2 De-identification Config Profiles](#002-de-identification-config-profiles)
  - [00.3 Shared Code Modules](#003-shared-code-modules)
  - [00.4 Network Layout & Port Map](#004-network-layout--port-map)
  - [00.5 Audit Logging & Observability](#005-audit-logging--observability)
- [01 — Pseudonymization Service (gPAS + MySQL)](#01--pseudonymization-service-gpas--mysql)
  - [01.1 What gPAS Does](#011-what-gpas-does)
  - [01.2 Container & Configuration](#012-container--configuration)
  - [01.3 Pseudonymization API](#013-pseudonymization-api)
  - [01.4 Authentication](#014-authentication)
  - [01.5 Retry, Circuit Breaker & Cache](#015-retry-circuit-breaker--cache)
  - [01.6 Domain Management & Operations](#016-domain-management--operations)
- [02 — De-identification Engine (Anonymizer API)](#02--de-identification-engine-anonymizer-api)
  - [02.1 What the Anonymizer Does](#021-what-the-anonymizer-does)
  - [02.2 Container & Build Stages](#022-container--build-stages)
  - [02.3 REST API Endpoints](#023-rest-api-endpoints)
  - [02.4 FHIRPath Rule Engine](#024-fhirpath-rule-engine)
  - [02.5 De-identification Actions](#025-de-identification-actions)
  - [02.6 API Authentication & Role-Based Access](#026-api-authentication--role-based-access)
  - [02.7 Request Middleware Stack](#027-request-middleware-stack)
  - [02.8 Command-Line Interface (CLI)](#028-command-line-interface-cli)
- [03 — Data Consumers (FHIR Server, UI & Integrations)](#03--data-consumers-fhir-server-ui--integrations)
  - [03.1 HAPI FHIR Server](#031-hapi-fhir-server)
  - [03.2 React Web Dashboard](#032-react-web-dashboard)
  - [03.3 External System Integrations (EDC / FIWARE)](#033-external-system-integrations-edc--fiware)
- [99 — Architecture Overview](#99--architecture-overview)
  - [99.1 End-to-End System Diagram](#991-end-to-end-system-diagram)
  - [99.2 Data Processing Flow](#992-data-processing-flow)
  - [99.3 Deployment Topologies](#993-deployment-topologies)
  - [99.4 Production Deployment Checklist](#994-production-deployment-checklist)

---

## 00 — Infrastructure & Shared Configuration

Cross-cutting concerns, shared configuration, and infrastructure elements that apply to all components.

### 00.1 Environment Variables Reference

All runtime configuration is injected via environment variables. Config YAML files are version-controlled; secrets are environment-only.

#### Core

| Variable | Default | Required | Description |
|---|---|---|---|
| `MEDANON_CONFIG_DIR` | `/code/config` | No | Directory containing YAML config profiles |
| `MEDANON_MAX_BODY_BYTES` | `10485760` (10 MB) | No | HTTP request body size limit |
| `MEDANON_API_KEY` | — | No | API key; if set, all non-open endpoints require `X-API-Key` header |
| `LOG_LEVEL` | `INFO` | No | Logging verbosity: DEBUG / INFO / WARNING / ERROR |
| `MEDANON_RATE_LIMIT_ENABLED` | `true` | No | Enable `slowapi` rate limiting |
| `MEDANON_CORS_ORIGINS` | — | No | Comma-separated list of allowed CORS origins |

#### Transformation Features

| Variable | Default | Required | Description |
|---|---|---|---|
| `MEDANON_MANIFEST_ENABLED` | `false` | No | Attach transformation manifest to `meta.tag` (GDPR Art. 30) |
| `MEDANON_HASH_KEY` | — | **Recommended** | HMAC key for `cryptohash` action; warn if unset |
| `MEDANON_HASH_ALLOW_PLAIN` | — | No | Set to `true` to suppress HMAC warning and allow plain SHA3-256 (dev only) |
| `MEDANON_RSA_PUBLIC_KEY` | — | For `encrypt` | Path to RSA public key file |
| `MEDANON_RSA_PRIVATE_KEY` | — | For `decrypt` | Path to RSA private key file |
| `MEDANON_KEY_ALLOWED_DIRS` | — | No | Colon-separated allowlist for RSA key directories (path traversal guard) |

#### gPAS Integration

| Variable | Default | Required | Description |
|---|---|---|---|
| `GPAS_URL` | — | For gPAS profiles | gPAS TTP-FHIR gateway base URL (also triggers `config_gpas.yaml` auto-selection) |
| `GPAS_DOMAIN` | — | For gPAS profiles | Pseudonymization domain name (must exist in gPAS) |
| `GPAS_OPERATION` | `pseudonymizeAllowCreate` | No | Operation type: `pseudonymizeAllowCreate`, `pseudonymize`, `dePseudonymize` |
| `GPAS_TIMEOUT_SEC` | `30` | No | HTTP timeout for gPAS requests |
| `GPAS_TOKEN` | — | No | Bearer token for gPAS (takes precedence over basic auth) |
| `GPAS_BASIC_USER` | — | No | gRAS username (format: `user@ths`) |
| `GPAS_BASIC_PASS` | — | No | gRAS password |
| `GPAS_CACHE_ENABLED` | `true` | No | Enable thread-safe LRU cache for pseudonym lookups |
| `GPAS_RETRY_COUNT` | `2` | No | Number of retry attempts on transient gPAS failures |
| `GPAS_RETRY_BACKOFF_SEC` | `0.2` | No | Initial exponential backoff delay (seconds) |
| `GPAS_ADMIN_URL` | — | No | Override gPAS admin URL for domain discovery (defaults to derived from `GPAS_URL`) |
| `GPAS_CB_FAILURE_THRESHOLD` | `5` | No | Circuit-breaker: failures before opening circuit |
| `GPAS_CB_RECOVERY_TIMEOUT_SEC` | `30` | No | Circuit-breaker: recovery probe delay (seconds) |
| `GPAS_CB_WINDOW_SEC` | `60` | No | Circuit-breaker: failure counting window (seconds) |
| `GPAS_MYSQL_ROOT_PASSWORD` | — | **Required** (Docker) | MySQL root password for gPAS database |

#### FHIR Server Integration

| Variable | Default | Required | Description |
|---|---|---|---|
| `FHIR_SOURCE_URL` | `http://hapi-fhir:8080/fhir` | No | Source FHIR server base URL |
| `FHIR_SOURCE_TOKEN` | — | No | Bearer token for source FHIR server |
| `FHIR_TARGET_URL` | — | For upload flows | Target FHIR server base URL |
| `FHIR_TARGET_TOKEN` | — | No | Bearer token for target FHIR server |
| `HAPI_SERVER_ADDRESS` | `http://localhost:8081/fhir` | No | Public-facing HAPI base URL (returned in Bundle responses) |
| `FHIR_RETRY_COUNT` | `2` | No | Retries on transient FHIR server failures |
| `FHIR_RETRY_BACKOFF_SEC` | `0.3` | No | Initial exponential backoff delay (seconds) |
| `FHIR_PAGE_SIZE` | `200` | No | Page size for paginated FHIR fetches (`_count`); set to `0` to let the server decide |
| `FHIR_MAX_PAGES` | `1000` | No | Safety limit on total pages fetched per operation |
| `FHIR_BULK_POLL_INTERVAL_SEC` | `5` | No | Polling interval (seconds) for async `$export` status checks |
| `FHIR_BULK_POLL_TIMEOUT_SEC` | `3600` | No | Maximum wait (seconds) for a bulk export operation to complete |

#### Audit & Readiness

| Variable | Default | Description |
|---|---|---|
| `MEDANON_AUDIT_LOG_FILE` | `/output/audit.log` | Audit log file path |
| `MEDANON_AUDIT_LOG_MAX_BYTES` | `10485760` (10 MB) | Rotation size |
| `MEDANON_AUDIT_LOG_BACKUP_COUNT` | `5` | Number of rotated backups |
| `MEDANON_READY_TIMEOUT` | `5.0` | Timeout (seconds) for `/ready` endpoint upstream checks |
| `MEDANON_NLP_MODEL` | — | Expected spaCy model name; triggers NLP readiness check when set |

---

### 00.2 De-identification Config Profiles

Each profile is a YAML file in `services/anonymizer/config/`. Rules are evaluated in the order they appear.

#### Profile Selection

```
?config_profile=hipaa      → config_hipaa_safe_harbor.yaml
?config_profile=gdpr       → config_gdpr_eu.yaml
?config_profile=gpas       → config_gpas.yaml
?config_profile=research   → config_research_pseudonymous.yaml
?config_profile=structural → config_structure_preserving.yaml
?config_profile=minimal    → config.yaml
?config_profile=auto       → config_gpas.yaml if GPAS_URL set, else config.yaml
(omitted)                  → same as auto
```

#### Profile Comparison

| | `minimal` | `gpas` | `gdpr` | `hipaa` | `research` | `structural` |
|---|---|---|---|---|---|---|
| **ID strategy** | cryptohash | gpas_pseudonymize | HMAC-SHA3-256 | cryptohash | cryptohash | gpas_pseudonymize |
| **Reversible IDs** | No | **Yes** | No | No | No | **Yes** |
| **Date strategy** | regex scrub | year-only | year-only | year-only | **year-month** | year-only (birthDate) |
| **ZIP strategy** | — | — | — | 3-digit prefix | 3-digit prefix | preserved |
| **Fields removed** | Yes | Yes | Yes | Yes | Yes | **No** |
| **gPAS required** | No | **Yes** | No | No | No | **Yes** |
| **Text scrubbing** | regex | NLP + regex | regex | regex | regex | NLP + regex |
| **Regulatory target** | Dev/test | Production | GDPR Art. 4(5) | HIPAA Safe Harbor | IRB research | Structure-first |

See [user-manual.md §5](user-manual.md#5-config-file-format) for the full rule file format, FHIRPath wildcard syntax, and YAML anchor examples.

---

### 00.3 Shared Code Modules

| Module | Path | Responsibility |
|---|---|---|
| Config loader | `pipeline/config.py` | YAML parsing; `${VAR:-default}` env interpolation; rule conflict detection |
| I/O formats | `pipeline/io_formats.py` | Multi-format parse (JSON, NDJSON, XML); serialize output; `defusedxml` for XXE safety |
| Crypto utils | `utils/crypto.py` | RSA key caching; `bounded_random()` CSPRNG; path traversal guard |
| FHIRPath utils | `utils/fhirpath.py` | `find_nodes()` tree traversal; error helpers |
| Metrics | `utils/metrics.py` | Prometheus `Counter` (request count) + `Histogram` (request latency) |
| Logging | `utils/logging.py` | `REQUEST_ID` context-var for X-Request-ID propagation across log lines |
| Deps | `api/deps.py` | Rate limiter; SSRF validation; profile map; `get_settings()` with `lru_cache(8)` |

---

### 00.4 Network Layout & Port Map

#### Docker Compose

| Service | Internal host | External bind | Port |
|---|---|---|---|
| anonymizer | `anonymizer` | `127.0.0.1:8000` | 8000 |
| fhir-server | `hapi-fhir` | `127.0.0.1:8081` | 8080 |
| gpas | `gpas` | `127.0.0.1:8080` | 8080 |
| gpas-db | `gpas-db` | **none** (internal only) | 3306 |
| ui | `medanon-ui` | `127.0.0.1:8501` | 8501 |

All services are on the `fhir-net` bridge network. Expose via a reverse proxy for external access.

#### Kubernetes Ingress

| Ingress path | Backend service | Port |
|---|---|---|
| `/` | anonymizer | 8000 |
| `/fhir` | fhir-server | 8080 |
| `/ttp-fhir` | gpas (API) | 8080 |
| `/gpas-web` | gpas (UI) | 8080 |

---

### 00.5 Audit Logging & Observability

#### Audit Log

Every HTTP request is appended to a structured JSON audit log:

```json
{
  "timestamp": "2026-03-23T12:34:56.789Z",
  "method": "POST",
  "path": "/process",
  "status": 200,
  "request_id": "a1b2c3d4-...",
  "subject": "api_key_user",
  "auth_method": "api_key"
}
```

PHI, raw tokens, and resource content are never logged.

**Rotation:** `MEDANON_AUDIT_LOG_MAX_BYTES` (default 10 MB) × `MEDANON_AUDIT_LOG_BACKUP_COUNT` (default 5) = up to 50 MB on disk.

#### Prometheus Metrics

Metrics exposed at `/metrics` (anonymizer) and `/actuator/prometheus` (HAPI FHIR):

| Metric | Type | Labels | Description |
|---|---|---|---|
| `medanon_request_total` | Counter | `endpoint`, `status_code` | Total HTTP requests |
| `medanon_request_latency_seconds` | Histogram | `endpoint` | Request processing time |

---

## 01 — Pseudonymization Service (gPAS + MySQL)

### 01.1 What gPAS Does

The Pseudonymization Service is the **Trusted Third Party (TTP)** that holds the sole mapping between real patient identifiers and their pseudonyms. It is the trust anchor of the pseudonymization process:

- The De-identification Engine never stores the mapping — it submits identifiers to gPAS and receives opaque pseudonyms
- The `gPAS MySQL` database is the only place where the mapping persists
- Access to re-identification requires credentials with `gras` admin rights

**Key guarantee:** Loss of the `gpas-db` volume means permanent loss of the pseudonym-to-original mapping. Back up regularly.

### 01.2 Container & Configuration

#### Docker Compose Services

**`gpas-db` (MySQL 8.0)**

```yaml
image:    mysql:8.0
port:     3306 (internal only — not exposed to host)
volume:   gpas-db-data (named volume, persistent)
memory:   4 GB limit / 512 MB reservation
schemas:  gpas, gras, notification_service
```

**`gpas` (WildFly 38 + gPAS 2025.2.0)**

```yaml
image:    harbor.miracum.org/gpas/gpas:2025.2.0
port:     8080 → 127.0.0.1:8080
memory:   6 GB limit / 1 GB reservation
JVM:      -Xms512M -Xmx4G
depends:  gpas-db (healthy)
```

#### Health Check

```bash
curl -f http://localhost:8080/  # gPAS root returns 200 when ready
```

Startup typically takes 60–90 seconds on first boot (WildFly deployment + schema migration).

### 01.3 Pseudonymization API

gPAS exposes a TTP-FHIR gateway. MedAnon communicates via:

```
POST http://gpas:8080/ttp-fhir/fhir/gpas/$pseudonymizeAllowCreate
Content-Type: application/fhir+json
Authorization: Basic <base64(user:pass)>

{
  "resourceType": "Parameters",
  "parameter": [
    { "name": "target",  "valueString": "DOMAIN_NAME" },
    { "name": "original","valueString": "PATIENT-001" }
  ]
}
```

**Supported operations:**

| `$operation` | Behaviour |
|---|---|
| `$pseudonymizeAllowCreate` | Return existing pseudonym or create new one (recommended) |
| `$pseudonymize` | Return existing pseudonym; fail if identifier unknown |
| `$dePseudonymize` | Return original identifier from pseudonym |

**Domain configuration:** Domains must be created in the gPAS web UI (`http://localhost:8080/gpas-web`) before use. Set `GPAS_DOMAIN` to the domain name.

### 01.4 Authentication

| Credential | Variable | Format |
|---|---|---|
| Bearer token | `GPAS_TOKEN` | JWT or opaque token |
| Basic username | `GPAS_BASIC_USER` | `username@ths` |
| Basic password | `GPAS_BASIC_PASS` | plaintext (inject via secret) |

Bearer token takes precedence over basic auth. Both are transmitted over HTTPS in production.

### 01.5 Retry, Circuit Breaker & Cache

The MedAnon gPAS client implements three resilience patterns:

#### Retry with Exponential Backoff

```
Attempt 1  → fail (HTTP 503)
Wait 0.2 s →
Attempt 2  → fail
Wait 0.4 s →
Attempt 3  → success (or final failure)
```

Configurable: `GPAS_RETRY_COUNT` (default 2), `GPAS_RETRY_BACKOFF_SEC` (default 0.2).

Retried on: connection errors, HTTP 429, 500, 502, 503, 504.

#### LRU Cache

Thread-safe in-process cache. The same identifier within a processing run is resolved once; subsequent lookups are served from cache. Reduces load on gPAS and improves throughput on bundles with repeated references.

Enable/disable: `GPAS_CACHE_ENABLED` (default `true`).

#### Circuit Breaker

```
State: CLOSED  (normal)
  │  Failure count > GPAS_CB_FAILURE_THRESHOLD within GPAS_CB_WINDOW_SEC
  ▼
State: OPEN  (fail-fast; callers get immediate error)
  │  GPAS_CB_RECOVERY_TIMEOUT_SEC elapsed
  ▼
State: HALF_OPEN  (single probe request allowed)
  │  Probe succeeds           │  Probe fails
  ▼                           ▼
State: CLOSED               State: OPEN
```

Prevents cascade failures when gPAS is unavailable. Configurable via `GPAS_CB_*` variables.

### 01.6 Domain Management & Operations

#### gPAS Web UI

Access: `http://localhost:8080/gpas-web` (Docker Compose) or `https://your-host/gpas-web` (Kubernetes).

Tasks performed in the UI:
- Create and manage pseudonymization **domains**
- View and search pseudonym mappings
- Import/export identifier mappings
- Manage gRAS users and roles

#### Backup

The `gpas-db-data` Docker volume contains all pseudonym mappings. Back up before any upgrade or infrastructure change:

```bash
docker run --rm -v gpas-db-data:/data -v $(pwd):/backup \
  mysql:8.0 mysqldump -h gpas-db -u root -p"$GPAS_MYSQL_ROOT_PASSWORD" \
  --all-databases > /backup/gpas-backup-$(date +%Y%m%d).sql
```

#### Domain Initialisation

```bash
# Create a domain via gPAS REST API (run once per domain per environment)
curl -X POST http://localhost:8080/ttp-fhir/fhir/gpas/domain \
  -H "Content-Type: application/fhir+json" \
  -u "admin@ths:password" \
  -d '{"resourceType":"DomainResource","name":"STUDY-42"}'
```

---

## 02 — De-identification Engine (Anonymizer API)

### 02.1 What the Anonymizer Does

The De-identification Engine is the service that **ingests real FHIR data, applies de-identification rules, and produces privacy-preserved output**. It is the primary interface for all data consumers. It:

- Enforces configurable de-identification profiles
- Orchestrates calls to the Pseudonymization Service (gPAS) for reversible pseudonymization
- Enforces access control and records every operation in the audit log
- Provides risk assessment and synthetic data generation for the resulting datasets

### 02.2 Container & Build Stages

```yaml
image:   medanon:latest
port:    8000 → 127.0.0.1:8000
user:    appuser (UID 1000, non-root)
fs:      read-only (tmpfs on /tmp, /output mounted as volume)
memory:  2 GB limit / 512 MB reservation
caps:    all dropped; no-new-privileges
health:  GET /health → {"status":"ok"}
```

**Build stages:**

| Stage | Purpose |
|---|---|
| `base` | System deps, Python 3.12, `pip install -r requirements.txt` |
| `prod` | Non-root user, copy source, `CMD ["uvicorn", "api.main:app", ...]` |
| `dev` | watchfiles hot-reload; source mounted via volume |
| `sdv` | Extends `base`; installs SDV synthetic engine (`requirements-sdv.txt`); same entry point as `prod` |

### 02.3 REST API Endpoints

#### Open Paths (no auth required)

| Method | Path | Response | Description |
|---|---|---|---|
| GET | `/health` | `{"status":"ok"}` | Liveness probe |
| GET | `/ready` | `{"ready":true/false}` | Readiness probe (checks gPAS, FHIR, NLP) |
| GET | `/metrics` | Prometheus text | Prometheus-format metrics |
| GET | `/docs` | HTML | Swagger UI |

Note: `/ready` returns full `checks` detail only to authenticated callers (prevents topology leakage).

#### Processing Endpoints (analyst role)

| Method | Path | Body | Output | Notes |
|---|---|---|---|---|
| POST | `/process` | JSON (resource or Bundle) | JSON | Synchronous |
| POST | `/process/raw` | JSON / NDJSON / XML | Specified format | `input_format`, `output_format` params |
| POST | `/process/ndjson` | NDJSON | NDJSON stream | One resource per line |
| POST | `/process/batch` | JSON / NDJSON / XML | NDJSON stream | Auto-detects format |
| POST | `/process/from-server` | JSON params | NDJSON stream | Fetches from remote FHIR server |
| POST | `/process/everything` | JSON params | NDJSON stream | FHIR `$everything` for one patient |
| POST | `/process/cohort` | JSON params | NDJSON stream | Search patients by condition code; fetch and de-identify `$everything` per patient |

**`/process/from-server` params:**

```json
{
  "server_url": "https://source-fhir.example.com/fhir",
  "resource_types": ["Patient", "Observation"],
  "token": "bearer-token-optional"
}
```

**`/process/everything` params:**

```json
{
  "server_url": "https://source-fhir.example.com/fhir",
  "resource_type": "Patient",
  "resource_id": "PAT-001"
}
```

#### Upload Endpoints (admin role)

| Method | Path | Notes |
|---|---|---|
| POST | `/process/and-upload` | De-identify a single resource and POST it to a target FHIR server |
| POST | `/process/round-trip` | Fetch source FHIR → de-identify → upload to target; streaming status |
| POST | `/process/bulk-export` | Initiate FHIR `$export` on source server; de-identify and stream NDJSON output |

#### Analytics Endpoints (analyst role)

| Method | Path | Notes |
|---|---|---|
| POST | `/analyse/risk` | k-anonymity + l-diversity + risk scores on de-identified resources |
| POST | `/generate/synthetic` | Synthetic FHIR Patient generation from de-identified input |

**`/generate/synthetic` query params:**

| Param | Default | Range | Description |
|---|---|---|---|
| `count` | 100 | 1–10 000 | Number of synthetic patients to generate |
| `seed` | — | any int | Random seed for reproducibility |
| `engine` | `auto` | `auto`, `sdv`, `stdlib` | Synthesis engine |
| `include_conditions` | `false` | bool | Also generate linked Conditions |
| `count_per_patient` | 2 | 0–10 | Max Conditions per synthetic Patient |

### 02.4 FHIRPath Rule Engine

The rule engine is implemented in `pipeline/processor.py`. It processes each FHIR resource independently:

```
resource (dict)
    │
    ├── For each rule in settings.rules (ordered):
    │     1. Compile FHIRPath expression (cached in module-level dict)
    │     2. find_nodes(resource, fhirpath_expr) → list of (path, value, parent, key)
    │     3. For each matched node:
    │           a. Check dedup set: skip if (path, action_category) already processed
    │           b. Dispatch to action handler(node, params, settings)
    │           c. Action modifies resource in place (or returns replacement value)
    │           d. Add (path, action_category) to dedup set
    │
    ├── Post-pass: rewrite_references(resource, id_map) — if enabled
    ├── Post-pass: rewrite_text_ids(resource, id_map) — if enabled
    └── Post-pass: attach_manifest(resource, fired_rules) — if MEDANON_MANIFEST_ENABLED
    │
    └─ Returns modified resource dict
```

**FHIRPath wildcards:**

| Expression | Matches |
|---|---|
| `*.id` | `id` on every resource type |
| `*.identifier.value` | `identifier[*].value` on every resource type |
| `*.subject.reference` | `subject.reference` on every resource type |
| `Patient.name.family` | Only `name[*].family` on Patient resources |

**Processing error policy:** Controlled by `processing_errors` in config (or `MEDANON_PROCESSING_ERRORS` env var):

- `raise` — any action error aborts the request (default for production)
- `skip` — log the error; continue with remaining rules

### 02.5 De-identification Actions

#### `redact`

Removes the field entirely (or sets it to a fixed blank value for required fields).

```yaml
action: redact
params:                  # optional
  replacement: "DELETED" # set value instead of deleting; defaults to null/delete
```

Use for: binary blobs (photo, attachment), whole-object fields you want gone.

#### `cryptohash`

Computes a deterministic one-way hash of the value. Preserves referential integrity (same input → same hash everywhere).

```yaml
action: cryptohash
```

Uses HMAC-SHA3-256 if `MEDANON_HASH_KEY` is set; plain SHA3-256 otherwise (warn: rainbow-table vulnerable).

#### `generalize`

Reduces the precision of a value according to the chosen strategy.

```yaml
action: generalize
params:
  strategy: date_year        # "2001-05-14" → "2001"
  # strategy: date_year_month  # "2001-05-14" → "2001-05"
  # strategy: zip_prefix       # "12345" → "123"
  # strategy: age_bracket      # age 47 → "45-49"
```

#### `perturb`

Adds a cryptographically random offset to a numeric or date value.

```yaml
action: perturb
params:
  max_days: 30       # for date fields: offset ±30 days
  max_value: 5.0     # for numeric fields: offset ± 5.0
```

Uses `secrets.randbelow()` (CSPRNG).

#### `substitute`

Replaces the field value with a fixed literal. The field remains present in the output.

```yaml
action: substitute
params:
  substitute_with: "[REDACTED]"
```

Use for: names, addresses, telecom values — when you need the field to exist but not contain real data.

#### `scrub_text`

Regex-based PHI tokenization inside text or XHTML fields. Replaces matched patterns with `[[PHI_N]]` tokens.

```yaml
action: scrub_text
params:
  mode: text         # text | html_tokenize
  patterns: all      # all | dates | ids | phones | ...
  extract_names: true
```

#### `nlp_detect`

Microsoft Presidio + spaCy NER entity detection. Replaces detected entities with `[[TYPE_N]]` tokens.

```yaml
action: nlp_detect
params:
  mode: tokenize
  html: true        # XHTML-safe (tokenizes only text nodes)
  threshold: 0.4    # confidence threshold (0.0 – 1.0)
```

Requires Presidio and `en_core_web_lg` model installed.

#### `encrypt` / `decrypt`

RSA-OAEP encryption/decryption for cross-study linkage use cases.

```yaml
action: encrypt
params:
  key_path: /keys/public.pem

action: decrypt
params:
  key_path: /keys/private.pem
```

Key path must be within `MEDANON_KEY_ALLOWED_DIRS`.

#### `gpas_pseudonymize` / `gpas_depseudonymize`

Reversible pseudonymization via the gPAS Governance Authority.

```yaml
action: gpas_pseudonymize
params:
  gpas_url: ${GPAS_URL}
  gpas_domain: ${GPAS_DOMAIN}
  gpas_operation: ${GPAS_OPERATION:-pseudonymizeAllowCreate}
  gpas_timeout_sec: ${GPAS_TIMEOUT_SEC:-30}
  gpas_basic_user: ${GPAS_BASIC_USER}
  gpas_basic_pass: ${GPAS_BASIC_PASS}
```

### 02.6 API Authentication & Role-Based Access

#### API Key Auth

```
MEDANON_API_KEY set?
  ├── No  →  Open mode — all callers get admin role
  └── Yes →  X-API-Key header required
               ├── Matches  → admin role
               └── Missing / wrong  → HTTP 401
```

#### Role Hierarchy

```
admin
  ├─ /process/and-upload
  ├─ /process/round-trip
  ├─ /process/bulk-export
  └─ (includes analyst)
       analyst
         ├─ /process, /process/raw, /process/ndjson, /process/batch
         ├─ /process/from-server, /process/everything, /process/cohort
         ├─ /analyse/risk
         ├─ /generate/synthetic
         └─ (includes viewer)
              viewer
                ├─ /health, /ready, /metrics
                └─ /docs, /openapi.json, /redoc
```

### 02.7 Request Middleware Stack

Applied to every request in this order:

| Middleware | Purpose | Configurable via |
|---|---|---|
| `auth_middleware` | Resolve auth context; enforce RBAC | `MEDANON_API_KEY` |
| `audit_middleware` | Log every request to structured JSON | `MEDANON_AUDIT_LOG_*` |
| `enforce_body_size` | Reject bodies > limit (both CL and chunked) | `MEDANON_MAX_BODY_BYTES` |
| `request_id_middleware` | Propagate/generate `X-Request-ID` | — |
| `metrics_middleware` | Prometheus request count + latency | — |
| CORS middleware | Allowlist origins | `MEDANON_CORS_ORIGINS` |
| Rate limiter | 30 req/min per IP (most endpoints) | `MEDANON_RATE_LIMIT_ENABLED` |

### 02.8 Command-Line Interface (CLI)

The CLI provides bulk processing without an HTTP server. Six subcommands:

| Subcommand | Purpose |
|---|---|
| `process` | Transform a local file (JSON/NDJSON/XML → JSON/NDJSON/XML) |
| `fetch` | Fetch from a FHIR server, de-identify, write NDJSON |
| `everything` | Fetch `$everything` for one patient, de-identify |
| `push` | Upload a local NDJSON file to a target FHIR server |
| `export` | Trigger FHIR `$export` (system or type level), de-identify, write NDJSON |
| `cohort` | Search by condition code, fetch `$everything` per patient, de-identify |

See [user-manual.md §4](user-manual.md#4-cli-reference) for full flag reference and examples.

---

## 03 — Data Consumers (FHIR Server, UI & Integrations)

These components **receive de-identified FHIR data** produced by the De-identification Engine and use it for research, analysis, or downstream processing.

### 03.1 HAPI FHIR Server

HAPI FHIR is the local HL7 FHIR R4 server. It serves as both a source of real data and a target for de-identified output in this stack.

#### Deployment

```yaml
image:   hapiproject/hapi:v7.6.0
port:    8080 → 127.0.0.1:8081
memory:  3 GB limit / 512 MB reservation
JVM:     -Xms256m -Xmx2048m
db:      H2 in-memory (switch to PostgreSQL for production persistence)
health:  Custom Java HealthCheck.class
```

#### Key Configuration (`services/fhir-server/config/application.yaml`)

```yaml
hapi:
  fhir:
    fhir_version: R4
    server_address: ${HAPI_SERVER_ADDRESS:http://localhost:8081/fhir}
    cors:
      allow_credentials: false
      allowed_origin:
        - http://medanon:8000
        - http://localhost:8000
        - http://localhost:8501
    default_page_size: 200
    max_page_size: 200
    narrative_enabled: false
```

#### Loading Test Data

```bash
# Import test patients
docker compose exec anonymizer python3 scripts/import_testbase.sh

# Or directly via FHIR REST API
curl -X POST http://localhost:8081/fhir/Patient \
     -H "Content-Type: application/fhir+json" \
     -d @patient.json
```

### 03.2 React Web Dashboard

The React UI is the primary operator-facing interface. Built with React, TypeScript, Shadcn/ui, and Tailwind CSS, it is served as a static SPA via nginx. The nginx layer also proxies API calls to backend services, eliminating CORS configuration.

#### Deployment

```yaml
image:   medanon-ui:latest (nginx:1.27-alpine)
port:    8501 → 127.0.0.1:8501
memory:  128 MB limit / 32 MB reservation
proxy:   /api/ → http://medanon:8000
         /fhir/ → http://hapi-fhir:8080/fhir
```

#### Page Descriptions

| Page | What the operator can do |
|---|---|
| **Patient Browser** | Search HAPI FHIR by name/ID; click to de-identify entire `$everything` bundle; download NDJSON |
| **Condition Browser** | Find patients by SNOMED/ICD condition code; select cohort; batch de-identify |
| **Process Resource** | Paste or upload any FHIR resource; choose profile; see original vs. de-identified side-by-side |
| **Batch Processing** | Upload NDJSON/JSON/XML file (up to 10 MB); select profile; stream results with progress indicator |
| **Risk Assessment** | Upload de-identified NDJSON; view k-anonymity groups, risk level, and l-diversity |
| **Synthetic Data** | Upload de-identified Patients; configure `count`, `seed`, `engine`; download synthetic cohort |
| **Status Dashboard** | Live health grid for all backend services; 30-second auto-refresh |

### 03.3 External System Integrations (EDC / FIWARE)

Two integration patterns are supported:
- **Pull-based file exchange** — MedAnon produces de-identified NDJSON via `/process/from-server`; registered as an EDC `HttpData` asset; consumer pulls under contract.
- **Real-time proxy** — Consumer EDC data plane POSTs to `/process/round-trip`; MedAnon fetches, de-identifies, and uploads. Not suitable for large datasets (EDC `HttpProxy` does not support chunked streaming).

See [connector-integration.md](connector-integration.md) for step-by-step EDC and FIWARE/NGSI-LD integration guides.

---

## 99 — Architecture Overview

### 99.1 End-to-End System Diagram

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                              OPERATOR / RESEARCHER                             │
└──────────────────┬──────────────────────────────┬──────────────────────────────┘
                   │  Browser (UI)                 │  curl / SDK / EDC Connector
                   ▼                               ▼
    ┌──────────────────────────┐   ┌───────────────────────────────────────────┐
    │  03 — Data Consumers     │   │    02 — De-identification Engine          │
    │   React Dashboard         │   │          MedAnon API (:8000)              │
    │   (:8501)                │   │                                           │
    │                          │   │  ┌─────────────────────────────────────┐  │
    │  • Patient Browser       │   │  │  Middleware Stack                   │  │
    │  • Condition Browser     │   │  │  auth → audit → body_size →         │  │
    │  • Process Resource      │◀──┼──│  request_id → metrics               │  │
    │  • Batch Processing      │   │  └────────────────┬────────────────────┘  │
    │  • Risk Assessment       │   │                   │                        │
    │  • Synthetic Data        │   │  ┌────────────────▼────────────────────┐  │
    │  • Status Dashboard      │   │  │  Rule Engine                        │  │
    └──────────────────────────┘   │  │  FHIRPath match → Action dispatch   │  │
                                   │  │  post-pass: rewrite_references      │  │
                                   │  └────────────────┬────────────────────┘  │
                                   │           ┌───────┴──────┐                │
                                   │           │              │                 │
                                   │  ┌────────▼─────┐  ┌────▼────────────┐   │
                                   │  │  gPAS client  │  │  FHIR client    │   │
                                   │  │  retry+cache  │  │  retry+paginate │   │
                                   │  │  circuit-bkr  │  │                 │   │
                                   │  └────────┬──────┘  └────┬────────────┘   │
                                   └───────────┼──────────────┼────────────────┘
                                               │              │
              ┌────────────────────────────────┘              │
              ▼                                                ▼
┌─────────────────────────────────┐          ┌───────────────────────────────┐
│  01 — Pseudonymization Service  │          │  03 — Data Consumers          │
│                                 │          │  HAPI FHIR Server (:8081)     │
│  gPAS (:8080)                   │          │                               │
│  WildFly + TTP-FHIR gateway     │          │  • Source: real patient data  │
│  • pseudonymizeAllowCreate      │          │  • Target: de-identified data │
│  • pseudonymize                 │          │  • FHIR R4 (HL7)              │
│  • dePseudonymize               │          │  • Paginated search           │
│  • Web UI for domain admin      │          │  • $everything operation      │
│                                 │          └───────────────────────────────┘
│  MySQL (:3306, internal)        │
│  • Pseudonym mappings (gpas)    │
│  • Users/roles (gras)           │
└─────────────────────────────────┘
```

### 99.2 Data Processing Flow

#### Standard De-identification

```
Real FHIR Data               De-identified Output
(Patient, Observation, ...)  (same structure, PII masked)

POST /process?config_profile=gpas
  │
  ├── Parse JSON/NDJSON/XML
  ├── Load config_gpas.yaml rules
  ├── For each resource, apply rules:
  │     Patient.id      → gpas_pseudonymize  → "PSEUDO-a1b2c3"
  │     Patient.name    → redact             → (removed)
  │     Patient.telecom → redact             → (removed)
  │     Patient.address → redact             → (removed)
  │     Patient.birthDate → generalize       → "1985"
  │     *.text          → scrub_text + nlp_detect  → tokenized
  │     *.id (all)      → gpas_pseudonymize  → deterministic pseudonyms
  ├── rewrite_references (Patient/PAT-001 → Patient/PSEUDO-a1b2c3)
  └── Return de-identified JSON
```

#### Structure-Preserving De-identification

```
Real Patient Bundle              De-identified Bundle (same fields)

POST /process?config_profile=structural
  │
  ├── Patient.id         → "PAT-001"     →  gpas_pseudonymize  → "PSEUDO-a1b2c3"
  ├── Patient.name.family→ "Smith"       →  substitute         → "[REDACTED]"
  ├── Patient.name.given → ["John","Jr"] →  substitute (each)  → ["[REDACTED]","[REDACTED]"]
  ├── Patient.telecom.value → "555-1234" →  substitute         → "[REDACTED]"
  ├── Patient.birthDate  → "1985-05-14"  →  generalize         → "1985"
  ├── Patient.gender     → "male"        →  (no rule)          → "male"  ← unchanged
  ├── Observation.id     → "OBS-001"     →  gpas_pseudonymize  → "PSEUDO-b2c3d4"
  ├── Observation.code   → LOINC code    →  (no rule)          → unchanged
  ├── Observation.value  → 7.2           →  (no rule)          → 7.2
  ├── rewrite_references: Observation.subject.reference → "Patient/PSEUDO-a1b2c3"
  └── Output: complete Bundle, all fields present
```

#### Risk Assessment → Synthetic Generation Pipeline

```
De-identified Patients (NDJSON)
  │
  ├── POST /analyse/risk
  │     Extract: (gender, birth_year, zip_3) per patient
  │     Group:   equivalence classes
  │     Output:  {"min_k":5, "risk_level":"low", ...}
  │
  └── POST /generate/synthetic?count=500&engine=sdv
        Learn distributions from real de-identified cohort
        Generate 500 synthetic patients (new UUIDs, sampled attributes)
        Tag: meta.tag[code=SYN]
        Output: 500-line NDJSON
```

### 99.3 Deployment Topologies

#### Local Development

```
localhost
  ├── :8000  MedAnon API
  ├── :8080  gPAS
  ├── :8081  HAPI FHIR
  └── :8501  React UI

All ports on 127.0.0.1 — local access only
Start: make up   (or: docker compose up -d)
```

#### Single-Node Production

```
Internet
  │  HTTPS (:443)
  ▼
Reverse Proxy (nginx / Caddy)
  ├── / → :8000 MedAnon API  (TLS terminated)
  ├── /fhir → :8081 HAPI FHIR
  ├── /ttp-fhir → :8080 gPAS API
  └── /gpas-web → :8080 gPAS UI

All Docker services on fhir-net bridge network
All ports bound to 127.0.0.1 (not reachable directly from outside)
```

#### Kubernetes

```
Ingress Controller
  ├── /           → anonymizer-svc:8000
  ├── /fhir       → fhir-server-svc:8080
  ├── /ttp-fhir   → gpas-svc:8080
  └── /gpas-web   → gpas-svc:8080

Namespace: medanon
  Deployments:   anonymizer, fhir-server, gpas
  StatefulSet:   gpas-db (MySQL, PVC)
  Services:      ClusterIP for all
  Secrets:       gpas-credentials, medanon-api-key, hash-key
  ConfigMaps:    anonymizer-config, fhir-server-config

Monitoring:
  Prometheus scrapes: /metrics (anonymizer), /actuator/prometheus (HAPI)
```

### 99.4 Production Deployment Checklist

See [RUNBOOK.md §12](RUNBOOK.md#12-security--go-live-checklist) for the full go-live checklist (secrets, network/TLS, logging, compliance, gPAS setup, load testing).
