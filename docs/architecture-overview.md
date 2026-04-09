# MedAnon — Architecture Overview

## 1. System Overview

**MedAnon (SPE FHIR BlackBox)** is a rule-driven FHIR de-identification and pseudonymization engine. It accepts healthcare data in FHIR (JSON, NDJSON, XML), HL7v2, and DICOM formats, applies configurable match-action rules from YAML profiles, and returns privacy-safe transformed data. The system is built as a modular Python backend with a multi-pass processing pipeline, an async job system for long-running bulk operations, and pluggable integration adapters for external services.

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                              ENTRY POINTS                                       │
│                                                                                 │
│  ┌───────────────┐   ┌─────────────────┐   ┌────────────────┐                  │
│  │ FastAPI REST   │   │  CLI (batch)    │   │  Worker        │                  │
│  │ (uvicorn)      │   │  batch_process  │   │  (standalone   │                  │
│  │                │   │                 │   │   container)   │                  │
│  └───────┬────────┘   └────────┬────────┘   └───────┬────────┘                  │
└──────────┼─────────────────────┼────────────────────┼───────────────────────────┘
           │                     │                    │
           ▼                     ▼                    ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                           SERVICE LAYER                                         │
│                                                                                 │
│  ProcessingService ─── single resource, NDJSON stream, bundle, batch            │
│  FhirServerService ─── fetch → process → upload (prefetch pipelining)           │
│  JobService ────────── async job lifecycle (submit, poll, cancel, download)      │
│  RiskAnalysisService ─ k-anonymity, l-diversity risk scoring                    │
│  SyntheticDataService  synthetic FHIR generation (SDV / stdlib)                 │
└─────────────────────────────────┬───────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                       PROCESSING PIPELINE                                       │
│                                                                                 │
│  ┌───────────────────────────────────────────────────────────────────────────┐  │
│  │                      processor.py  (Orchestrator)                         │  │
│  │                                                                           │  │
│  │  ┌──────────────┐   ┌───────────────────┐   ┌─────────────────────────┐  │  │
│  │  │ Rule Matcher  │──▶│ Action Dispatcher │──▶│   gPAS Orchestrator     │  │  │
│  │  │              │   │                   │   │                         │  │  │
│  │  │ · resource   │   │ · Pass 1: execute │   │ · Pass 2: deduplicate  │  │  │
│  │  │   type index │   │   non-gPAS actions│   │   across all resources │  │  │
│  │  │ · FHIRPath   │   │ · accumulate gPAS │   │ · single batch HTTP    │  │  │
│  │  │   fast paths │   │   BatchWork items │   │ · write-back pseudonyms│  │  │
│  │  └──────────────┘   └───────────────────┘   └────────────┬────────────┘  │  │
│  │                                                           │               │  │
│  │  ┌───────────────────┐   ┌──────────────────┐            ▼               │  │
│  │  │  Post-Processor   │   │ Manifest Tagger  │   ┌─────────────────┐      │  │
│  │  │                   │   │                  │   │  I/O Formats    │      │  │
│  │  │ · ref rewrite     │   │ · audit trail in │   │  JSON / NDJSON  │      │  │
│  │  │ · text-ID replace │   │   meta.tag       │   │  XML (defused)  │      │  │
│  │  │ · Aho-Corasick    │   │ · no PHI logged  │   │                 │      │  │
│  │  └───────────────────┘   └──────────────────┘   └─────────────────┘      │  │
│  └───────────────────────────────────────────────────────────────────────────┘  │
│                                                                                 │
│  ┌─────────────────────────── ACTIONS ──────────────────────────────────────┐   │
│  │  redact · cryptohash · encrypt · decrypt · perturb · substitute         │   │
│  │  generalize · scrub_text (16 regex patterns) · nlp_detect               │   │
│  └──────────────────────────────────────────────────────────────────────────┘   │
│                                                                                 │
│  ┌─────────────────────────── CONFIG ───────────────────────────────────────┐   │
│  │  6 YAML profiles: minimal · gpas · gdpr · hipaa · research · structural │   │
│  │  + user-defined profiles (CRUD API + SQLite metadata store)             │   │
│  └──────────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        INTEGRATION LAYER                                        │
│                                                                                 │
│  ┌───────────────┐  ┌───────────────┐  ┌─────────────┐  ┌──────────────────┐  │
│  │  gPAS Client  │  │  FHIR Client  │  │  NLP Client  │  │ Analytics Client │  │
│  │  batch pseudo │  │  read / write │  │  Presidio or │  │  risk + synth    │  │
│  │  + circuit    │  │  bulk export  │  │  remote HTTP │  │  proxy           │  │
│  │  breaker      │  │  + circuit    │  │  + circuit   │  │  + circuit       │  │
│  │  + retry      │  │  breaker     │  │  breaker     │  │  breaker         │  │
│  └───────┬───────┘  └───────┬───────┘  └──────┬──────┘  └────────┬─────────┘  │
│          │                  │                  │                   │            │
│  ┌───────┴───────┐  ┌──────┴────────┐  ┌──────┴──────┐  ┌────────┴─────────┐  │
│  │ Tiered Cache  │  │ Result Store  │  │ PostgreSQL  │  │ Shared HTTP Pool │  │
│  │ L1 LRU + L2  │  │ local / S3    │  │ Staging     │  │ urllib3 + retry  │  │
│  │ Redis         │  │ (MinIO)       │  │ (opt-in)    │  │ + jitter         │  │
│  └───────────────┘  └───────────────┘  └─────────────┘  └──────────────────┘  │
└─────────────────────────────────┬───────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL SERVICES                                        │
│                                                                                 │
│  ┌──────────┐  ┌────────────┐  ┌────────────┐  ┌────────┐  ┌───────────────┐  │
│  │   gPAS   │  │ HAPI FHIR  │  │ HAPI FHIR  │  │ Redis  │  │ MinIO / S3    │  │
│  │ WildFly  │  │ Source     │  │ Target     │  │        │  │ (opt-in)      │  │
│  └────┬─────┘  └─────┬──────┘  └─────┬──────┘  └────┬───┘  └───────────────┘  │
│       │              │               │              │                          │
│  ┌────┴─────┐  ┌─────┴──────┐  ┌─────┴──────┐      │    Optional:            │
│  │  MySQL   │  │ PostgreSQL │  │ PostgreSQL │      │    NLP, Analytics       │
│  └──────────┘  └────────────┘  └────────────┘      │    microservices        │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Architecture Layers

