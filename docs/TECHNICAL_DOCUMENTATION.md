# SPE FHIR BlackBox — Comprehensive Technical Documentation

**Audience:** Architects, developers, and operations teams  
**Last updated:** 2026-06-05  
**Current phase:** Phase 5 (use-case validation, AI + Kubernetes)  
**Branch:** `Ph5_UseCase_AI_KUB`

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Overview](#2-architecture-overview)
3. [APIs & Interfaces](#3-apis--interfaces)
4. [Dependencies](#4-dependencies)
5. [Data Model](#5-data-model)
6. [Data & Processing](#6-data--processing)
7. [Security & Compliance](#7-security--compliance)
8. [Deployment & Infrastructure](#8-deployment--infrastructure)
9. [Operations](#9-operations)

---

## 1. System Overview

### What the system does

SPE FHIR BlackBox (marketed as **MedAnon**) is a FHIR R4 de-identification and pseudonymization engine. It sits between an identified clinical FHIR source (a hospital EHR or research repository) and downstream consumers (analytics pipelines, research databases, dataspace connectors) and transforms patient data so that individuals cannot be re-identified.

The system accepts FHIR resources in JSON, NDJSON, or XML format, applies a configurable set of match-action rules from a YAML profile, and returns transformed data with PII removed, pseudonymized, or generalized. It also supports HL7 v2 messages and DICOM files.

### Why it was built this way

Clinical data sharing is legally and ethically constrained. GDPR (Europe), HIPAA (US), and various national frameworks require that patient identifiers be removed before data leaves the clinical environment. Existing solutions either require expensive commercial licenses, are too opinionated about the transformation logic, or do not support reversible pseudonymization for follow-up linkage. MedAnon provides:

- **Rule-driven flexibility** — YAML profiles that any team can read and audit
- **Reversible pseudonymization** — via gPAS (a trusted third-party service), so de-identified data can be re-linked for adverse event investigation under controlled conditions
- **Compliance profiles** — seven pre-built profiles covering GDPR, HIPAA Safe Harbor, IRB research, and structural preservation
- **AI-assisted configuration** — natural language to YAML rule generation, PII leak detection, and regulatory gap analysis (Phase 4)

### Key use cases

| Use case | Description |
|---|---|
| **Research export** | Bulk export of de-identified patient cohorts to a research data warehouse |
| **Data space sharing** | De-identify FHIR resources before sharing through IDSA/FIWARE data connectors |
| **Real-time pipeline** | Per-request de-identification via REST API for streaming pipelines |
| **Compliance audit** | Score de-identified output for k-anonymity, utility preservation, and quality |
| **Pseudonymization hub** | Centralized TTP pseudonymization via gPAS for multi-site studies |
| **NLP scrubbing** | Free-text PHI removal from clinical narratives using Presidio NER |

---

## 2. Architecture Overview

### System Context (C4 Level 1)

```
┌──────────────────────────────────────────────────────────────────────────┐
│                          External Context                                │
│                                                                          │
│  ┌───────────┐    HTTP/REST    ┌─────────────────────────────────────┐  │
│  │  Browser  │───────────────►│                                     │  │
│  │  (React   │                │    SPE FHIR BlackBox (MedAnon)      │  │
│  │   SPA)    │                │    FHIR de-identification engine    │  │
│  └───────────┘                │                                     │  │
│                               └──────────────┬──────────────────────┘  │
│  ┌───────────┐    FHIR R4     │              │                          │
│  │  Source   │◄───────────────┘         FHIR R4                        │
│  │  FHIR     │  (reads identified        │                             │
│  │  Server   │   patient data)           ▼                             │
│  └───────────┘              ┌────────────────────────┐                 │
│                              │  Target FHIR Server    │                 │
│  ┌───────────┐    REST API   │  (de-identified data)  │                 │
│  │ API       │──────────────►│                        │                 │
│  │ Clients   │               └────────────────────────┘                 │
│  └───────────┘                                                           │
└──────────────────────────────────────────────────────────────────────────┘
```

The system isolates identified and de-identified data at the network and database level. Identified data never enters the same network segment as de-identified data.

---

### Container Architecture (C4 Level 2)

```
┌─────────── Browser / API Client ───────────────────────────────────────┐
│                       http://host:8501                                  │
└────────────────────────────┬───────────────────────────────────────────┘
                             │
                ┌────────────▼──────────────┐
                │     UI nginx (port 8501)  │  React SPA + reverse proxy
                │   /        → SPA files   │
                │   /api/*   → anonymizer  │
                │   /fhir/*  → source FHIR │
                │   /fhir-target/* → target│
                └────────────┬─────────────┘
                             │  /api/*
              ┌──────────────▼──────────────────────────────────┐
              │         Anonymizer (port 8000)                   │
              │         FastAPI de-identification engine         │
              │         Python 3.12 · uvicorn                   │
              └───┬──────────┬──────────┬───────────┬───────────┘
                  │          │          │           │
          ┌───────▼─────────────┐ ┌───────┐ ┌─────────────┐
          │  gateway (Traefik)  │ │ Redis │ │   app-db    │
          │  :8080 (gpas)       │ │ :6379 │ │ PostgreSQL  │
          │  :8200 (nlp)        │ └───────┘ └─────────────┘
          │  aliases: gpas-lb,  │
          │           nlp-lb    │
          └──┬──────────────┬───┘
             │              │
         ┌───▼─────┐  ┌─────▼─────┐
         │ gPAS ×N │  │  NLP ×N   │
         │ WildFly │  │ Presidio  │
         └────┬────┘  └───────────┘
              │
         ┌────▼────┐
         │ gpas-db │
         │ Postgres│
         └─────────┘

        ┌──── source-net (isolated) ─────────────┐
        │  hapi-fhir:8080  → hapi-postgres:5432  │
        │  (no host port — accessed via anonymizer│
        │   proxy endpoints only)                 │
        └────────────────────────────────────────┘

        ┌──── processing-net ────────────────────┐
        │  All services above + fhir-target      │
        │  hapi-fhir-target:8080                 │
        │       → hapi-target-postgres:5432      │
        └────────────────────────────────────────┘
```

**Why two networks?** The source FHIR server stores identified patient data. Isolating it on `source-net` means that only the anonymizer (which bridges both networks) can reach it. No other service — including the UI, analytics, or target FHIR — can make direct requests to the source. This satisfies physical separation requirements under GDPR Art. 25 (data minimization by design).

---

### Service Inventory

**Always-on services (14 total):**

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | `8000` | FastAPI de-identification engine |
| `worker` | `medanon:latest` | `9091` (metrics) | Dedicated async job worker |
| `ui` | `medanon-ui:latest` | `8501` | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | none (isolated) | Source FHIR R4 (identified data) |
| `hapi-db` | `postgres:16-alpine` | internal | PostgreSQL for source HAPI |
| `fhir-target` | `hapiproject/hapi:v7.6.0` | `8082` | Target FHIR R4 (de-identified data) |
| `hapi-target-db` | `postgres:16-alpine` | internal | PostgreSQL for target HAPI |
| `gateway` | `traefik:v3` | `8080` (gPAS), `8200` (NLP) | API gateway — Docker-provider service discovery; carries `gpas-lb` / `nlp-lb` network aliases for backward-compatible URLs |
| `gpas` | WildFly 38 + gPAS | via gateway | Reversible pseudonymization (TTP); scaled with `--scale gpas=N` |
| `gpas-db` | `postgres:16-alpine` | internal | gPAS pseudonym store |
| `app-db` | `postgres:16-alpine` | internal | Jobs, configs, subscriptions, staging |
| `redis` | `redis:7-alpine` | internal | Job queue (Redis Streams) + gPAS L2 cache |
| `analytics` | `medanon-analytics:latest` | `8100` | Risk analysis + synthetic data |
| `nlp` | `medanon-nlp:latest` | via gateway | Presidio NLP microservice; scaled with `--scale nlp=N` |

**Opt-in profiles (started with `--profile <name>`):**

| Container | Profile | Role |
|---|---|---|
| `gpas-db-replica` | `ha` | PostgreSQL streaming replica for gPAS HA |
| `minio` | `s3` | S3-compatible object storage for job results |
| `ollama` | `ai` | Local LLM inference for AI agents |

---

### Pipeline Architecture (C4 Level 3)

The de-identification pipeline runs inside the anonymizer service. Every resource passes through four ordered passes:

```
Input FHIR (JSON / NDJSON / XML)
        │
        ▼
  io_formats.py ──── Parse. XML uses defusedxml (prevents entity expansion attacks).
        │
        ▼
  config/service.py ─ Load YAML profile. LRU-cached per profile name (loaded once per process).
        │              ${VAR:-default} env interpolation at load time.
        ▼
  rule_matcher.py ─── Build per-resource-type rule index from FHIRPath expressions.
        │              Results cached per (resource_type, rules_hash).
        ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  PASS 1 — action_dispatcher.py                               │
 │                                                              │
 │  For each matched rule:                                      │
 │  • Stateless actions execute immediately:                    │
 │    redact, cryptohash, generalize, substitute, perturb,     │
 │    scrub_text, encrypt                                       │
 │  • NLP actions deferred → NlpWork list                       │
 │  • gPAS-bound values deferred → BatchWork list              │
 │                                                              │
 │  Neither NLP nor gPAS is called here. Batching is critical   │
 │  for throughput: 300 resources → 1 HTTP call each, not 300.  │
 └──────────────────────────┬───────────────────────────────────┘
                            │ NlpWork + BatchWork collected
                            ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  PASS 1.5 — nlp_orchestrator.py                              │
 │                                                              │
 │  Phase A: Extract unique texts from all NlpWork items        │
 │  Phase B: Deduplicate, POST /v1/detect/batch to nlp-lb       │
 │           (one HTTP call for all resources in the batch)     │
 │  Phase C: Per-resource token replacement with isolated       │
 │           token_state (deterministic within resource,        │
 │           unique across resources)                           │
 │                                                              │
 │  Fail-closed: NLP unavailable → [NLP_UNAVAILABLE] replaces  │
 │  all detected text. No PHI leaks on service failure.         │
 └──────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  PASS 2 — gpas_orchestrator.py                               │
 │                                                              │
 │  1. Check L1 local LRU cache (50K entries, per-process)      │
 │  2. Check L2 Redis cache (1h TTL, cross-replica)            │
 │  3. Uncached values → one POST to gpas-lb:8080              │
 │     ($pseudonymizeAllowCreate, batch FHIR Parameters)       │
 │  4. Write results back to resource fields                    │
 │  5. Populate L1 + L2 caches                                  │
 │                                                              │
 │  Cross-chunk dedup: seen_values set prevents re-fetching     │
 │  pseudonyms for values already seen in earlier chunks.       │
 └──────────────────────────┬───────────────────────────────────┘
                            │
                            ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  PASS 3+4 — post_processor.py + manifest.py                  │
 │                                                              │
 │  • Rewrite FHIR bundle references after ID changes          │
 │    (Patient/123 → Patient/psn-abc where IDs changed)        │
 │  • Replace pseudonym-changed IDs in free-text fields        │
 │  • Attach transformation manifest to meta.tag               │
 │    (when MEDANON_MANIFEST_ENABLED=true)                      │
 └──────────────────────────────────────────────────────────────┘
        │
        ▼
  io_formats.py ──── Serialize. Output format matches input or as requested.
        │
        ▼
Output de-identified FHIR
```

**Why staged + concurrent?** Each external service (gPAS, NLP) has per-call HTTP overhead. The match stage accumulates deferred work across all resources. The phi_detection and pseudonymize stages then each make one batch request to their respective services — concurrently, since they touch disjoint resource paths. This reduces hundreds of HTTP calls to 2–3 and overlaps the two batch calls instead of running them sequentially.

---

## 3. APIs & Interfaces

### Communication patterns

The anonymizer exposes a **REST API** (FastAPI, OpenAPI 3.1). All endpoints accept and return JSON. NDJSON is supported for bulk operations. Streaming responses use `text/event-stream` (Server-Sent Events) for AI agent outputs.

The anonymizer communicates with downstream services using:
- **gPAS**: FHIR R4 `Parameters` over HTTP (TTP-FHIR protocol)
- **NLP microservice**: REST JSON batch API
- **FHIR servers**: Standard FHIR R4 REST
- **Redis**: Redis protocol (XADD, XREADGROUP, XACK, HSET, ZADD)
- **PostgreSQL**: `psycopg2` with `ThreadedConnectionPool`

### REST API reference

#### Health & observability

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | None | Liveness check. Returns `{"status":"ok"}` immediately. Used by Docker healthcheck. |
| GET | `/ready` | None | Readiness check. Probes FHIR + gPAS with 5s timeout. Use to confirm stack is operational, not just started. |
| GET | `/metrics` | None | Prometheus metrics (counters + histograms for requests, gPAS, FHIR, jobs). |
| GET | `/docs` | None | Interactive Swagger UI. |

#### De-identification

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/process` | analyst | De-identify a single FHIR resource (JSON or XML body). |
| POST | `/process/batch` | analyst | De-identify NDJSON, Bundle, or XML list. Returns same format. |
| POST | `/process/ndjson` | analyst | NDJSON-only endpoint with streaming response. |
| POST | `/process/raw` | analyst | Raw text scrubbing without FHIR structure parsing. |
| POST | `/process/from-server` | analyst | Fetch from FHIR server + de-identify + stream response. |
| POST | `/process/everything` | analyst | Patient `$everything` de-identify for a single patient. |
| POST | `/process/and-upload` | analyst | Fetch + de-identify + upload to target FHIR server. |
| POST | `/process/round-trip` | analyst | Full round-trip: source → de-identify → target. |

Query parameter: `?config_profile=<name>` overrides the active config profile for any processing endpoint.

#### Async jobs

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/v1/jobs/bulk-export` | analyst | Submit async bulk export. Returns 202 + `job_id`. |
| POST | `/v1/jobs/cohort` | analyst | Submit async cohort export. Returns 202 + `job_id`. |
| GET | `/v1/jobs` | analyst | List jobs. Filters: `?status=`, `?type=`, `?limit=`, `?offset=`. |
| GET | `/v1/jobs/{id}` | analyst | Poll job status. |
| GET | `/v1/jobs/{id}/result` | analyst | Download completed job NDJSON. |
| DELETE | `/v1/jobs/{id}` | admin | Cancel / delete job. |

#### Scoring

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/v1/score` | analyst | Score a single de-identified resource (ad-hoc). |
| POST | `/v1/jobs/{id}/score` | analyst | Trigger scoring for a completed job. |
| GET | `/v1/jobs/{id}/score` | analyst | Retrieve cached composite score. |
| GET | `/v1/jobs/{id}/score/report` | analyst | Download Markdown audit report. |

#### Config profiles

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/v1/configs` | viewer | List all profiles (system + user-defined). |
| GET | `/v1/configs/{name}` | viewer | Fetch YAML for a named profile. |
| POST | `/v1/configs` | admin | Create a user-defined profile. |
| PUT | `/v1/configs/{name}` | admin | Replace rules of a user-defined profile. |
| DELETE | `/v1/configs/{name}` | admin | Delete a user-defined profile (system profiles are read-only). |

#### Analytics

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/analyse/risk` | analyst | k-anonymity + l-diversity assessment. Proxies to analytics service. |
| POST | `/generate/synthetic` | analyst | Generate synthetic FHIR patients. Proxies to analytics service. |

#### Processing history

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/v1/processing-runs` | analyst | List processing runs with scoring stats. |
| GET | `/v1/processing-runs/{id}` | analyst | Get a specific run with full score breakdown. |
| DELETE | `/v1/processing-runs` | admin | Purge processing run history. |

#### Specialty formats

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/process/dicom` | analyst | De-identify a single DICOM file. |
| POST | `/process/dicom/batch` | analyst | De-identify multiple DICOM files (multipart/form-data). |
| POST | `/process/hl7v2` | analyst | De-identify a single HL7 v2 message. |
| POST | `/process/hl7v2/batch` | analyst | De-identify a batch of HL7 v2 messages. |

#### FHIR subscriptions

| Method | Path | Role | Description |
|---|---|---|---|
| POST | `/fhir/Subscription` | analyst | Create a FHIR R4 Subscription (rest-hook only). |
| GET | `/fhir/Subscription/{id}` | analyst | Retrieve a subscription. |
| PUT | `/fhir/Subscription/{id}` | analyst | Update a subscription. |
| DELETE | `/fhir/Subscription/{id}` | analyst | Delete a subscription. |
| GET | `/fhir/Subscription` | admin | List all subscriptions. |

#### SMART on FHIR

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/.well-known/smart-configuration` | None | SMART capability discovery (RFC 8414). |
| POST | `/oauth2/introspect` | None | Token introspection (RFC 7662). |

#### AI agents (Phase 4)

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/v1/ai/status` | analyst | AI provider health + circuit breaker state. |
| POST | `/v1/ai/generate-config` | analyst | Generate a config profile from natural-language description. |
| POST | `/v1/ai/detect-pii` | analyst | Scan de-identified resources for PII leaks. |
| POST | `/v1/ai/explain` | analyst | Explain config rules in plain language (SSE streaming). |
| POST | `/v1/ai/compliance` | analyst | Regulatory gap analysis vs HIPAA / GDPR / other frameworks. |

#### Audit

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/v1/audit/events` | admin | Query recent audit events. Filters: `?count=`, `?event_type=`. |

---

### Request / Response contracts

**Authentication header** (when `MEDANON_API_KEY` is set):
```http
X-API-Key: <your-api-key>
```

**Single resource de-identification:**
```http
POST /process?config_profile=hipaa
Content-Type: application/json

{"resourceType": "Patient", "id": "123", "name": [{"family": "Smith"}], "birthDate": "1985-03-15"}
```
Response: transformed Patient resource, same shape.

**Async job submission:**
```http
POST /v1/jobs/bulk-export
Content-Type: application/json

{"source_url": "http://hapi-fhir:8080/fhir", "config_profile": "research"}
```
Response: `{"job_id": "abc123", "status": "pending"}`

**AI config generation:**
```http
POST /v1/ai/generate-config
Content-Type: application/json

{"prompt": "GDPR research study, keep birth year and diagnosis codes, remove all names and addresses", "regulation": "GDPR"}
```
Response: `{"profile": "...", "yaml": "rules:\n  - name: ...", "warning": null}`

---

## 4. Dependencies

### Internal services (self-hosted)

| Service | Technology | Why it is used |
|---|---|---|
| **gPAS** | WildFly 38 + Java EE, PostgreSQL 16 | Reversible pseudonymization via TTP model. Audited, persistent pseudonym mappings. Developed by University Medicine Greifswald. |
| **HAPI FHIR** | Spring Boot, PostgreSQL 16 | FHIR R4 reference server. Used as both source (identified) and target (de-identified). |
| **NLP microservice** | Python + Presidio + spaCy `en_core_web_lg` | Named-entity recognition for free-text PHI. Isolated to avoid loading an 800 MB spaCy model into the anonymizer process. |
| **Analytics microservice** | Python + pandas + scipy | k-anonymity, l-diversity, and synthetic data generation. Isolated because SDV (synthetic data vault) adds ~2 GB of dependencies. |
| **Redis 7** | redis:7-alpine | Job queue (Redis Streams + consumer groups, at-least-once delivery) + gPAS L2 cross-replica cache. Password-protected. |
| **PostgreSQL 16** (app-db) | postgres:16-alpine | Application state: jobs, config profiles, subscriptions, two-phase staging. |
| **Ollama** (opt-in) | ollama/ollama | Local LLM inference for AI agents. Activated with `--profile ai`. |
| **MinIO** (opt-in) | minio/minio | S3-compatible object storage for job result NDJSON files. |

### Key Python libraries (anonymizer)

| Library | Version pin | Purpose |
|---|---|---|
| `fastapi` | exact pin | HTTP framework |
| `uvicorn` | exact pin | ASGI server |
| `pydantic` | exact pin | Request/response validation |
| `presidio-analyzer` + `presidio-anonymizer` | exact pin | NLP entity detection (local fallback, always installed) |
| `spacy` | exact pin | spaCy NLP engine |
| `fhirpathpy` | exact pin | FHIRPath expression evaluation |
| `defusedxml` | exact pin | Safe XML parsing (prevents entity expansion) |
| `psycopg2-binary` | exact pin | PostgreSQL driver |
| `redis` | exact pin | Redis client |
| `slowapi` | exact pin | Rate limiting |
| `prometheus-client` | exact pin | Metrics exposition |
| `litellm` | floor pin (`>=1.63.0`) | LLM provider abstraction for AI agents |
| `cryptography` | exact pin | RSA key operations |

All production dependencies use **exact version pins** to ensure reproducible builds. The `litellm` floor pin is a known deviation (tracked in the architecture audit).

### External integration points

| Integration | Protocol | When required |
|---|---|---|
| Source FHIR server | FHIR R4 REST | `process/from-server`, `process/and-upload`, `bulk-export`, `cohort` |
| Target FHIR server | FHIR R4 REST | `process/and-upload`, `bulk-export` upload phase |
| gPAS TTP service | TTP-FHIR (`$pseudonymizeAllowCreate`) | `gpas_pseudonymize` action in config profiles |
| AI provider (LLM) | OpenAI-compatible REST or Ollama | `/v1/ai/*` endpoints when `MEDANON_AI_ENABLED=true` |

---

## 5. Data Model

### Core entities

#### Job
An async processing task. Stored in Redis (production), PostgreSQL, or SQLite (dev).

```
Job
├── id: str           — UUID v4, globally unique
├── type: str         — "bulk-export" | "cohort" | "patient-export" | ...
├── status: enum      — pending → running → done | failed | cancelled
├── config_profile: str
├── source_url: str
├── created_at: datetime
├── updated_at: datetime
├── error: str | null — failure message when status=failed
└── result_path: str  — path to NDJSON output when status=done
```

#### ProcessingRun
A record of a completed de-identification operation with scoring data.

```
ProcessingRun
├── id: str
├── endpoint: str         — e.g. "/process/batch"
├── config_profile: str
├── resource_count: int
├── composite_score: float — privacy × utility × quality (0.0–1.0)
├── privacy_score: float
├── utility_score: float
├── quality_score: float
├── created_at: datetime
└── manifest: JSON        — per-rule transformation summary
```

#### StagedResource
Intermediate storage for two-phase bulk operations.

```
StagedResource
├── job_id: str
├── resource_type: str
├── resource_id: str
├── payload: JSONB        — de-identified FHIR resource
├── tier: int             — topological upload tier
└── created_at: datetime
```

#### ConfigProfile
A user-defined de-identification rule set.

```
ConfigProfile
├── name: str             — slug (lowercase, hyphens)
├── description: str
├── yaml_content: text    — YAML rule profile
├── is_system: bool       — system profiles are read-only
└── created_at: datetime
```

#### FHIRSubscription
A FHIR R4 rest-hook subscription.

```
FHIRSubscription
├── id: str
├── status: str           — "requested" | "active" | "error"
├── endpoint: str         — webhook URL
├── criteria: str         — FHIR search expression
└── created_at: datetime
```

### PostgreSQL schema (app-db)

All tables live in the `medanon` schema, created by `services/anonymizer/sql/init.sql`.

```sql
-- Jobs
medanon.jobs (id, type, status, config_profile, source_url, error, result_path, created_at, updated_at)

-- Two-phase staging for bulk operations
medanon.staged_resources (job_id, resource_type, resource_id, payload JSONB, tier, created_at)
-- Index: (job_id, resource_type) for efficient bulk retrieval

-- Config profiles
medanon.configs (name, description, yaml_content, is_system, created_at)

-- FHIR subscriptions
medanon.subscriptions (id, status, endpoint, criteria, created_at)

-- Processing runs (scoring history)
medanon.processing_runs (id, endpoint, config_profile, resource_count, composite_score,
                          privacy_score, utility_score, quality_score, manifest JSONB, created_at)
```

---

## 6. Data & Processing

### Data lifecycle

```
1. INGEST
   Clinical FHIR data lives in source HAPI FHIR (identified, source-net).
   Accessed only by anonymizer/worker — no other service has network access.

2. DE-IDENTIFY
   Anonymizer applies YAML rules via the 4-stage pipeline.
   Each resource is transformed in memory; original data is never modified.
   For bulk operations, de-identified resources are staged in PostgreSQL (app-db).

3. STORE
   De-identified resources written to:
   a. Target HAPI FHIR (via batch Bundle PUT)
   b. NDJSON files in MEDANON_OUTPUT_DIR (default: /output)
   c. MinIO S3 when MEDANON_RESULT_STORAGE=s3

4. SCORE
   Composite score computed (privacy × utility × quality).
   Score + manifest stored in medanon.processing_runs.

5. EXPIRE
   NDJSON result files: TTL set by MEDANON_RESULT_TTL_SEC.
   Processing runs: retained indefinitely (purge via DELETE /v1/processing-runs).
   Staged resources: retained for MEDANON_STAGING_RETENTION_DAYS then deleted.
   gPAS L2 Redis cache: 1h TTL per entry.
```

### Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Data Flow — Bulk Export                          │
└─────────────────────────────────────────────────────────────────────────┘

Clinician / EHR
      │ stores FHIR resources
      ▼
┌─────────────┐      ┌──────────────────────────────────────────────────┐
│ Source FHIR │      │ POST /v1/jobs/bulk-export                        │
│ (identified)│      │         │                                         │
│ source-net  │      │         ▼                                         │
└──────┬──────┘      │  Worker picks up job (XREADGROUP)                │
       │             │         │                                         │
       │ GET /fhir/  │         ▼                                         │
       │ Patient?    │  PHASE 1: Fetch resource types from /metadata    │
       │ _count=500  │         │                                         │
       │◄────────────│         ▼                                         │
       │             │  PHASE 2: For each type, fetch pages             │
       │ 500 Patients│   → Run 4-stage de-identification pipeline       │
       │────────────►│   → Stage de-identified resources in app-db     │
       │             │         │                                         │
       │             │         ▼                                         │
       │             │  PHASE 3: Build reference graph, Bellman-Ford   │
       │             │           topological sort (tier 0, 1, 2, ...)  │
       │             │           Rewrite IDs in cross-references        │
       │             │         │                                         │
       │             │         ▼                                         │
       │             │  PHASE 4: Upload to target FHIR by tier         │
       │             │   → PUT batch Bundle (200 resources/chunk)      │
       │             │         │                                         │
       │             │         ▼                                         │
       │             │  PHASE 5: Write NDJSON to /output/{job_id}      │
       │             │   → job.status = "done"                         │
       │             └──────────────────────────────────────────────────┘
       │                                    │
       │                                    ▼
       │                     ┌─────────────────────────┐
       │                     │  Target FHIR (de-id)    │
       │                     │  (de-identified data)   │
       │                     └─────────────────────────┘
       │
       └── Source FHIR never modified. Original data preserved.
```

### De-identification actions

| Action | Mechanism | Reversible | Use case |
|---|---|---|---|
| `redact` | Replace value with fixed string (default `""`) | No | Names, addresses, free text |
| `cryptohash` | HMAC-SHA3-256 (keyed) or plain SHA3-256 | No | IDs needing consistency without re-linking |
| `gpas_pseudonymize` | gPAS TTP reversible pseudonym | Yes (TTP) | Patient IDs in research requiring follow-up |
| `encrypt` | RSA public-key encryption | Yes (private key) | Values needing encryption by authorized parties |
| `generalize` | Date → year/year-month; zip → 3-digit prefix; age → bracket | No | Dates, geographic, quasi-identifiers |
| `substitute` | Replace with fixed `substitute_with` value | No | FHIR code binding constraints (e.g. gender → "unknown") |
| `perturb` | Add bounded random noise (CSPRNG) | No | Numeric values (lab results, dosage) |
| `scrub_text` | Regex-based PHI pattern removal | No | Structured PHI in text (phone, SSN, NPI) |
| `nlp_scrub` | NLP entity detection + token substitution | No | Free-text clinical narratives |
| `nlp_detect_act` | NLP + per-entity-type action routing | Configurable | Fine-grained field control |
| `gpas_depseudonymize` | gPAS reverse lookup | Yes (TTP) | Re-linkage under authorization |

### Config profiles

Seven selectable built-in profiles cover the main regulatory scenarios:

| Profile | ID handling | Dates | Free text | gPAS required | Regulation |
|---|---|---|---|---|---|
| `config.yaml` | SHA3-256 hash | Year only | Regex scrub | No | Local dev / testing |
| `config_gpas.yaml` | gPAS pseudonym | Year only | NLP scrub | Yes | Production (GDPR compatible) |
| `config_gdpr_eu.yaml` | HMAC SHA3-256 | Redacted | Redacted | No | GDPR Art. 4(5) |
| `config_hipaa_safe_harbor.yaml` | Redacted | Year only | Regex scrub | No | US HIPAA Safe Harbor |
| `config_research_pseudonymous.yaml` | SHA3-256 hash | Year-month | NLP scrub | No | IRB research |
| `config_structure_preserving.yaml` | gPAS pseudonym | Year (birthDate) | Preserved | Yes | Downstream FHIR consumers |
| `config_value_masking.yaml` | gPAS pseudonym | Decade/year | `nlp_detect_act` | Yes | Field-complete with entity routing |

---

## 7. Security & Compliance

### Authentication and authorization

The anonymizer uses **API key authentication** with role-based access control.

| `MEDANON_API_KEY` env var | Behavior |
|---|---|
| Unset | All endpoints open. Suitable for local development only. |
| Set | All endpoints except `/health`, `/ready`, `/metrics`, `/docs` require `X-API-Key: <value>`. |

**RBAC roles:**

| Role | Access |
|---|---|
| `admin` | All endpoints including DELETE, purge, subscription list, audit events. |
| `analyst` | Processing, jobs, analytics, scoring, configs (read + create). |
| `viewer` | Read-only: config list/fetch, job status polling. |

Role assignment is static per API key — the key encodes the role. Parameterized paths (e.g. `/v1/jobs/{id}`) are resolved by prefix matching in `ENDPOINT_ROLE_PREFIXES` in `api/auth.py`.

### Encryption

**In transit:**
- All inter-service communication is within Docker's `processing-net` (private). Source FHIR is on the isolated `source-net`.
- For external exposure, a reverse proxy (nginx, Caddy, Traefik) terminates TLS in front of port 8501 (UI) and 8000 (API). The system never terminates TLS itself.
- The gPAS web UI port (8080) must never be publicly accessible.

**At rest:**
- gPAS pseudonym mappings are stored in `gpas-db` (PostgreSQL, encrypted volume at OS level).
- Job results (NDJSON) are stored on `MEDANON_OUTPUT_DIR` (filesystem) or MinIO (S3 AES-256 when configured).
- The `encrypt` action produces RSA-encrypted ciphertext in FHIR fields — only the private key holder can decrypt.
- HMAC keys (`MEDANON_HASH_KEY`) and RSA private keys must be stored outside the container (mounted as read-only volumes or injected as env vars from a secrets manager).

**Key management:**
- `MEDANON_HASH_KEY` — 32-byte hex key for HMAC-SHA3-256. Rotating this key breaks pseudonym consistency for all existing `cryptohash` output.
- RSA keypair — generated with `openssl genrsa`. Rotating the private key makes all previously encrypted values unreadable.
- All secret files are in `.gitignore`. Never commit `.env` or key files.

### Input validation and SSRF protection

- **Body size cap**: `MEDANON_MAX_BODY_BYTES` (default 10 MB). Requests exceeding this return 413.
- **SSRF guard**: `api/deps.py` blocks RFC-1918 private IP ranges (10.x, 172.16–31.x, 192.168.x) in `server_url` parameters. Prevents the anonymizer from being used as a proxy to internal services.
- **XML safety**: All XML parsing uses `defusedxml`, which prevents XML entity expansion (billion-laughs attack) and external entity inclusion.
- **Rate limiting**: `slowapi` enforces per-IP rate limits. AI endpoints limited to 10 requests/minute.

### Logging and auditing

**What is logged:**
- Timestamp, HTTP method, path, status code, request ID, auth subject, auth method
- Job lifecycle events (created, started, completed, failed)
- gPAS circuit breaker state changes
- NLP failures and fallback activations
- PII gate detections (count only, no content)

**What is never logged:**
- PHI or PII from FHIR resources
- API keys or credentials
- De-identified resource content

**Audit log format** (structured JSON, one line per event):
```json
{"timestamp": "2026-04-21T14:32:00Z", "method": "POST", "path": "/process", "status": 200, "request_id": "abc123", "subject": "analyst-key", "auth": "api_key"}
```

Logs are written to `MEDANON_AUDIT_LOG_FILE` (default: `/output/audit.log`) with rotation (10 MB, 5 backups). Optionally appended to a Redis Stream (`medanon:audit`) for centralized collection.

**Enable for production:**
```bash
MEDANON_MANIFEST_ENABLED=true     # GDPR Art. 30: tag each resource with applied rules
MEDANON_AUDIT_LOG_FILE=/output/audit.log
LOG_LEVEL=INFO                    # DEBUG may log resource content containing PHI
```

### Security hardening (containers)

All production containers apply:
- `read_only: true` filesystem + `tmpfs` for writable scratch space
- `cap_drop: ALL` (no Linux capabilities retained)
- `security_opt: no-new-privileges`

Exceptions with rationale:
- `app-db` (PostgreSQL): `cap_drop` omitted — PostgreSQL requires `CAP_CHOWN` and `CAP_SETUID` at startup for `chown` on data directory
- `ollama`: `read_only` omitted — Ollama writes downloaded model files to volume during runtime

### GDPR considerations

| Requirement | How it is addressed |
|---|---|
| **Art. 4(5) Pseudonymization** | `config_gdpr_eu.yaml` applies HMAC-SHA3-256 to all identifiers. `config_gpas.yaml` uses gPAS TTP pseudonymization. |
| **Art. 25 Data minimization by design** | Source FHIR isolated on `source-net`. Identified and de-identified data never share a database. |
| **Art. 30 Records of processing** | `MEDANON_MANIFEST_ENABLED=true` tags each output resource with the applied rule set. Processing runs stored in `medanon.processing_runs`. |
| **Art. 17 Right to erasure** | De-identified resources use pseudonyms or hashes; original IDs are not stored in the de-identified output. gPAS pseudonym mappings can be deleted to sever the linkage. |
| **Art. 32 Security of processing** | API key auth, TLS in transit (via reverse proxy), HMAC keys, audit log, no PHI in logs, container hardening. |
| **Data subject categories** | Health data (FHIR Patient, Condition, Observation, Encounter, MedicationRequest, etc.). Classified as special category data under Art. 9. |
| **Processing purposes** | Medical research, clinical analytics, data space integration, federated learning. Document in your own ROPA. |
| **Retention** | `MEDANON_RESULT_TTL_SEC` controls NDJSON output TTL. `MEDANON_STAGING_RETENTION_DAYS` controls staging retention. Raw identified data on source FHIR is under the clinical institution's own retention policy. |

### Compliance profiles

| Regulation | Built-in profile | Key properties |
|---|---|---|
| GDPR Art. 4(5) | `config_gdpr_eu.yaml` | HMAC pseudonymization, all direct identifiers redacted |
| HIPAA Safe Harbor | `config_hipaa_safe_harbor.yaml` | 18 PHI categories removed, dates → year only, zip → 3-digit |
| IRB Research | `config_research_pseudonymous.yaml` | Dates → year-month, IDs hashed for longitudinal linkage |
| Full FHIR structure | `config_structure_preserving.yaml` | IDs via gPAS, PII → `[REDACTED]`, structure preserved |
| Value masking | `config_value_masking.yaml` | Entity-specific NLP routing, encrypt + generalize combos |

---

## 8. Deployment & Infrastructure

### Environments

| Environment | Configuration | Notes |
|---|---|---|
| **Local dev** | `make setup` + uvicorn `--reload` | Run the anonymizer from `.venv`; NLP/gPAS/analytics degrade gracefully when unset |
| **Docker Compose (staging/prod)** | `make up` | Full stack, persistent PostgreSQL volumes, Redis, gPAS |
| **Kubernetes / K3s** | `make helm-install` | Helm umbrella chart, HPA for worker, Traefik or nginx ingress |

### Docker Compose setup

**Prerequisites:**

| Requirement | Minimum |
|---|---|
| Docker Engine | 24.x |
| Docker Compose plugin | v2.x |
| RAM | 8 GB (12 GB+ recommended) |
| Disk | 10 GB (images + volumes) |

**Step 1 — Environment configuration:**

```bash
cp .env.example .env
# Edit .env — generate secrets:
openssl rand -hex 32       # → MEDANON_HASH_KEY
openssl rand -base64 24    # → GPAS_BASIC_PASS, GPAS_DB_PASSWORD, MEDANON_REDIS_PASSWORD, etc.
```

Key variables that must be set before production use:

| Variable | Purpose |
|---|---|
| `MEDANON_HASH_KEY` | HMAC key for cryptohash. Without this, SHA3-256 is used (rainbow-table reversible). |
| `MEDANON_API_KEY` | API authentication. Leave blank only for local dev. |
| `GPAS_BASIC_PASS` | gPAS admin password. Rotate from default. |
| `MEDANON_REDIS_PASSWORD` | Redis authentication. Required in production. |
| `EXTERNAL_HOST` | Host/IP browsers use to reach this server. Used in CORS + HAPI address. |

**Step 2 — Build images:**

```bash
make build
```

Builds `medanon:latest` (anonymizer, 4 stages: base/prod/dev/sdv) and `medanon-ui:latest` (React + nginx). gPAS and HAPI use upstream images.

**Step 3 — Start the stack:**

```bash
make up          # start all services
docker compose ps  # wait until all show "healthy"
```

gPAS (WildFly) takes ~90 seconds on first boot.

**Step 4 — Initialize gPAS domain:**

```bash
make init-domains
```

Or manually via `http://localhost:8080/gpas-web/`. Always use the gPAS API — never insert domain rows directly into PostgreSQL. gPAS maintains an in-memory `domainLocks HashMap` that is only populated via its own REST API; direct SQL inserts bypass this and cause "domain not found" at runtime.

**Step 5 — Verify:**

```bash
curl http://localhost:8000/health   # {"status":"ok"}
curl http://localhost:8000/ready    # {"ready":true,...}
open http://localhost:8501          # React UI
```

### Opt-in profiles

```bash
docker compose --profile analytics up   # analytics microservice
docker compose --profile nlp up         # NLP microservice (Presidio, ~800 MB image)
docker compose --profile ha up          # gPAS PostgreSQL read replica
docker compose --profile s3 up          # MinIO S3 for job results
docker compose --profile ai up          # Ollama local LLM for AI agents
```

### Horizontal scaling

```bash
# Scale gPAS to 3 replicas (round-robin via gpas-lb)
docker compose up -d --scale gpas=3

# Scale NLP to 4 replicas (least-conn via nlp-lb)
docker compose up -d --scale nlp=4
```

The nginx load balancers use Docker DNS (`resolver 127.0.0.11`) for dynamic discovery — scaling takes effect without restarting the load balancer.

### Deployment Diagram

```
┌─────────────────── Host / VM ──────────────────────────────────────────┐
│                                                                         │
│  ┌─────────────────── processing-net (Docker bridge) ───────────────┐  │
│  │                                                                    │  │
│  │  medanon-ui:8501       medanon:8000      medanon-worker           │  │
│  │  (nginx + React SPA)   (FastAPI)         (same image, job loop)   │  │
│  │       │                    │  │  │                                 │  │
│  │       │           ┌────────┘  │  └──────────────────┐             │  │
│  │       │           │           │                      │             │  │
│  │       │     gpas-lb:8080  app-db:5432           redis:6379         │  │
│  │       │     (nginx)       (PostgreSQL)          (Redis 7)          │  │
│  │       │        │                                                    │  │
│  │       │   gpas ×N:8080                                             │  │
│  │       │   (WildFly 38)                                             │  │
│  │       │        │                                                    │  │
│  │       │   gpas-db:5432                                             │  │
│  │       │   (PostgreSQL)                                             │  │
│  │       │                                                            │  │
│  │       │   analytics:8100   nlp-lb:8200                            │  │
│  │       │                        │                                   │  │
│  │       │                   nlp ×N:8200                             │  │
│  │       │                   (Presidio)                              │  │
│  │       │                                                            │  │
│  │  fhir-target:8080 → hapi-target-db:5432                          │  │
│  │                                                                    │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌─────────── source-net (isolated Docker bridge) ───────────────────┐  │
│  │  fhir-server:8080 → hapi-postgres:5432                           │  │
│  │  (only anonymizer/worker bridge both networks)                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  Host ports exposed: 8000 (API), 8082 (target FHIR), 8080 (gPAS UI),  │
│                       8200 (NLP), 8501 (UI), 8100 (analytics)          │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Kubernetes / Helm

**Chart structure:**

```
helm/
├── medanon/           # Umbrella chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       └── ingress.yaml
└── charts/
    ├── anonymizer/    # Deployment + Service + ConfigMap + Secret
    ├── worker/        # Deployment + HPA (1–5 replicas, CPU 70%)
    ├── fhir-server/   # Source + target FHIR (deployed twice via alias)
    ├── gpas/          # StatefulSet + PostgreSQL StatefulSet + LB Service
    ├── ui/            # Deployment + Service + ConfigMap (nginx) + NetworkPolicy
    └── analytics/     # Optional (condition: analytics.enabled)
```

**Install:**

```bash
make helm-lint       # validate (no cluster needed)
make helm-template   # dry-run rendered YAML

helm upgrade --install medanon ./helm/medanon \
  --set global.registry=registry.example.com \
  --set anonymizer.secrets.MEDANON_HASH_KEY=<hex-key> \
  --set gpas.secrets.WF_ADMIN_PASS=<password> \
  --namespace medanon --create-namespace
```

**K3s (single-node / edge):**

Use `helm/k3s-values.yaml` — configures Traefik ingress and `local-path` storage class. K3s uses a separate containerd image store from Docker:

```bash
docker save medanon:latest | sudo k3s ctr images import -
docker save medanon-ui:latest | sudo k3s ctr images import -
```

Note: K3s uses Flannel by default, which does not enforce `NetworkPolicy`. For regulated environments, use Cilium instead.

### Container resource limits

| Service | RAM limit | CPU limit | Peak usage notes |
|---|---|---|---|
| `anonymizer` | 3 GB | 2.0 | NLP runs in separate microservice; adjust if OOM during bulk export |
| `worker` | 2 GB | 1.0 | Lower than anonymizer — single job at a time |
| `gpas` | 2.5 GB | 1.0 | WildFly JVM: Xms128M Xmx1536M, G1GC |
| `gpas-db` | 2 GB | 1.0 | PostgreSQL shared_buffers 512 MB |
| `fhir-server` | 3 GB | 2.0 | JVM heap |
| `app-db` | 1 GB | 0.5 | PostgreSQL for jobs/configs/staging |
| `ui` | 128 MB | 0.5 | nginx is lightweight |
| `redis` | 512 MB | 0.5 | Cache + queue |

### TLS / Reverse proxy (production)

Docker Compose binds ports to the host. Place a reverse proxy for TLS termination:

```nginx
server {
    listen 443 ssl;
    server_name medanon.example.com;
    ssl_certificate /etc/ssl/certs/medanon.crt;
    ssl_certificate_key /etc/ssl/private/medanon.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / { proxy_pass http://127.0.0.1:8501; }  # React UI
    location /api/ {
        proxy_pass http://127.0.0.1:8000/;
        proxy_set_header X-Request-ID $request_id;
    }
}
```

**Never expose port 8080** (gPAS web UI) externally.

---

## 9. Operations

### Starting and stopping

```bash
make up       # start full stack (preflight checks + docker compose + smoke verify)
make down     # stop containers; volumes preserved
make logs     # tail all container logs
make verify   # smoke-test a running stack
```

### Monitoring

**Prometheus metrics** (scraped from `http://anonymizer:8000/metrics`):

| Metric | Labels | Description |
|---|---|---|
| `medanon_requests_total` | `method`, `path`, `status` | Total HTTP requests |
| `medanon_request_duration_seconds` | `path` | Latency histogram |
| `medanon_gpas_calls_total` | `operation`, `cached` | gPAS call count + cache hit rate |
| `medanon_gpas_latency_seconds` | — | gPAS round-trip latency |
| `medanon_fhir_calls_total` | `operation`, `server` | FHIR client call count |
| `medanon_jobs_total` | `type`, `status` | Job completion counts |

Worker metrics are exposed separately on port 9091.

All Helm chart pods include `prometheus.io/scrape: "true"` annotations.

**Key health checks:**

```bash
curl http://localhost:8000/health   # {"status":"ok"} — liveness, no external calls
curl http://localhost:8000/ready    # {"ready":true}  — probes FHIR + gPAS (5s timeout each)
docker compose ps                   # all containers + health status
```

### Scaling strategy

**Anonymizer / worker** — scale horizontally. Redis Streams + consumer groups act as the shared job queue across replicas; each replica receives a disjoint slice of new messages and crashed-worker entries are reclaimed via `XAUTOCLAIM`. Set `MEDANON_JOB_WORKERS` to control concurrent jobs per instance.

**gPAS** — scale with `--scale gpas=N`. The Traefik `gateway` distributes requests round-robin (with sticky cookies for the gPAS web UI). Each gPAS instance connects to the same `gpas-db` PostgreSQL.

**NLP** — scale with `--scale nlp=N`. The Traefik `gateway` distributes requests round-robin to the least busy NLP replica. NLP is CPU-bound; scaling NLP instances directly increases throughput.

**When NOT to scale FHIR fetch threads:** `MEDANON_FHIR_FETCH_PARALLEL` should stay at `1`. The bottleneck is gPAS, not FHIR reads. Multiple fetch threads compete for the Python GIL and the internal queue lock while waiting for gPAS — observed to be slower than single-threaded at scale.

### Backup and recovery

**gPAS PostgreSQL (critical — pseudonym mappings):**

Loss of the gPAS database makes all pseudonym mappings unrecoverable. Back up before any maintenance.

```bash
# Backup
docker exec gpas-postgres pg_dump -U gpas_user -d gpas -F c -f /tmp/backup.dump
docker cp gpas-postgres:/tmp/backup.dump ./backup/gpas-$(date +%Y%m%d).dump

# Restore (requires stack down)
docker compose down
docker compose up -d gpas-db
docker cp ./backup/gpas-YYYYMMDD.dump gpas-postgres:/tmp/restore.dump
docker exec gpas-postgres pg_restore -U gpas_user -d gpas --clean /tmp/restore.dump
docker compose up -d
```

**App-db (jobs, configs, subscriptions):**

```bash
docker exec medanon-app-db pg_dump -U medanon -d medanon -F c -f /tmp/appdb.dump
docker cp medanon-app-db:/tmp/appdb.dump ./backup/appdb-$(date +%Y%m%d).dump
```

**Config profiles and keys:**

```bash
cp -r services/anonymizer/config/ backup/config-$(date +%Y%m%d)/
cp services/anonymizer/keys/id_rsa* backup/keys-$(date +%Y%m%d)/
```

### Secret rotation

| Secret | Rotation procedure | Impact |
|---|---|---|
| `MEDANON_HASH_KEY` | Update `.env`, restart anonymizer | All existing `cryptohash` pseudonyms change — old output cannot be re-linked to new |
| RSA private key | Generate new keypair, update `.env` paths | Old encrypted values unreadable; keep old key for historical data |
| `GPAS_BASIC_PASS` | Update `.env` + run `CALL changePassword(...)` in gPAS DB, restart anonymizer | Existing gPAS sessions invalidated |
| `GPAS_DB_PASSWORD` | Requires `docker compose down -v` to recreate volume | **Destroys all pseudonym mappings** — back up first |
| `MEDANON_API_KEY` | Update `.env`, restart anonymizer | All API clients must update their key |
| `MEDANON_REDIS_PASSWORD` | Update `.env`, restart anonymizer + worker | Redis sessions invalidated |

### Troubleshooting

**gPAS "domain not found":**
Domain was created via direct SQL instead of the gPAS API. gPAS maintains a JVM-level `domainLocks HashMap` not visible to SQL. Run `make init-domains` (which uses the gPAS REST API).

**gPAS circuit breaker open:**
gPAS failed 5+ times in 60s. The circuit fails-fast for 30s, then probes. Restart gPAS if the issue is resolved: `docker compose restart gpas`. Tune: `GPAS_CB_FAILURE_THRESHOLD`, `GPAS_CB_RECOVERY_TIMEOUT_SEC`.

**Bulk export slow:**
```bash
MEDANON_BATCH_SIZE=300       # resources per gPAS HTTP call
FHIR_PAGE_SIZE=500           # resources per FHIR page
MEDANON_FHIR_FETCH_PARALLEL=1  # keep at 1 — more threads compete for GIL
MEDANON_JOB_WORKERS=10       # concurrent background jobs
```

**FHIR upload HAPI-1094 (referenced resource not found):**
A resource was uploaded before one it references. The topological sort missed a reference pattern. Check `docker compose logs anonymizer` for `tier map:` debug output. The Bellman-Ford sort handles standard FHIR reference chains; custom extensions referencing resources are not always captured.

**FHIR gender rejection HAPI-1821:**
`Patient.gender` must be one of `male | female | other | unknown`. De-identification rules must use `substitute_with: "unknown"` for gender fields, not `[REDACTED]`.

**503 on job endpoints:**
Job store not initialized. Check Redis connectivity (`docker compose ps redis`) or SQLite path writability (`MEDANON_JOB_DB`).

**NLP returning `[NLP_UNAVAILABLE]`:**
NLP microservice is down or unreachable. Check: `docker compose ps nlp gateway`. The system fails-closed by design — no PHI leaks, but NLP scrubbing is not applied. Restart: `docker compose restart nlp`.

**OOM killed container:**
```bash
docker inspect --format='{{.State.OOMKilled}}' <container>
```
Increase `mem_limit` in `docker-compose.yml` for the affected container. See resource limits table above.

### Go-live checklist

#### Secrets
- [ ] `MEDANON_HASH_KEY` set (`openssl rand -hex 32`)
- [ ] `MEDANON_API_KEY` set for authenticated access
- [ ] `GPAS_BASIC_PASS` rotated from default
- [ ] `GPAS_DB_PASSWORD` set and not default
- [ ] `MEDANON_REDIS_PASSWORD` set
- [ ] `HAPI_DB_PASSWORD` and `HAPI_TARGET_DB_PASSWORD` set
- [ ] `.env` is in `.gitignore` and not committed

#### Network and TLS
- [ ] TLS termination at reverse proxy
- [ ] `MEDANON_CORS_ORIGINS` restricted to known origins
- [ ] gPAS web UI (port 8080) not publicly accessible
- [ ] Source FHIR (source-net) not reachable from outside

#### Logging and monitoring
- [ ] `LOG_LEVEL=INFO` (not DEBUG — DEBUG may log PHI)
- [ ] `MEDANON_MANIFEST_ENABLED=true` (GDPR Art. 30 accountability)
- [ ] Audit log volume mounted and rotated
- [ ] Prometheus scraping configured

#### gPAS
- [ ] Domain created via web UI or `make init-domains` (not direct SQL)
- [ ] `GPAS_DOMAIN` matches exactly what was created
- [ ] gPAS PostgreSQL backed up before first production run
- [ ] `curl http://localhost:8080/ttp-fhir/fhir/gpas/metadata` returns 200

#### Validation
- [ ] `/ready` returns `{"ready": true}`
- [ ] End-to-end: POST sample Patient to `/process`, verify output
- [ ] Run `/analyse/risk` on de-identified output before sharing
- [ ] Select appropriate config profile per [policies.md](policies.md)
- [ ] Verify NLP scrubbing active: `docker compose ps nlp nlp-lb` healthy

---

## Appendix — Performance reference

| Variable | Default | Effect |
|---|---|---|
| `MEDANON_BATCH_SIZE` | 1000 | Resources per gPAS batch (one HTTP call per batch) |
| `MEDANON_JOB_WORKERS` | 3 | Concurrent background jobs per anonymizer instance |
| `MEDANON_PARALLEL_WORKERS` | 8 | Thread pool size for pipeline stage parallelism |
| `FHIR_PAGE_SIZE` | 500 | Resources per FHIR paginated fetch |
| `MEDANON_FHIR_FETCH_PARALLEL` | 1 | Parallel FHIR resource-type fetch threads (keep at 1) |
| `MEDANON_COHORT_PARALLEL` | 2 | Parallel `$everything` threads for cohort export |
| `GPAS_MAX_BATCH_SIZE` | — | Maximum IDs per single gPAS request |
| `GPAS_POOL_SIZE` | — | gPAS HTTP connection pool size |

---

## Appendix — Quick reference commands

```bash
# Start / stop
make up                                           # full stack
make down                                         # stop, keep volumes
docker compose down -v                            # stop + delete all data

# Opt-in profiles
docker compose --profile nlp up -d               # NLP microservice
docker compose --profile ai up -d                # Ollama LLM

# Scale
docker compose up -d --scale gpas=3              # 3 gPAS replicas
docker compose up -d --scale nlp=4               # 4 NLP replicas

# Tests (from services/anonymizer/)
python3 -m pytest tests/ -q                       # full suite
python3 -m pytest tests/ --cov=src               # with coverage

# Kubernetes
make helm-lint                                    # validate chart
make helm-template                                # dry-run
make helm-install                                 # install/upgrade

# Backup
docker exec gpas-postgres pg_dump -U gpas_user -d gpas -F c -f /tmp/backup.dump

# Debug
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/metrics
docker compose exec anonymizer tail -f /output/audit.log
docker compose logs anonymizer --tail 50
```
