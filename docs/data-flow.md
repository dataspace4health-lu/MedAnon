# Data Flow Reference

Complete trace of data from ingestion to output across all components.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Browser (React SPA)                                                    │
│  localhost:8501                                                         │
└──────┬──────────────────────────────┬───────────────────────────────────┘
       │ /api/*                       │ /fhir/*
       ▼                              ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  nginx (medanon-ui :8501)                                                │
│  ┌─────────────────────┐  ┌──────────────────────┐  ┌────────────────┐  │
│  │ /api/ → medanon:8000│  │ /fhir/ → hapi:8080   │  │ / → SPA files  │  │
│  │ strips /api prefix  │  │ passthrough          │  │ index.html     │  │
│  │ buffering OFF       │  │ timeout 300s         │  │ fallback       │  │
│  └─────────────────────┘  └──────────────────────┘  └────────────────┘  │
└──────┬───────────────────────────────┬──────────────────────────────────┘
       │                               │
       ▼                               ▼
┌──────────────────┐          ┌─────────────────────┐
│ Anonymizer       │          │ HAPI FHIR Server    │
│ medanon:8000     │◄────────►│ hapi-fhir:8080      │
│ (FastAPI)        │ HTTP     │ (Spring Boot)       │
│                  │          │                     │
│ Reads from HAPI  │          │ Stores FHIR JSON    │
│ Writes to HAPI   │          │ in PostgreSQL       │
└──┬──────┬────────┘          └─────────┬───────────┘
   │      │  │                          │
   │      │  └──────────────────────────┼──────────────────────────────┐
   │      ▼                             ▼                              ▼
   │ ┌────────────┐            ┌──────────────────┐         ┌─────────────────────┐
   │ │ gPAS       │            │ PostgreSQL       │         │ PostgreSQL (staging)│
   │ │ gpas:8080  │            │ hapi-postgres    │         │ (optional)          │
   │ │ (WildFly)  │            │ :5432 (internal) │         │ MEDANON_STAGING_    │
   │ └─────┬──────┘            │ Vol: hapi-pg-data│         │ DB_URL              │
   │       │                   │ Format: relational│         │                     │
   │       ▼                   └──────────────────┘         │ medanon.staged_     │
   │ ┌────────────┐                                         │ resources table     │
   │ │ MySQL      │                                         └─────────────────────┘
   │ │ gpas-mysql  │
   │ │ :3306       │
   │ │ Vol: gpas-db│
   │ └────────────┘
   │
   ▼
┌──────────────┐    ┌──────────────────────────┐
│ Redis        │    │ Disk (/output)           │
│ redis:6379   │    │ ├── {jobId}.ndjson       │
│ (internal)   │    │ ├── jobs.db (SQLite)     │
│              │    │ └── audit.log            │
│ Vol: redis-  │    │ Vol: ./output:/output    │
│   data       │    └──────────────────────────┘
└──────────────┘
```

---

## Storage Inventory

| Location | What | Format | Lifetime |
|---|---|---|---|
| **PostgreSQL** (`hapi-pg-data` volume) | FHIR resources | Relational (HAPI JPA schema) | Persistent |
| **PostgreSQL** (`MEDANON_STAGING_DB_URL`) | Staged resources for bulk jobs | `medanon.staged_resources` table | `MEDANON_STAGING_RETENTION_DAYS` (default 30) |
| **MySQL** (`gpas-db-data` volume) | Pseudonym domain mappings | Relational (gPAS schema) | Persistent |
| **Redis** (`redis-data` volume) | gPAS cache (`medanon:gpas:*`) | Key-value, string | 1 hour TTL |
| **Redis** | Job metadata (`medanon:job:*`) | Hash per job | 7-day TTL |
| **Redis** | Job queue (`medanon:job_queue`) | List of job IDs | Consumed on read |
| **Redis** | Job indexes (`medanon:jobs:status:*`, `medanon:jobs:type:*`) | Sets | No TTL |
| **SQLite** (`/output/jobs.db`) | Job metadata (fallback when no Redis) | Single `jobs` table | Indefinite |
| **Disk** (`/output/{jobId}.ndjson`) | Completed async job results | NDJSON | Indefinite |
| **Disk** (`/output/audit.log`) | Audit trail | Structured log lines | 10 MB rotation, 5 backups |
| **In-process memory** | gPAS LRU cache (50K entries) | Python dict | Until restart |
| **In-process memory** | FHIRPath compiled expressions | Python dict | Until restart |
| **In-process memory** | Rule index per config file | Python dict | Until restart |

### Staging Table Schema

```sql
CREATE TABLE medanon.staged_resources (
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT        NOT NULL,
    resource_id TEXT        NOT NULL,         -- FHIR resource id
    payload     TEXT        NOT NULL,         -- JSON-serialised resource
    status      TEXT        NOT NULL DEFAULT 'pending',  -- pending|done|error
    error_msg   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    UNIQUE (job_id, resource_id)
);
```

Row lifecycle: `pending` → `done` (or `error`). Rows expire after `MEDANON_STAGING_RETENTION_DAYS` and are purged hourly by `_cleanup_loop()`.

---

## Data Formats at Each Boundary

| Boundary | Format | Content-Type |
|---|---|---|
| Browser → HAPI FHIR (read) | FHIR JSON | `application/fhir+json` |
| Browser → Anonymizer (single) | FHIR JSON | `application/json` |
| Browser → Anonymizer (batch) | FHIR JSON Bundle or NDJSON or XML | auto-detected from `Content-Type` |
| Anonymizer → Browser (single) | FHIR JSON or XML | `application/fhir+json` or `application/fhir+xml` |
| Anonymizer → Browser (batch/stream) | NDJSON | `application/x-ndjson` |
| Anonymizer → HAPI FHIR (read) | FHIR JSON (paginated Bundles) | `application/fhir+json` |
| Anonymizer → HAPI FHIR (write) | FHIR JSON (PUT/POST per resource) | `application/fhir+json` |
| Anonymizer → gPAS (pseudonymize) | FHIR Parameters XML | `application/xml` |
| gPAS → Anonymizer | FHIR Parameters XML | `application/xml` |
| Anonymizer → Redis (cache) | String values keyed by JSON tuples | N/A |
| Anonymizer → Staging DB (Phase 1) | JSON-serialised FHIR resource | TEXT column, batched INSERT |
| Staging DB → Anonymizer (Phase 2) | JSON-serialised FHIR resource | TEXT, batch SELECT FOR UPDATE SKIP LOCKED |
| Worker → Disk (job result) | NDJSON file | one JSON object per line |
| Worker → SQLite/Redis (job state) | Job metadata | see schema above |
| HAPI FHIR → PostgreSQL | HAPI JPA entities | Hibernate ORM |

---

## Entry Points (How Data Gets In)

### A. Loading Data into HAPI FHIR

HAPI FHIR accepts standard FHIR REST operations. Data enters via:

| Path | Method | Payload | Notes |
|---|---|---|---|
| Direct FHIR API | `PUT /fhir/{Type}/{id}` | Single FHIR JSON resource | Create/update by ID |
| Direct FHIR API | `POST /fhir/{Type}` | Single FHIR JSON resource | Server-assigned ID |
| Direct FHIR API | `POST /fhir` | FHIR Bundle (transaction/batch) | Multiple resources, atomically |
| Anonymizer round-trip | `POST /process/round-trip` | Fetches from source, writes to target | See endpoint #8 |
| Anonymizer and-upload | `POST /process/and-upload` | De-identifies then uploads | See endpoint #7 |

Data is stored in **PostgreSQL** via Hibernate JPA. The HAPI FHIR schema stores resources as JSON blobs with indexed search parameters.

### B. Processing Data Through the Anonymizer

17 API endpoints accept data. Grouped by pattern:

**Direct processing (resource provided in request body):**

| # | Endpoint | Input | Output | Streaming |
|---|---|---|---|---|
| 1 | `POST /process` | JSON (resource, array, or Parameters wrapper) | JSON | No |
| 2 | `POST /process/ndjson` | NDJSON (one resource per line) | NDJSON | Yes |
| 3 | `POST /process/raw` | JSON, XML, or NDJSON (auto-detected) | JSON, XML, or NDJSON (`output_format` param) | No |
| 4 | `POST /process/batch` | JSON, XML, or NDJSON | NDJSON | Yes |

**Server-fetched processing (anonymizer fetches from FHIR server):**

| # | Endpoint | Input | What It Fetches | Output | Writes to FHIR |
|---|---|---|---|---|---|
| 5 | `POST /process/from-server` | `{server_url, resource_types}` | Paginated `GET /{Type}` for each type | NDJSON | No |
| 6 | `POST /process/everything` | `{server_url, resource_type, resource_id}` | `GET /{Type}/{id}/$everything` | NDJSON | No |
| 7 | `POST /process/and-upload` | `{target_server_url, resource}` | N/A (resource in body) | JSON summary | **Yes** |
| 8 | `POST /process/round-trip` | `{source_url, target_url, types}` | Paginated `GET /{Type}` from source | NDJSON status log | **Yes** |
| 9 | `POST /process/bulk-export` | `{server_url, level, type_filter}` | FHIR `$export` protocol | NDJSON | No |
| 10 | `POST /process/cohort` | `{server_url, search_type, search_params}` | Search → patient refs → `$everything` per patient | NDJSON | No |

**Async jobs (queued, executed by worker):**

| # | Endpoint | Job Type | Executor | Staged Path |
|---|---|---|---|---|
| 11 | `POST /v1/jobs/bulk-export` | `bulk-export` | `_execute_bulk_export` → `execute_bulk_export_staged` | When `MEDANON_STAGING_DB_URL` set |
| 12 | `POST /v1/jobs/cohort` | `cohort` | `_execute_cohort` → `execute_cohort_staged` | When `MEDANON_STAGING_DB_URL` set |
| 13 | `POST /v1/jobs/patient-export` | `patient-export` | `_execute_patient_export` → `execute_patient_export_staged` | When `MEDANON_STAGING_DB_URL` set |
| 14 | `POST /v1/jobs/{id}/reprocess` | `reprocess` | `execute_reprocess_staged` | Requires staging |
| 15 | `GET /v1/jobs/{id}` | — | N/A (poll status) | Returns `staged_count` when staging active |
| 16 | `GET /v1/jobs/{id}/result` | — | N/A (serves file) | Returns `/output/{jobId}.ndjson` |
| 17 | `GET /v1/jobs/{id}/staged-stats` | — | N/A | Returns `{pending, done, error, total}` |

---

## De-identification Pipeline (What Happens to Each Resource)

Every resource passes through the same 4-stage pipeline in `processor.py`:

```
Input: FHIR resource dict (in memory)
│
│  ROUTING (process_data)
│  ├── list  → recurse per element
│  ├── Bundle → _process_bundle (snapshots IDs, processes each entry, rewrites refs)
│  └── single → _process_single_resource
│
▼
┌─────────────────────────────────────────────────────────────────┐
│  PASS 1: Rule Matching + Action Dispatch                        │
│  (action_dispatcher.py)                                         │
│                                                                 │
│  For each rule in the config YAML:                              │
│  1. Evaluate FHIRPath expression against the resource           │
│     (cached compiled expressions in memory)                     │
│  2. For each matched element:                                   │
│     ├── redact      → replace with "[REDACTED]" or remove       │
│     ├── cryptohash  → SHA3-256 / HMAC-SHA3 of the value        │
│     ├── encrypt     → RSA encrypt with public key               │
│     ├── generalize  → date→year, age→range, zip→prefix          │
│     ├── perturb     → add bounded random noise                  │
│     ├── substitute  → replace with a fixed/mapped value         │
│     ├── scrub_text  → regex patterns (SSN, phone, email, etc.)  │
│     ├── nlp_detect  → NER via Presidio (local or remote)        │
│     └── gpas_pseudonymize → DEFERRED to Pass 2                  │
│                                                                 │
│  Actions mutate the resource dict in place.                     │
│  gPAS items are collected as BatchWork for Pass 2.              │
│                                                                 │
│  Output: list[BatchWork] (deferred gPAS items)                  │
│  Storage: none (in-memory only)                                 │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASS 2: Batch gPAS Pseudonymization                            │
│  (gpas_orchestrator.py)                                         │
│                                                                 │
│  Single-resource path (streaming / direct API):                 │
│  1. Collect all original values from this resource's BatchWork  │
│  2. One HTTP call to gPAS per resource                          │
│                                                                 │
│  Staged-batch path (execute_*_staged, batch of N resources):    │
│  1. run_gpas_batch_for_batch() collects all values from         │
│     ALL resources in the batch, deduplicates with              │
│     dict.fromkeys(), makes ONE gPAS HTTP call for the batch     │
│  2. Results warm the in-process LRU cache                       │
│  3. Per-resource run_gpas_batch() calls are pure cache hits     │
│     (zero additional HTTP calls)                                │
│                                                                 │
│  Either path:                                                   │
│  POST gpas:8080/ttp-fhir/fhir/$pseudonymize-or-create          │
│  Body: FHIR Parameters (XML)                                    │
│  gPAS returns {original → pseudonym} mapping                    │
│  Pseudonyms written back into resource at FHIRPath nodes        │
│                                                                 │
│  Cache: LocalLruCache (50K entries) or RedisCache (1h TTL)      │
│  No-op when: no gpas_pseudonymize rules matched                 │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASS 3: Post-Processing                                        │
│  (post_processor.py)                                            │
│                                                                 │
│  A. Reference Rewriting (when rewrite_references: true)         │
│     - Deep-walk all "reference" fields                          │
│     - Batch-pseudonymize extracted IDs via gPAS                 │
│     - Rewrite references + delete "display" fields (PHI)        │
│                                                                 │
│  B. Text-ID Rewriting (when rewrite_text_ids: true)             │
│     - Compile regex from changed ID pairs                       │
│     - Replace old IDs with pseudonyms in all string values      │
│                                                                 │
│  Output: resource dict (mutated in place)                       │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│  PASS 4: Manifest Tagging (optional)                            │
│  (manifest.py)                                                  │
│                                                                 │
│  When MEDANON_MANIFEST_ENABLED=true:                            │
│  - Appends meta.tag with JSON array of {rule, action, path}     │
│                                                                 │
│  Output: resource with manifest in meta.tag                     │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
Output: De-identified FHIR resource dict (in memory)
```

---

## End-to-End Flows

### Flow 1: Per-Patient De-identification (UI)

```
User clicks "Run $everything" for Patient/123
│
▼
Browser ──GET /fhir/Patient/123/$everything?_count=5000──► nginx ──► HAPI FHIR
                                                                        │
                Format: FHIR JSON Bundle                                │
                Contains: all resources in patient compartment           │
                Source: PostgreSQL (HAPI JPA tables)                    │
                                                                        │
Browser ◄── FHIR Bundle (JSON) ◄────────────────────────────────────────┘
│
│  Browser stores original resources in memory (for diff view)
│
▼
Browser ──POST /api/v1/process/batch──► nginx ──► Anonymizer
           Body: the full Bundle (JSON)             │
           Content-Type: application/json            │
                                                     ▼
                                              Bundle detected →
                                              process_bundle_stream() →
                                              process_data(bundle) →
                                              _process_bundle():
                                                1. Snapshot pre-IDs
                                                2. Process each entry (4-stage pipeline)
                                                3. Build ref_map {old→new}
                                                4. Deep-rewrite all references
                                                5. Rewrite text IDs
                                              Emit each entry.resource as NDJSON line
                                                     │
Browser ◄── Streaming NDJSON ◄────── nginx ◄─────────┘
│           (one JSON object per line, proxy_buffering off)
│
▼
Browser accumulates resources, updates UI progressively:
  - Output tab: JSON viewer
  - Compare tab: diff original vs de-identified
  - Table tab: field-level view with PII highlighting
│
▼
Download options:
  - NDJSON: client-side Blob from accumulated resources
  - JSON Bundle: client-side wraps resources in Bundle structure
  - XML: POST /api/v1/process/raw?output_format=xml
          (sends already-processed resources, just format conversion)
```

### Flow 2: Async Bulk Export (UI)

```
User selects resource types on Patient Browser, clicks "Export"
│
▼
Browser ──POST /api/v1/jobs/bulk-export──► Anonymizer
           Body: {server_url, resource_types, config_profile}
                                                     │
                                              Job created (SQLite or Redis)
                                              Returns HTTP 202 {job_id}
│
│  Browser polls GET /api/v1/jobs/{jobId} every 3 seconds
│
│  Worker picks up job:
│
│  Without staging (streaming fallback):
│  ┌─────────────────────────────────────────────────────────────┐
│  │  For each resource type:                                    │
│  │    Paginated GET /{Type}?_count=100 from HAPI FHIR          │
│  │    For each resource: process_data() → write NDJSON line    │
│  │    Checkpoint every 100 resources (cursor + lines_written)  │
│  └─────────────────────────────────────────────────────────────┘
│
│  With staging — see Flow 5 (Staged Two-Phase Pipeline)
│
▼
Poll sees status=done
Browser ──GET /api/v1/jobs/{jobId}/result──► FileResponse: /output/{id}.ndjson
```

### Flow 3: Cohort Export (UI)

```
User filters conditions on Condition Browser, clicks "Export by Condition"
│
▼
Browser ──POST /api/v1/jobs/cohort──► Anonymizer
           Body: {server_url, search_type: "Condition",
                  search_params: {code, clinical-status}, config_profile}
│
│  Worker (_execute_cohort):
│
│  Without staging:
│  ┌─────────────────────────────────────────────────────────────┐
│  │  Phase 1: GET /Condition?code=E11                           │
│  │           Extract unique Patient IDs from subject.reference │
│  │  Phase 2: For each patient:                                 │
│  │           GET /Patient/{id}/$everything → process → NDJSON  │
│  └─────────────────────────────────────────────────────────────┘
│
│  With staging — see Flow 5 (Staged Two-Phase Pipeline)
│
▼
Same polling + download flow as Flow 2
```

### Flow 4: Round-Trip (Source → De-identify → Target FHIR Server)

```
API call: POST /process/round-trip
Body: {source_server_url, target_server_url, resource_types}
│
▼
For each resource type:
  GET source/{Type}?_count=100 (paginated FHIR JSON Bundles)
  For each resource:
    process_data() (4-stage pipeline, in memory)
    PUT or POST to target/{Type}/{id}  (Content-Type: application/fhir+json)
    Yield status NDJSON: {"resourceType":"Patient","target_id":"p-123","status":"ok"}
│
▼
Streaming NDJSON status log to caller
Data now lives in target HAPI FHIR PostgreSQL
```

### Flow 5: Staged Two-Phase Pipeline

Activated when `MEDANON_STAGING_DB_URL` is set. Applies to `bulk-export`, `cohort`, and `patient-export` jobs.

```
                     ┌──────────────────────────────────────────┐
                     │  PHASE 1: Fetch → Staging DB             │
                     │  (execute_*_staged)                      │
                     │                                          │
                     │  Generator: fetch_resource_type() or     │
                     │             fetch_everything()           │
                     │  yield_cursors=True → (resource, cursor) │
                     │  Start URL from checkpoint on resume     │
                     │                                          │
                     │  For every MEDANON_STAGING_BATCH_SIZE    │
                     │  resources (default 500):                │
                     │  ┌──────────────────────────────────────┐│
                     │  │ INSERT INTO medanon.staged_resources  ││
                     │  │   (job_id, resource_id, payload)      ││
                     │  │   ON CONFLICT DO NOTHING              ││
                     │  │ Save checkpoint {phase, staged_count, ││
                     │  │   fetch_cursor, type_index}           ││
                     │  └──────────────────────────────────────┘│
                     │                                          │
                     │  Checkpoint survives crashes →           │
                     │  resume from exact HAPI page             │
                     └─────────────────┬────────────────────────┘
                                       │
                                       ▼
                     ┌──────────────────────────────────────────┐
                     │  PHASE 2: Process Staged Rows → NDJSON   │
                     │  (_process_batch × N)                    │
                     │                                          │
                     │  Loop until no pending rows:             │
                     │  ┌──────────────────────────────────────┐│
                     │  │ SELECT ... FROM staged_resources      ││
                     │  │   WHERE job_id=? AND status='pending' ││
                     │  │   FOR UPDATE SKIP LOCKED              ││
                     │  │   LIMIT 500                           ││
                     │  │                                       ││
                     │  │  Per batch:                           ││
                     │  │  1. Pass 1 for each resource          ││
                     │  │     (rule matching + action dispatch) ││
                     │  │  2. run_gpas_batch_for_batch()        ││
                     │  │     ONE gPAS HTTP call for all 500    ││
                     │  │     deduped IDs across the batch      ││
                     │  │     → warms in-process LRU cache      ││
                     │  │  3. Pass 2 per resource               ││
                     │  │     (pure cache hits, zero HTTP)      ││
                     │  │  4. Post-process + manifest tag       ││
                     │  │  5. Write NDJSON to /output/{id}.ndjson│
                     │  │  6. UPDATE status='done'              ││
                     │  │  7. Checkpoint {processed}            ││
                     │  └──────────────────────────────────────┘│
                     │                                          │
                     │  gPAS call reduction:                    │
                     │  Streaming: 1 call per resource          │
                     │  Staged:    1 call per 500 resources     │
                     │  = 500× fewer gPAS HTTP calls            │
                     └──────────────────────────────────────────┘
```

**Throughput reference (50K resources, local Docker):**

| Phase | No gPAS | gPAS local | gPAS remote |
|---|---|---|---|
| Phase 1 (fetch + stage) | ~80s | ~80s | ~80s |
| Phase 2 (process) | ~25s | ~60s | ~90s |
| **Total** | **~105s** | **~140s** | **~170s** |

Streaming path without staging and gPAS per-resource: ~5–7 hours for 50K resources.

### Flow 6: Reprocess Job

Replays staged rows from a completed/cancelled job with the same or a different config profile. No FHIR re-fetch required.

```
User clicks "Re-process with same profile" on a completed job card
│
▼
Browser ──POST /api/v1/jobs/{sourceJobId}/reprocess?config_profile=auto──► Anonymizer
                                                           │
                                                    New job created (type: "reprocess")
                                                    params: {source_job_id, config_profile}
                                                    Returns HTTP 202 {new_job_id}
│
navigate("/bulk-deidentify", { autoSelectId: newId })
│
▼
Worker (execute_reprocess_staged):
  1. staging.reset_pending(source_job_id)   — flip done→pending for all rows
  2. Load new config profile (YAML)         — may differ from original
  3. Run Phase 2 loop (same as Flow 5)      — process rows, write new NDJSON
  4. New output: /output/{new_job_id}.ndjson
```

### Flow 7: Single Resource Processing (UI)

```
User pastes FHIR JSON/XML on Process Resource page
│
▼
Browser ──POST /api/v1/process/raw?output_format=json&config_profile=auto──► Anonymizer
           Body: raw FHIR content (JSON, XML, or NDJSON)
                                                     │
                                              1. parse_payload_bytes(): detect format
                                              2. process_data() (4-stage pipeline)
                                              3. serialize_payload(): format output
                                                     │
Browser ◄── De-identified resource ◄─────────────────┘
UI displays in syntax-highlighted viewer
```

### Flow 8: Round-trip / Source → De-identify → Target FHIR Server

See Flow 4 above.

---

## Config Profile Selection

The `?config_profile=` query parameter selects which YAML rule set applies:

| Profile | File | Strategy | Dependencies |
|---|---|---|---|
| `auto` (default) | `config.yaml` when `GPAS_URL` unset; `config_gpas.yaml` when set | Auto-selects based on environment | none / gPAS |
| `default` | `config.yaml` | SHA3-256 hash for IDs, regex+NLP text scrub | none |
| `gpas` | `config_gpas.yaml` | gPAS pseudonymization + NLP | gPAS server |
| `gdpr_eu` | `config_gdpr_eu.yaml` | GDPR Art. 4(5) HMAC pseudonymization | HMAC key |
| `hipaa_safe_harbor` | `config_hipaa_safe_harbor.yaml` | HIPAA 18 identifiers, dates→year, zip→3-digit | none |
| `research_pseudonymous` | `config_research_pseudonymous.yaml` | IRB-grade, dates→year-month, IDs cryptohashed | HMAC key |
| `structure_preserving` | `config_structure_preserving.yaml` | Full FHIR structure, IDs via gPAS, PII→`[REDACTED]` | gPAS server |

---

## Format Conversion Matrix

| Input Format | Auto-detected By | Can Output As |
|---|---|---|
| FHIR JSON (`application/json`, `application/fhir+json`) | Content-Type or `.json` extension | JSON, XML, NDJSON |
| FHIR XML (`application/xml`, `application/fhir+xml`) | Content-Type or `.xml` extension | JSON, XML, NDJSON |
| NDJSON (`application/x-ndjson`) | Content-Type or `.ndjson`/`.jsonl` extension | JSON (array), NDJSON |

XML parsing uses `defusedxml` (XXE protection). XML→dict conversion handles FHIR-specific structures: repeating elements forced to arrays, `value` attributes coerced to native types.

---

## Network Ports Summary

| Service | Container Hostname | Internal Port | Host Port | Protocol |
|---|---|---|---|---|
| Anonymizer | `medanon` | 8000 | 8000 | HTTP (FastAPI) |
| HAPI FHIR | `hapi-fhir` | 8080 | 8081 | HTTP (Spring Boot) |
| gPAS | `gpas-wildfly` | 8080 | 8080 | HTTP (WildFly) |
| UI (nginx) | `medanon-ui` | 8501 | 8501 | HTTP |
| PostgreSQL (HAPI) | `hapi-postgres` | 5432 | (none) | PostgreSQL wire |
| PostgreSQL (staging) | external / configured | 5432 | (none) | PostgreSQL wire |
| MySQL | `gpas-mysql` | 3306 | (none) | MySQL wire |
| Redis | `medanon-redis` | 6379 | (none) | Redis wire |
| Analytics | `medanon-analytics` | 8100 | 8100 | HTTP (opt-in) |
| NLP | `medanon-nlp` | 8200 | 8200 | HTTP (opt-in) |