### 3.1 Service Layer

**Purpose:** Business logic orchestration — HTTP-unaware, testable in isolation.

| Service | Role |
|---|---|
| `ProcessingService` | Wraps pipeline calls with timeout, error classification (504/503/422/400/500), and gPAS failure handling. Supports four modes: single resource, NDJSON stream, bundle (preserving cross-resource references), and batch. |
| `FhirServerService` | Orchestrates multi-step FHIR workflows: fetch → process → upload. Uses bounded `asyncio.Queue` for prefetch pipelining — overlapping network I/O with CPU processing. Handles topological ordering for upload. |
| `JobService` | Manages the async job lifecycle. Creates jobs in the store, notifies the worker, exposes status/result/cancel. Supports reprocess (re-run with a different config) and upload-to-target (push results to a FHIR server). |
| `RiskAnalysisService` | Computes re-identification risk (k-anonymity, l-diversity, prosecutor/journalist/marketer scores). Routes to local compute or remote analytics proxy. |
| `SyntheticDataService` | Generates synthetic FHIR Patient resources from de-identified input. Engine resolution: SDV (GaussianCopula) if available, otherwise stdlib weighted sampling. |

### 3.2 Processing Pipeline

**Purpose:** Core de-identification engine — the heart of the system.

Detailed in **Section 4** below.

### 3.3 Integration Layer

**Purpose:** All external service communication, isolated behind adapters and port protocols.

Each integration sub-package owns its own HTTP transport, error handling, and resilience patterns (circuit breaker, retry, caching). The pipeline never imports transport code directly — it depends on port protocols (`PseudonymizerPort`, `FhirClientPort`, `NlpAdapter`).

### 3.4 Async Job System

**Purpose:** Execute long-running bulk operations outside the request/response cycle.

Detailed in **Section 5** below.

---

## 4. Processing Pipeline — Deep Dive

### 4.1 Pipeline Architecture

The pipeline is a **multi-pass, rule-driven transformation engine**. It processes FHIR resources through a fixed sequence of stages, coordinated by `processor.py`.

