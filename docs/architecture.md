# SPE FHIR BlackBox — Architecture

## Table of Contents

1. [Overview](#1-overview)
2. [Components](#2-components)
   - 2.1 [Common Components](#21-common-components)
   - 2.2 [Governance Authority — gPAS (Trusted Third Party)](#22-governance-authority--gpas-trusted-third-party)
   - 2.3 [De-identification Service — Anonymizer](#23-de-identification-service--anonymizer)
   - 2.4 [FHIR Data Layer — HAPI FHIR Server](#24-fhir-data-layer--hapi-fhir-server)
   - 2.5 [User Interface — Streamlit Dashboard](#25-user-interface--streamlit-dashboard)
3. [Data Flows](#3-data-flows)
   - 3.1 [Core Processing Pipeline](#31-core-processing-pipeline)
   - 3.2 [Inline De-identification](#32-inline-de-identification)
   - 3.3 [Server-to-Server Round-Trip](#33-server-to-server-round-trip)
   - 3.4 [Risk Assessment Chain](#34-risk-assessment-chain)
   - 3.5 [Synthetic Data Generation](#35-synthetic-data-generation)
4. [Security Architecture](#4-security-architecture)
5. [Deployment Architecture](#5-deployment-architecture)
   - 5.1 [Docker Compose (Single-Node)](#51-docker-compose-single-node)
   - 5.2 [Kubernetes / Helm (Multi-Node)](#52-kubernetes--helm-multi-node)
6. [Configuration Profiles](#6-configuration-profiles)
7. [API Surface](#7-api-surface)

---

## 1. Overview

**SPE FHIR BlackBox** (also referred to as *MedAnon*) is a rule-driven FHIR de-identification and pseudonymization engine for healthcare data. It accepts HL7 FHIR R4 resources in JSON, NDJSON, or XML format, applies configurable match-action rules from YAML profiles, and outputs transformed data in the same or a different format.

### Purpose

The system enables healthcare organisations and researchers to:

- **De-identify** patient records for regulatory compliance (GDPR, HIPAA Safe Harbor)
- **Pseudonymize** identifiers via a Trusted Third Party (gPAS) for reversible, longitudinal studies
- **Assess re-identification risk** using k-anonymity and l-diversity metrics
- **Generate synthetic data** that preserves statistical distributions without copying real records
- **Exchange data** through privacy-preserving dataspace connectors (EDC, FIWARE/NGSI-LD)

### Key Properties

| Property | Value |
|---|---|
| Input formats | JSON, NDJSON, FHIR Bundle, XML |
| Output formats | JSON, NDJSON, XML |
| FHIR version | R4 |
| Rule language | FHIRPath expressions in YAML |
| Interfaces | REST API, Web UI, CLI |
| Auth | API key + RBAC (optional) |
| Deployment | Docker Compose, Kubernetes (Helm) |

### High-Level Architecture Diagram

```
                         ┌─────────────────────────────────────────┐
                         │          OPERATOR / RESEARCHER           │
                         └───────┬──────────────────┬──────────────┘
                                 │ Browser / CLI     │ REST API
                                 ▼                   ▼
                    ┌────────────────────┐  ┌────────────────────┐
                    │  Streamlit UI      │  │  MedAnon API       │
                    │  (port 8501)       │  │  (port 8000)       │
                    └────────┬───────────┘  └────────┬───────────┘
                             │                       │
                             └──────────┬────────────┘
                                        │
                            ┌───────────▼───────────┐
                            │   Rule Engine          │
                            │   (FHIRPath + Actions) │
                            └───────────┬───────────┘
                            ┌──────────┤├──────────┐
                            │          │           │
                   ┌────────▼─────┐    │   ┌───────▼──────────┐
                   │  gPAS (TTP)  │    │   │  HAPI FHIR       │
                   │  (port 8080) │    │   │  (port 8081)     │
                   └────────┬─────┘    │   └──────────────────┘
                            │          │
                   ┌────────▼─────┐    │   ┌──────────────────┐
                   │  MySQL       │    └──▶│  External FHIR   │
                   │  (internal)  │        │  Servers         │
                   └─────────────-┘        └──────────────────┘
```

---

## 2. Components

### 2.1 Common Components

These cross-cutting elements are shared across all services or form the foundation on which each component is built.

#### Rule Engine

The rule engine is the heart of the system. Each de-identification profile is a YAML file containing an ordered list of rules:

```yaml
rules:
  - name: "descriptive rule name"
    match: "FHIRPath expression"   # e.g. Patient.name.family, *.id
    action: "action_name"           # redact | cryptohash | substitute | generalize | ...
    params:                         # action-specific parameters (optional)
      key: value
```

**FHIRPath evaluation** is performed by `fhirpathpy`; compiled expressions are cached in a module-level dict. Wildcards (`*.id`) match the field across all resource types.

**Actions available:**

| Action | Effect | Example use |
|---|---|---|
| `redact` | Delete field or set fixed blank value | Patient.photo |
| `cryptohash` | HMAC-SHA3-256 or SHA3-256 (one-way) | IDs in minimal/HIPAA profiles |
| `generalize` | Reduce precision — `date_year`, `date_year_month`, `zip_prefix` | birthDate, postalCode |
| `perturb` | CSPRNG random offset on dates/numbers | Lab values for research |
| `substitute` | Replace with a literal value | Name → `[REDACTED]` |
| `scrub_text` | Regex-based PHI tokenization in text/XHTML | Narrative fields |
| `nlp_detect` | Presidio NER entity detection and tokenization | Clinical notes |
| `encrypt` | RSA-OAEP encryption | Cross-study linkage |
| `decrypt` | RSA-OAEP decryption | Reverse encryption |
| `gpas_pseudonymize` | Reversible pseudonym via gPAS TTP | IDs in gPAS profile |
| `gpas_depseudonymize` | Reverse gPAS pseudonym to original | Recontact workflows |

#### Reference Rewriting

When `rewrite_references: true` is set in the config profile, the engine performs a post-processing pass over the entire Bundle:

1. Collect `{old_id → new_id}` for every resource processed.
2. Rewrite all `reference` fields (e.g. `"Patient/PAT-001"` → `"Patient/PSEUDO-f25c9686"`).
3. If `rewrite_text_ids: true`, replace literal ID strings in free-text fields.

This preserves FHIR referential integrity without adding explicit rules for every reference type.

#### Transformation Manifest

When `MEDANON_MANIFEST_ENABLED=true`, each processed resource carries a `meta.tag` entry that records which rules fired, which actions were applied, and which FHIRPath paths were matched. PHI values are never included. This supports GDPR Art. 30 accountability.

#### Shared Libraries

| Module | Role |
|---|---|
| `pipeline/io_formats.py` | Parse JSON / NDJSON / XML; serialize output; XXE-safe via `defusedxml` |
| `pipeline/config.py` | YAML loader; `${VAR:-default}` env-var interpolation; rule conflict detection |
| `utils/crypto.py` | RSA key management; `bounded_random()` (CSPRNG via `secrets.randbelow`) |
| `utils/fhirpath.py` | FHIRPath tree traversal helpers |
| `utils/metrics.py` | Prometheus counters + histograms |
| `utils/logging.py` | Request-ID context-var propagation |

#### Networking

All services communicate over an internal Docker bridge network (`fhir-net`). External ports are bound to `127.0.0.1` only. A reverse proxy (nginx, Caddy, or Traefik) is required for remote access and TLS termination.

---

### 2.2 Governance Authority — gPAS (Trusted Third Party)

gPAS is the **Trusted Third Party** (TTP) that holds the authoritative mapping between real identifiers and their pseudonyms. It is the governance layer of the system: no one else possesses the mapping table.

#### Role

- Stores and manages pseudonym domains (e.g., `TESTING`, `STUDY-42`)
- Accepts a real identifier; returns a consistent, opaque pseudonym
- Supports reversal (`dePseudonymize`) for authorised re-contact workflows
- Provides a web UI for domain administration

#### Architecture

```
MedAnon Anonymizer
  └─ integrations/gpas/dispatcher.py   ← adapter (pipeline calls)
       └─ integrations/gpas/client.py  ← HTTP client
            │  Retry (exp. backoff, default 2× @ 0.2 s)
            │  LRU cache (thread-safe, reduces TTP load)
            │  Circuit breaker (CLOSED → OPEN → HALF_OPEN)
            ▼
     gPAS WildFly container  (port 8080)
       └─ /ttp-fhir/fhir/gpas/$pseudonymizeAllowCreate
     gPAS MySQL container    (port 3306, internal only)
       └─ schemas: gpas (mappings), gras (users/roles)
```

#### Operations

| Operation | Behaviour | Config value |
|---|---|---|
| `pseudonymizeAllowCreate` | Create domain entry if missing (recommended) | Default |
| `pseudonymize` | Fail if identifier unknown to gPAS | Strict mode |
| `dePseudonymize` | Reverse pseudonym → original identifier | Re-contact |

#### Circuit Breaker States

```
CLOSED ──(threshold failures)──▶ OPEN ──(recovery timeout)──▶ HALF_OPEN
  ▲                                                                │
  └──────────────────(probe success)──────────────────────────────┘
```

Configurable via `GPAS_CB_FAILURE_THRESHOLD`, `GPAS_CB_RECOVERY_TIMEOUT_SEC`, `GPAS_CB_WINDOW_SEC`.

#### Authentication to gPAS

- **Bearer token (preferred):** `GPAS_TOKEN`
- **HTTP Basic (fallback):** `GPAS_BASIC_USER` + `GPAS_BASIC_PASS` (gRAS format: `user@ths`)

---

### 2.3 De-identification Service — Anonymizer

The Anonymizer is the core service of the stack. It exposes a FastAPI REST API, implements the rule engine, and orchestrates all de-identification, pseudonymization, risk assessment, and synthetic data workflows.

#### Internal Architecture

```
services/anonymizer/src/
├── api/
│   ├── main.py           ← FastAPI app; middleware stack; router registration
│   ├── auth.py           ← API-key auth; RBAC; structured audit log
│   ├── deps.py           ← Rate limiter; SSRF protection; config loader; shared helpers
│   └── routers/
│       ├── process.py    ← /process, /process/raw, /process/ndjson, /process/batch,
│       │                    /process/from-server, /process/everything,
│       │                    /process/and-upload, /process/round-trip
│       ├── analytics.py  ← /analyse/risk
│       ├── synthetic.py  ← /generate/synthetic
│       └── fhir_server.py
├── pipeline/
│   ├── config.py         ← YAML loader + env interpolation
│   ├── processor.py      ← Rule engine: FHIRPath match → action dispatch
│   └── io_formats.py     ← Multi-format parse + serialize
├── actions/              ← One file per action type
├── integrations/
│   ├── gpas/             ← gPAS client (dispatcher + HTTP client)
│   ├── fhir/             ← FHIR server client (fetch, upload, pagination)
│   └── nlp/              ← Presidio NER detector
└── analytics/
    ├── risk.py           ← k-anonymity / l-diversity metrics
    └── synthetic.py      ← Synthetic FHIR Patient generation
```

#### Middleware Stack (applied to every request)

```
Request → [auth_middleware]
        → [audit_middleware]
        → [enforce_body_size]    ← rejects bodies > MEDANON_MAX_BODY_BYTES (10 MB)
        → [request_id_middleware] ← propagates X-Request-ID
        → [metrics_middleware]   ← Prometheus counters + latency
        → Route handler
```

#### Processing Pipeline (per request)

```
HTTP body
  │
  ▼ parse_payload_bytes()         ← detect JSON / NDJSON / XML
  │
  ▼ _unwrap_to_resources()        ← flatten Bundle, list, or single resource
  │
  ▼ get_settings(profile)         ← load & cache YAML config (lru_cache, 8 slots)
  │
  ▼ asyncio.to_thread(process_data, ...) ← off event loop for CPU work
  │  └─ For each resource:
  │       For each rule:
  │         • Evaluate FHIRPath match
  │         • Dispatch matched nodes to action handler
  │         • Track processed paths (dedup)
  │  └─ Post-pass: rewrite_references (if enabled)
  │  └─ Post-pass: manifest tagging (if enabled)
  │
  ▼ Serialize (JSON / NDJSON / XML)
  │
  ▼ HTTP response (sync) or StreamingResponse (async NDJSON)
```

#### SSRF Protection

User-supplied `server_url` values are validated before any HTTP call:

- Non-`http(s)` schemes rejected (file://, gopher://, etc.)
- Raw IP addresses in private/loopback ranges rejected (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, ::1, fc00::/7)
- DNS names allowed — Docker service names (e.g., `hapi-fhir`) work correctly

Environment-variable URLs (set by administrators) bypass SSRF checks.

---

### 2.4 FHIR Data Layer — HAPI FHIR Server

HAPI FHIR acts as the primary data store and reference implementation for FHIR R4 in this stack. It serves as both the source of real patient data and (optionally) the target for de-identified output.

#### Role

- Store and serve HL7 FHIR R4 Patient, Observation, Condition, and other resources
- Support paginated search (`GET /fhir/Patient?_count=200`)
- Support `$everything` operations (`GET /fhir/Patient/{id}/$everything`)
- Provide CORS access to the Streamlit UI and Anonymizer

#### FHIR Client (in Anonymizer)

```
integrations/fhir/client.py
  • Paginated fetch (auto-follows Bundle.link[rel=next])
  • $everything operation
  • Resource upload (POST per resource type)
  • Exponential backoff retry (default 2× @ 0.3 s)
  • Configurable timeout per request
```

#### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FHIR_SOURCE_URL` | `http://hapi-fhir:8080/fhir` | Source server base URL |
| `FHIR_SOURCE_TOKEN` | — | Bearer token for source server |
| `FHIR_TARGET_URL` | — | Target server base URL |
| `FHIR_TARGET_TOKEN` | — | Bearer token for target server |
| `HAPI_SERVER_ADDRESS` | `http://localhost:8081/fhir` | Public HAPI URL (returned in responses) |

---

### 2.5 User Interface — Streamlit Dashboard

The Streamlit UI provides a browser-based operator interface for exploring patient data, triggering de-identification, and reviewing results. It wraps the MedAnon REST API.

#### Pages

| Page | Purpose |
|---|---|
| **Patient Browser** | Search patients in HAPI FHIR; de-identify complete `$everything` bundles |
| **Condition Browser** | Find patients by diagnosis (SNOMED/ICD codes); bulk de-identify |
| **Process Resource** | Paste or upload a single resource; view original vs. de-identified side-by-side |
| **Batch Processing** | Upload NDJSON/JSON/XML; stream de-identified output with progress bar |
| **Risk Assessment** | Upload de-identified resources; view k-anonymity, l-diversity, risk scores |
| **Synthetic Data** | Generate synthetic FHIR Patients preserving statistical distributions |
| **Status Dashboard** | Live health check for all backend services (30-second refresh) |

---

## 3. Data Flows

### 3.1 Core Processing Pipeline

```
INPUT
  │  JSON (single resource or Bundle)
  │  NDJSON (one resource per line)
  │  XML (defusedxml, XXE-safe)
  │
  ▼ io_formats.py — parse_payload_bytes()
  │
  ▼ io_formats.py — _unwrap_to_resources()  →  List[Dict]
  │
  ▼ pipeline/config.py — Settings(config_path)
  │  • Load YAML rules
  │  • Expand ${VAR:-default} environment variables
  │  • Check for rule conflicts
  │
  ▼ pipeline/processor.py — process_resource(resource, settings)
  │  For each rule (ordered):
  │    1. Compile / cache FHIRPath expression
  │    2. Evaluate match → list of matched nodes
  │    3. For each node: dispatch to action module
  │    4. Record (path, action_category) for dedup
  │  Post-pass:
  │    • rewrite_references (bundle cross-references)
  │    • rewrite_text_ids (literal IDs in free text)
  │    • Attach transformation manifest (if enabled)
  │
  ▼ io_formats.py — serialize(resources, format)
  │
OUTPUT
     Synchronous JSON response   OR   Streaming NDJSON
```

### 3.2 Inline De-identification

```
Client / UI
  │  POST /process   (Content-Type: application/json)
  ▼
  Anonymizer — parse → apply rules → serialize
  ▼
  HTTP 200 JSON (synchronous)
```

Best for single resources or small bundles. Config profile selected via `?config_profile=` query parameter.

### 3.3 Server-to-Server Round-Trip

```
POST /process/round-trip
  { source_server_url, target_server_url, resource_types, ... }
          │
          ├── FHIR client fetches paginated resources from source
          │     GET /fhir/Patient?_count=200
          │     GET /fhir/Patient?_page_token=...  (follows next links)
          │
          ├── Per resource: apply rules (asyncio.to_thread)
          │     → gPAS TTP called for gpas_pseudonymize rules
          │
          └── FHIR client uploads to target
                POST /fhir/Patient
                POST /fhir/Observation
                ...
          │
          ▼ Streaming NDJSON status lines
            {"status":"ok","resource":"Patient/PSEUDO-xxx"}
            {"status":"error","resource":"...","detail":"..."}
```

Requires **admin** role. Returns streaming NDJSON so the caller can monitor progress.

### 3.4 Risk Assessment Chain

```
POST /analyse/risk   (Content-Type: application/x-ndjson)
          │
          ├── Extract Patient quasi-identifiers:
          │     gender, birth_year (from birthDate), zip_3 (postalCode[:3])
          │
          ├── Group records by (gender, birth_year, zip_3) tuples
          │
          ├── Compute k-anonymity:
          │     min_k = smallest group size
          │
          ├── Compute risk scores:
          │     prosecutor_risk = 1 / min_k
          │     journalist_risk = max(1/k_i for each group)
          │     marketer_risk = num_groups / total_records
          │
          └── If Condition resources present:
                Correlate via subject.reference
                Compute l-diversity per equivalence class
          │
          ▼ JSON risk report
            { "summary": { "min_k": 5, "risk_level": "low", ... },
              "groups": [...] }
```

### 3.5 Synthetic Data Generation

```
POST /generate/synthetic?count=200&engine=auto
  (body: de-identified NDJSON of Patient resources)
          │
          ├── Extract distributions from de-identified Patients:
          │     gender_distribution, birth_year_distribution, zip_prefix_distribution
          │
          ├── Engine selection:
          │     auto  → SDV (GaussianCopulaSynthesizer) if installed, else stdlib
          │     sdv   → GaussianCopulaSynthesizer (multivariate correlations)
          │     stdlib → Weighted per-attribute sampling (zero dependencies)
          │
          ├── Generate `count` synthetic Patients
          │     • New UUIDs as IDs
          │     • Sampled attributes with ±2-year jitter on birth year
          │     • meta.tag[code=SYN] added to every synthetic resource
          │
          └── Optional: generate linked Conditions (include_conditions=true)
          │
          ▼ Streaming NDJSON
```

---

## 4. Security Architecture

### Authentication & Authorization

API key authentication is optional and toggled by the `MEDANON_API_KEY` environment variable:

| `MEDANON_API_KEY` set? | Behaviour |
|---|---|
| No | Open access — all callers receive `admin` role |
| Yes | `X-API-Key` header required on all endpoints except open paths |

Role hierarchy (each role includes all roles below it):

```
admin
  └─ analyst
       └─ viewer
```

| Role | Permitted endpoints |
|---|---|
| `viewer` | `/health`, `/ready`, `/metrics`, `/docs`, `/openapi.json` |
| `analyst` | All `/process/*`, `/analyse/risk`, `/generate/synthetic` |
| `admin` | `/process/and-upload`, `/process/round-trip` |

### Security Layers

| Layer | Mechanism |
|---|---|
| **API auth** | `X-API-Key` header; configurable |
| **RBAC** | Three-tier role system enforced per endpoint |
| **Audit log** | Structured JSON, `/output/audit.log`; rotating (10 MB × 5 backups) |
| **Body size limit** | `MEDANON_MAX_BODY_BYTES` (default 10 MB); both Content-Length and chunked |
| **SSRF protection** | Private IP blocklist + scheme whitelist (http/https only) |
| **XML safety** | `defusedxml` — prevents XXE injection |
| **Container hardening** | Non-root `appuser` (UID 1000); read-only filesystem; no-new-privileges; capabilities dropped; `/tmp` tmpfs |
| **Rate limiting** | `slowapi` (30 req/min per IP on most endpoints); optional via env var |
| **CORS** | Configured allowlist via `MEDANON_CORS_ORIGINS`; credentials: false |
| **Cryptographic RNG** | `secrets.randbelow()` (CSPRNG) for all random offsets |
| **Path traversal** | `_validate_key_path()` restricts RSA key access to allowlisted directories |
| **PHI in logs** | Processing errors logged without resource content; raw keys never logged |
| **Hash key warning** | Startup warning if `MEDANON_HASH_KEY` unset (SHA3-256 without HMAC is rainbow-table vulnerable) |
| **Network isolation** | All ports bind to `127.0.0.1`; only reverse proxy port exposed externally |

---

## 5. Deployment Architecture

### 5.1 Docker Compose (Single-Node)

```yaml
Services (fhir-net bridge network):

  gpas-db        port 3306  INTERNAL ONLY   MySQL 8.0
  fhir-server    port 8081  → 127.0.0.1     HAPI FHIR JPA
  gpas           port 8080  → 127.0.0.1     WildFly + gPAS
  anonymizer     port 8000  → 127.0.0.1     FastAPI (MedAnon)
  ui             port 8501  → 127.0.0.1     Streamlit
```

**Startup sequence** (health-check driven):
1. `gpas-db` — MySQL ready (~15 s)
2. `fhir-server` — HAPI ready (~30 s)
3. `gpas` — WildFly + gPAS ready (~90 s; depends on MySQL)
4. `anonymizer` — FastAPI ready (~10 s; depends on gPAS + FHIR)
5. `ui` — Streamlit ready (~10 s; depends on anonymizer)

**Resource requirements:**

| Service | RAM limit | RAM floor |
|---|---|---|
| anonymizer | 2 GB | 512 MB |
| fhir-server | 3 GB | 512 MB |
| gpas | 6 GB | 1 GB |
| gpas-db | 4 GB | 512 MB |
| ui | 512 MB | 128 MB |
| **Total** | **15.5 GB** | **~2.7 GB** |

Add ~800 MB to anonymizer if NLP model (`en_core_web_lg`) is loaded.

### 5.2 Kubernetes / Helm (Multi-Node)

**Chart layout:**

```
helm/
├── medanon/            ← umbrella chart
│   ├── Chart.yaml
│   ├── values.yaml     ← global overrides
│   └── templates/
│       └── ingress.yaml
└── charts/
    ├── anonymizer/     ← Deployment, Service, ConfigMap, Secret
    ├── fhir-server/    ← Deployment, Service, ConfigMap
    └── gpas/           ← Deployment + StatefulSet (MySQL), Services, Secrets
```

**Ingress path routing:**

| Path | Target service |
|---|---|
| `/` | `anonymizer` (port 8000) |
| `/fhir` | `fhir-server` (port 8080) |
| `/gpas-web` | `gpas` web UI (port 8080) |
| `/ttp-fhir` | `gpas` TTP-FHIR API (port 8080) |

**Security posture:**
- Secret checksums: Pods roll automatically when Secrets change
- Network policies: Each sub-chart restricts ingress to expected callers
- Non-root: Anonymizer UID 1000; `runAsNonRoot: true` on all pods
- Persistence: MySQL StatefulSet with PVC (survives pod restarts and upgrades)
- Monitoring: All pods annotated for Prometheus scraping (`/metrics` on anonymizer and gPAS; `/actuator/prometheus` on HAPI FHIR)

---

## 6. Configuration Profiles

Each profile is a YAML file under `services/anonymizer/config/`. The active profile is selected via the `?config_profile=` query parameter (API) or `--config` flag (CLI). If not specified, auto-selection applies: use `config_gpas.yaml` when `GPAS_URL` is set, else `config.yaml`.

| Profile key | File | Use case | ID strategy | Date strategy | gPAS required |
|---|---|---|---|---|---|
| `auto` | (auto-selected) | Default; selects based on env | — | — | — |
| `minimal` | `config.yaml` | Dev/test; no external services | cryptohash | regex scrub | No |
| `gpas` | `config_gpas.yaml` | Production; reversible pseudonyms | gpas_pseudonymize | year-only | **Yes** |
| `gdpr` | `config_gdpr_eu.yaml` | GDPR Art. 4(5) compliance | HMAC-SHA3-256 | year-only | No |
| `hipaa` | `config_hipaa_safe_harbor.yaml` | HIPAA Safe Harbor (45 CFR §164.514) | cryptohash | year-only; ZIP→3-digit | No |
| `research` | `config_research_pseudonymous.yaml` | IRB-grade longitudinal research | cryptohash | year-month | No |
| `structural` | `config_structure_preserving.yaml` | Preserve full FHIR structure; mask PII | gpas_pseudonymize | year-only | **Yes** |

### Structural Profile — Key Properties

The `structural` profile is designed for downstream consumers that require a complete FHIR structure:

- **Fields are never removed** — `action: substitute` replaces PII text with `[REDACTED]`; the field and its array structure remain
- **IDs are pseudonymized** via gPAS; cross-references are rewritten by `rewrite_references: true`
- **Clinical data is untouched** — no rules target codes, values, observations, conditions, or non-birthDate dates
- **birthDate** generalised to year only (`generalize: date_year`)
- **Binary blobs** (photo, attachment) are redacted (no meaningful text substitute)
- **Free text** (narrative, notes, comments) scrubbed inline via `scrub_text` + `nlp_detect`

---

## 7. API Surface

### Open Paths (no authentication required)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe — always 200 |
| GET | `/ready` | Readiness probe — checks gPAS, FHIR, NLP |
| GET | `/metrics` | Prometheus metrics |
| GET | `/docs` | Swagger UI |
| GET | `/openapi.json` | OpenAPI 3.0 schema |

### Processing Endpoints (analyst role minimum)

| Method | Path | Input | Output | Notes |
|---|---|---|---|---|
| POST | `/process` | JSON | JSON | Synchronous; single resource or Bundle |
| POST | `/process/raw` | Any format | Specified format | `input_format` + `output_format` query params |
| POST | `/process/ndjson` | NDJSON | NDJSON | Streaming; one resource per line |
| POST | `/process/batch` | JSON/NDJSON/XML | NDJSON | Unified batch endpoint |
| POST | `/process/from-server` | JSON params | NDJSON | Fetch from FHIR server; stream de-identified output |
| POST | `/process/everything` | JSON params | NDJSON | `$everything` for a single patient |

### Upload Endpoints (admin role required)

| Method | Path | Input | Output |
|---|---|---|---|
| POST | `/process/and-upload` | JSON (resource + target params) | JSON status |
| POST | `/process/round-trip` | JSON (source + target params) | NDJSON status stream |

### Analytics Endpoints (analyst role minimum)

| Method | Path | Input | Output |
|---|---|---|---|
| POST | `/analyse/risk` | NDJSON (de-identified resources) | JSON risk report |
| POST | `/generate/synthetic` | NDJSON (de-identified Patients) | NDJSON synthetic Patients |

### Common Query Parameters

- `config_profile` — Profile key (`auto`, `minimal`, `gpas`, `gdpr`, `hipaa`, `research`, `structural`)
- `/generate/synthetic?count=N` — Number of synthetic patients (1–10 000, default 100)
- `/generate/synthetic?engine=auto|sdv|stdlib` — Synthesis engine selection
- `/generate/synthetic?seed=N` — Random seed for reproducibility
- `/generate/synthetic?include_conditions=true` — Also generate linked Condition resources