```
                         ┌─────────────────────────────┐
                         │    processor.py              │
                         │    (Orchestrator)             │
                         │                              │
                         │  process_data()         N=1  │
                         │  process_data_batch()   N>1  │
                         │  process_data_stream()  gen  │
                         └──────────────┬───────────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              ▼                         ▼                         ▼
    ┌─────────────────┐     ┌───────────────────┐     ┌──────────────────┐
    │  config/        │     │  rule_matcher.py   │     │  io_formats.py   │
    │                 │     │                    │     │                  │
    │  YAML loader    │     │  _build_rule_index │     │  parse:          │
    │  ${VAR} interp  │     │  per-resource-type │     │  JSON/NDJSON/XML │
    │  LRU cache      │     │  O(1) lookup       │     │  (defusedxml)    │
    │  profile alias  │     │                    │     │                  │
    │  resolution     │     │  FHIRPath eval:    │     │  serialize:      │
    │                 │     │  4 fast-path tiers  │     │  JSON/NDJSON/XML │
    └─────────────────┘     └───────────────────┘     └──────────────────┘

                    Pipeline Execution (per batch)
                    ═══════════════════════════════

 ┌──────────────────────────────────────────────────────────────────────┐
 │  PASS 1 — Action Dispatch (per resource, parallelizable)            │
 │                                                                      │
 │  For each resource:                                                  │
 │    rule_matcher → applicable rules (O(1) index lookup)               │
 │    For each rule:                                                    │
 │      FHIRPath eval → matched nodes                                   │
 │      ┌─ non-gPAS action? → execute immediately (mutate in-place)    │
 │      └─ gPAS action?     → accumulate BatchWork (deferred)          │
 │                                                                      │
 │  Output: mutated resources + list[BatchWork] + manifest entries      │
 └──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  PASS 2 — gPAS Batch Pseudonymization (single HTTP call)            │
 │                                                                      │
 │  Collect ALL values from ALL resources' BatchWork                    │
 │  → deduplicate (dict.fromkeys)                                       │
 │  → ONE pseudonymizer.pseudonymize_batch() call                       │
 │  → shared {original: pseudonym} mapping                              │
 │  → write back pseudonyms into each resource                          │
 │                                                                      │
 │  Reduces gPAS round trips from O(N × rules) to O(1) per batch       │
 └──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
 ┌──────────────────────────────────────────────────────────────────────┐
 │  PASS 3 — Post-Processing (per resource)                            │
 │                                                                      │
 │  Build text-ID matcher once (Aho-Corasick or regex fallback)         │
 │  Collect all changed reference IDs → one gPAS batch call             │
 │                                                                      │
 │  Per resource (single tree walk):                                    │
 │    · _deep_rewrite_references_gpas  — pseudonymize FHIR references  │
 │    · _rewrite_text_ids              — replace IDs in free-text       │
 │    · _attach_manifest               — audit trail in meta.tag        │
 │                                                                      │
 └──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                            Output resources
```

### 4.2 Rule Matcher — FHIRPath Fast Paths

The rule matcher classifies every FHIRPath `match` expression into one of four evaluation tiers, avoiding the overhead of the full `fhirpathpy` engine whenever possible:

```
 Expression                          Tier            Performance
 ────────────────────────────────────────────────────────────────
 Patient.name                        simple          O(1) dict traversal
 *.telecom                           wildcard        single-level scan
 Extension.where(url='...')          native where    custom evaluator
 Bundle.entry.resource.ofType(...)   full fhirpathpy LRU-cached result
```

A **per-resource-type rule index** (`_build_rule_index`) pre-groups rules by resource type at config load time, so `_get_rules_for_resource("Patient")` is an O(1) dict lookup instead of scanning all rules.

### 4.3 Action Registry

Actions are organized into three categories by `deidentify.py`:

```
┌──────────────────────────────────────────────────────────────┐
│                    Action Registry                            │
│                                                              │
│  De-identification Actions (execute in Pass 1):              │
│  ┌─────────┬────────────┬───────────┬────────────┐          │
│  │ redact  │ cryptohash │ perturb   │ generalize │          │
│  │ delete  │ HMAC-SHA3  │ CSPRNG    │ 6 strats:  │          │
│  │ node    │ -256 hash  │ noise on  │ date_year  │          │
│  │         │            │ nums/dates│ age_bracket│          │
│  └─────────┴────────────┴───────────┴──────┬─────┘          │
│  ┌───────────┬────────────┐                │                │
│  │ scrub_text│ nlp_detect │   zip_prefix, number_round,     │
│  │ 16 regex  │ Presidio   │   date_year_month, category     │
│  │ patterns  │ NER-based  │                                  │
│  │ 3 modes   │ PHI detect │                                  │
│  └───────────┴────────────┘                                  │
│                                                              │
│  Pseudonymization Actions (deferred to Pass 2):             │
│  ┌──────────────────┬──────────┐                             │
│  │ gpas_pseudonymize│ encrypt  │                             │
│  │ gPAS batch call  │ RSA-OAEP │                             │
│  └──────────────────┴──────────┘                             │
│                                                              │
│  De-pseudonymization Actions (reverse operations):          │
│  ┌────────────────────┬──────────┐                           │
│  │ gpas_depseudonymize│ decrypt  │                           │
│  │ gPAS reverse lookup│ RSA-OAEP │                           │
│  └────────────────────┴──────────┘                           │
└──────────────────────────────────────────────────────────────┘
```

### 4.4 Config System

```
┌──────────────────────────────────────────────────────────────────┐
│                     Config Resolution                            │
│                                                                  │
│  ?config_profile=auto (default)                                  │
│    │                                                             │
│    ├── GPAS_URL set?  ──yes──▶  config_gpas.yaml                │
│    └── GPAS_URL unset? ─────▶  config.yaml (minimal)            │
│                                                                  │
│  Named profiles:                                                 │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │  minimal ─── HMAC hash + regex scrub, no gPAS            │   │
│  │  gpas ────── gPAS pseudonymization + NLP dual-pass       │   │
│  │  gdpr ────── GDPR Art. 4(5) HMAC pseudonymization        │   │
│  │  hipaa ───── HIPAA Safe Harbor 18 PHI categories          │   │
│  │  research ── IRB-grade: dates→year-month, crypto IDs     │   │
│  │  structural  full structure preserved, IDs via gPAS      │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                  │
│  Rule shape:                                                     │
│  ┌─────────────────────────────────────────┐                     │
│  │  - name: "hash patient ID"             │                     │
│  │    match: "Patient.identifier.value"    │                     │
│  │    action: "cryptohash"                 │                     │
│  │    params:                              │                     │
│  │      algorithm: "sha3_256"              │                     │
│  └─────────────────────────────────────────┘                     │
│                                                                  │
│  Features: ${VAR:-default} env interpolation, LRU cache,        │
│  conflict detection, user-defined profiles in SQLite store      │
└──────────────────────────────────────────────────────────────────┘
```

---

## 5. Async Job System — Deep Dive

The job system handles bulk operations that may take minutes to hours.

### 5.1 Architecture

```
┌──────────┐  submit   ┌──────────────┐  notify   ┌────────────────────────┐
│  Client  │─────────▶ │  JobService  │─────────▶ │  Worker                │
│          │           │              │           │                        │
│          │  poll     │  JobStore    │           │  asyncio.Semaphore     │
│          │◀─ ─ ─ ─ ─│  (Redis or   │           │  (max_concurrent=3)    │
│          │           │   SQLite)    │           │                        │
│          │  result   │              │           │  Job types:            │
│          │◀═════════ │              │           │  · bulk-export         │
└──────────┘           └──────────────┘           │  · cohort              │
                                                  │  · patient-export      │
                                                  │  · bulk-import         │
                                                  │  · reprocess           │
                                                  └───────────┬────────────┘
                                                              │
                              ┌──────────────────────────────┼─────────────┐
                              ▼                              ▼             ▼
                     ┌─────────────────┐        ┌────────────────┐  ┌──────────┐
                     │ Standard Worker │        │ Staged Worker  │  │ Result   │
                     │                 │        │ (opt-in)       │  │ Storage  │
                     │ FHIR fetch      │        │                │  │          │
                     │ → pipeline      │        │ Phase 1: stage │  │ local or │
                     │ → write NDJSON  │        │ to PostgreSQL  │  │ S3/MinIO │
                     │ → checkpoint    │        │                │  │          │
                     │                 │        │ Phase 2: read  │  └──────────┘
                     │ Crash recovery: │        │ pending → proc │
                     │ resume from     │        │ → mark done    │
                     │ checkpoint      │        │                │
                     └─────────────────┘        │ Crash recovery:│
                                                │ exact resume   │
                                                │ from phase +   │
                                                │ cursor position│
                                                └────────────────┘
```

### 5.2 Backend Selection

| Condition | Backend | Dispatch | Use Case |
|---|---|---|---|
| `MEDANON_REDIS_URL` set | `RedisJobStore` | Event-driven (Streams + `XREADGROUP`) | Production (multi-replica) |
| `MEDANON_REDIS_URL` unset | `SqliteJobStore` | Polling (2s interval) | Development (single-instance) |

### 5.3 Staged Worker (Two-Phase Processing)

For maximum durability, bulk jobs can opt into PostgreSQL staging:

```
Phase 1: Ingest                    Phase 2: Process
─────────────────                  ──────────────────
FHIR Server                       PostgreSQL
    │                                  │
    │  fetch pages                     │  SELECT pending
    ▼                                  │  FOR UPDATE SKIP LOCKED
┌──────────┐                           ▼
│ stage_   │                      ┌──────────┐
│ batch()  │──▶ PostgreSQL        │ process  │──▶ NDJSON
│          │    staged_resources  │ _batch() │    result file
└──────────┘    (ON CONFLICT      └──────────┘
                 DO NOTHING)           │
                                       ▼
                                  mark_done() / mark_error()
```

Crash at any point resumes from the exact phase and cursor position — no FHIR re-fetch needed.

---

## 6. Integration Layer — Deep Dive

### 6.1 gPAS Integration

```
┌─────────────────────────────────────────────────────────────┐
│  gPAS Client Stack                                          │
│                                                             │
│  pipeline (PseudonymizerPort)                               │
│      │                                                      │
│      ▼                                                      │
│  adapter.py ─── GpasPseudonymizerAdapter                    │
│      │                                                      │
│      ▼                                                      │
│  client.py ──── gpas_pseudonymize_batch()                   │
│      │           · cache lookup (L1 → L2)                   │
│      │           · deduplicate values                       │
│      │           · chunk into sub-batches (max 500)         │
│      │           · parallel execution (up to 8 concurrent)  │
│      │           · retry failed chunks                      │
│      ▼                                                      │
│  transport.py ── HTTP layer                                 │
│      │           · urllib3 connection pool                   │
│      │           · retry + exponential backoff + jitter     │
│      │           · circuit breaker gate                     │
│      │           · Prometheus metrics                       │
│      ▼                                                      │
│  protocol.py ── FHIR Parameters request/response builder   │
│                                                             │
│  Cache: TieredCache(LocalLruCache[50K], RedisCache)         │
│  Circuit Breaker: 3-state (CLOSED→OPEN→HALF_OPEN)          │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 FHIR Client

```
┌────────────────────────────────────────────────────────────┐
│  FHIR Client Stack                                         │
│                                                            │
│  adapter.py ─── HttpFhirClientAdapter (FhirClientPort)     │
│      │                                                     │
│      ├── reader.py                                         │
│      │    · fetch_resource_type (paginated search)         │
│      │    · fetch_everything ($everything)                 │
│      │    · fetch_all_resource_types (parallel multi-type) │
│      │    · fetch_cohort (search + $everything per match)  │
│      │                                                     │
│      ├── writer.py                                         │
│      │    · upload_resources (topological ordering)        │
│      │    · post_bundle (batch Bundles)                    │
│      │    · reference rewriting for uploaded resources     │
│      │                                                     │
│      ├── bulk.py                                           │
│      │    · bulk_export_kick_off → poll → download NDJSON  │
│      │    · Retry-After polling, parallel download         │
│      │                                                     │
│      └── _transport.py                                     │
│           · urllib3 pool, retry, circuit breaker            │
│           · ID validation, SSRF-safe pagination            │
└────────────────────────────────────────────────────────────┘
```

### 6.3 NLP Integration (Strangler Fig)

```
  Pipeline calls nlp_detect_by_path()
      │
      ▼
  deidentify.py → _get_nlp_adapter() (lazy singleton)
      │
      ├── NLP_SERVICE_URL set?
      │       │
      │       ▼
      │   RemoteNlpAdapter ──HTTP──▶ NLP Microservice
      │       │                      (Presidio + spaCy)
      │       │                      ~800 MB image
      │       ├── circuit breaker (3-state)
      │       └── fail mode: always fail-closed → [NLP_UNAVAILABLE] marker
      │
      └── NLP_SERVICE_URL unset?
              │
              ▼
          LocalPresidioAdapter ── in-process Presidio + spaCy
              · deterministic [[TYPE_N]] surrogate tokens
              · XHTML-safe (scrubs text nodes only)
```

### 6.4 Shared Resilience Patterns

All integration clients share these patterns:

```
┌────────────────────────────────────────────────────────┐
│  Circuit Breaker (circuit_breaker.py)                  │
│                                                        │
│  CLOSED ──failure_threshold──▶ OPEN                    │
│    ▲                             │                     │
│    │                      recovery_timeout             │
│    │                             ▼                     │
│    └──probe_success──── HALF_OPEN                      │
│                          (limited probes)              │
│                                                        │
│  Each integration has its own breaker instance:        │
│  gPAS / FHIR / NLP / Analytics                        │
└────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────┐
│  Tiered Cache (cache.py)                               │
│                                                        │
│     get(key)                                           │
│       │                                                │
│       ├─ L1 hit (LocalLruCache, 50K entries) → return  │
│       │                                                │
│       ├─ L2 hit (RedisCache, shared) → promote to L1   │
│       │                                                │
│       └─ miss → fetch from source → store L1 + L2     │
│                                                        │
│  Redis errors are swallowed — cache never blocks       │
│  processing.                                           │
└────────────────────────────────────────────────────────┘
```

---

## 7. Data Flow

### 7.1 Synchronous Processing (single/batch)

```
Input (JSON / NDJSON / XML)
    │
    ▼
io_formats.py ─────── detect format, parse to dict(s)
    │
    ▼
config/service.py ──── resolve profile, load YAML rules (LRU cached)
    │
    ▼
processor.py ─────────  orchestrate:
    │
    ├─▶ rule_matcher ──── resource type → rules (O(1) index)
    │                      FHIRPath eval (4 fast-path tiers)
    │
    ├─▶ action_dispatcher  PASS 1: non-gPAS actions execute immediately
    │                       gPAS actions → BatchWork (deferred)
    │
    ├─▶ gpas_orchestrator   PASS 2: deduplicate → single gPAS HTTP call
    │                       → write back pseudonyms
    │
    ├─▶ post_processor ──── rewrite FHIR references
    │                       replace IDs in free-text (Aho-Corasick)
    │
    └─▶ manifest ─────────  attach audit trail to meta.tag
    │
    ▼
io_formats.py ─────── serialize to requested format
    │
    ▼
Output (JSON / NDJSON / XML)
```

### 7.2 Async Bulk Processing

```
Client                     API Server               Worker                 FHIR Server
  │                           │                        │                       │
  │  POST /v1/jobs/*          │                        │                       │
  │──────────────────────────▶│                        │                       │
  │  202 {job_id}             │  create job            │                       │
  │◀──────────────────────────│  → notify worker       │                       │
  │                           │───────────────────────▶│                       │
  │                           │                        │── fetch resources ───▶│
  │                           │                        │◀─────────────────────│
  │                           │                        │                       │
  │                           │                        │── process via         │
  │                           │                        │   pipeline (batches)  │
  │                           │                        │── checkpoint          │
  │                           │                        │── write NDJSON        │
  │                           │                        │                       │
  │  GET /v1/jobs/{id}        │                        │                       │
  │──────────────────────────▶│  poll status            │                       │
  │  {status: "running",      │                        │                       │
  │   progress: 75%}          │                        │                       │
  │◀──────────────────────────│                        │                       │
  │                           │                        │── mark done ─────────▶│
  │  GET /v1/jobs/{id}/result │                        │                       │
  │──────────────────────────▶│  stream NDJSON         │                       │
  │◀══════════════════════════│  (or S3 presigned URL) │                       │
```

### 7.3 FHIR Round-Trip with Prefetch Pipelining

```
┌────────────┐         ┌──────────────────────────────────────┐        ┌─────────────┐
│ Source FHIR│         │         FhirServerService             │        │ Target FHIR │
│   Server   │         │                                      │        │   Server    │
└─────┬──────┘         │  ┌──────────┐       ┌─────────────┐ │        └──────┬──────┘
      │                │  │ Producer │       │  Consumer   │ │               │
      │  page 1        │  │ (async)  │       │  (async)    │ │               │
      │◀───────────────│──│          │       │             │ │               │
      │  page 2        │  │ fetch ──▶│Queue  │◀── process  │ │               │
      │◀───────────────│──│ pages    │(bound)│   batches   │ │               │
      │  ...           │  │          │       │             │ │               │
      │                │  └──────────┘       └──────┬──────┘ │               │
      │                │                            │        │               │
      │                │           collect all results        │               │
      │                │                            │        │               │
      │                │                  topological sort    │               │
      │                │                            │        │               │
      │                │                    upload batches ──│──────────────▶│
      │                │                                     │               │
      │                └─────────────────────────────────────┘               │
```

---

## 8. External Integrations

| Service | Technology | Role |
|---|---|---|
| **gPAS** | WildFly, FHIR `$pseudonymize` | Generates and stores deterministic pseudonyms for identifiers |
| **HAPI FHIR (Source)** | HAPI v7.6, PostgreSQL | Source FHIR R4 server with identified patient data |
| **HAPI FHIR (Target)** | HAPI v7.6, PostgreSQL | Target FHIR R4 server receiving de-identified data |
| **Redis** | Redis 7 | Shared L2 cache for gPAS results + event-driven job queue (Streams) |
| **PostgreSQL** | psycopg2 | Staging table for two-phase durable bulk processing (opt-in) |
| **MinIO / S3** | S3 API | Object storage for large job result files (opt-in) |
| **NLP Microservice** | Presidio + spaCy | Extracted NER-based PHI detection (opt-in, strangler fig) |
| **Analytics Microservice** | FastAPI | Extracted risk analysis + synthetic data generation (opt-in) |
| **MySQL** | MySQL 8.0 | Backend database for gPAS pseudonym storage |

---

## 9. Key Design Patterns

### Multi-Pass Pipeline
Two-pass architecture minimizes external HTTP calls. Pass 1 executes all non-gPAS actions immediately. Pass 2 deduplicates all gPAS values across all resources into a single batch call. This reduces gPAS round trips from O(N × rules) to O(1).

### Port & Adapter (Hexagonal)
The pipeline depends on abstract ports (`PseudonymizerPort`, `FhirClientPort`, `NlpAdapter`), never on HTTP transport code. Adapters (`GpasPseudonymizerAdapter`, `HttpFhirClientAdapter`, etc.) bridge from ports to infrastructure. This enables testing the pipeline with in-memory fakes.

### Strangler Fig Extraction
NLP and Analytics capabilities can be extracted to dedicated microservices by setting a single env var. Both degrade gracefully when the microservice is unavailable — the monolith continues to work.

### Circuit Breaker + Retry + Backoff
Every external integration has an independent 3-state circuit breaker (CLOSED → OPEN → HALF_OPEN) combined with retry and exponential backoff + jitter. Prevents cascade failures.

### Tiered Caching (L1 + L2)
gPAS pseudonym lookups use in-process LRU (50K entries) as L1 and shared Redis as L2 with promotion on hit. Redis errors are swallowed — cache failure never blocks processing.

### Event-Driven Job Queue
Redis Streams with consumer groups provide at-least-once delivery for production. SQLite polling (2s) provides a zero-dependency fallback. Checkpoint/resume enables crash recovery without re-fetching from FHIR.

### Prefetch Pipelining
FHIR server workflows overlap network I/O with CPU processing using a bounded `asyncio.Queue` — a producer fetches pages while the consumer processes the previous batch.

### FHIRPath Fast-Path Tiers
Rule matching classifies expressions into 4 evaluation tiers (simple → wildcard → native `.where()` → full engine) to minimize overhead. Results of the full engine are LRU-cached.

### Config-Driven Rules
All transformation behavior is defined in YAML. Six bundled compliance profiles (HIPAA, GDPR, IRB, etc.) plus user-defined profiles via API. Env var interpolation (`${VAR:-default}`) allows runtime configuration.

---

## 10. Technology Stack

| Area | Technologies |
|---|---|
| **Runtime** | Python 3.12, FastAPI, uvicorn, asyncio |
| **FHIR** | HAPI FHIR v7.6, fhirpathpy, defusedxml |
| **Pseudonymization** | gPAS (WildFly), HMAC-SHA3-256, RSA-OAEP (PyCryptodome) |
| **NLP** | Presidio Analyzer, spaCy `en_core_web_lg` |
| **Databases** | PostgreSQL 16, MySQL 8.0, SQLite (WAL), Redis 7 |
| **Observability** | Prometheus counters/histograms, structured JSON audit logs |
| **Infrastructure** | Docker Compose, Helm/K8s, nginx, MinIO (S3) |
| **Performance** | orjson (3-10x JSON), pyahocorasick (text search), LRU caches |
| **Security** | CSPRNG (`secrets`), SSRF guards, path-traversal guards, rate limiting |
