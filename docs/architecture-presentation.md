# MedAnon — System Architecture

```
┌───────────────────────────────────────────────────────────┐
│                   OPERATOR / RESEARCHER                    │
└──────────┬──────────────────────────────┬──────────────────┘
           │ Browser                      │ REST API / CLI
           ▼                              ▼
┌────────────────┐    ┌──────────────────────────────────────────────────────┐
│  React UI      │    │           MedAnon Anonymizer  (:8000)                │
│  (:8501)       │───▶│                                                      │
│                │    │  ┌────────────────────────────────────────────────┐  │
│  patient browse│    │  │  API — auth · RBAC · rate-limit · audit       │  │
│  de-identify   │    │  └──────────────────────┬─────────────────────────┘  │
│  batch jobs    │    │                         │                             │
│  risk / synth  │    │  ┌──────────────────────▼─────────────────────────┐  │
│  config editor │    │  │  Services                                      │  │
└────────────────┘    │  │  Processing · FhirServer · Jobs · Risk · Synth │  │
                      │  └──────────────────────┬─────────────────────────┘  │
                      │                         │                             │
                      │  ┌══════════════════════▼══════════════════════════┐ │
                      │  ║         PROCESSING PIPELINE                    ║ │
                      │  ║                                                ║ │
                      │  ║  Config (6 YAML profiles + user-defined)       ║ │
                      │  ║  I/O Formats (JSON · NDJSON · XML)             ║ │
                      │  ║                                                ║ │
                      │  ║  ┌──────────────────────────────────────────┐  ║ │
                      │  ║  │ PASS 1 — Rule Match + Action Dispatch   │  ║ │
                      │  ║  │                                          │  ║ │
                      │  ║  │ FHIRPath eval (4 tiers: simple → full)  │  ║ │
                      │  ║  │                                          │  ║ │
                      │  ║  │ Execute: redact · cryptohash · encrypt  │  ║ │
                      │  ║  │   perturb · generalize · scrub_text    │  ║ │
                      │  ║  │   nlp_detect                            │  ║ │
                      │  ║  │ Defer:   gpas_pseudonymize → BatchWork  │  ║ │
                      │  ║  └────────────────────┬─────────────────────┘  ║ │
                      │  ║                       │                         ║ │
                      │  ║  ┌────────────────────▼─────────────────────┐  ║ │
                      │  ║  │ PASS 2 — gPAS Batch Pseudonymization    │  ║ │
                      │  ║  │ deduplicate → ONE HTTP call → write back │  ║ │
                      │  ║  └────────────────────┬─────────────────────┘  ║ │
                      │  ║                       │                         ║ │
                      │  ║  ┌────────────────────▼─────────────────────┐  ║ │
                      │  ║  │ PASS 3 — Post-Processing                 │  ║ │
                      │  ║  │ rewrite references · replace text IDs   │  ║ │
                      │  ║  │ attach manifest                          │  ║ │
                      │  ║  └──────────────────────────────────────────┘  ║ │
                      │  ╚═════════════════════╤══════════════════════════╝ │
                      │                        │                            │
                      │  ┌─────────────────────▼────────────────────────┐   │
                      │  │  Integration Clients (port/adapter pattern)  │   │
                      │  │                                               │   │
                      │  │  gPAS Adapter    FHIR Client    NLP Adapter  │   │
                      │  │  batch + cache   read/write     local or     │   │
                      │  │  + circuit brk   bulk export    remote       │   │
                      │  │  + retry         + circuit brk  + circuit brk│   │
                      │  │                                               │   │
                      │  │  Analytics Proxy · Shared HTTP Pool           │   │
                      │  └─────────────────────┬────────────────────────┘   │
                      └────────────────────────┼────────────────────────────┘
                                               │
                      ┌────────────────────────┼────────────────────────────┐
                      │  Worker (:8000 image)   │  async job executor       │
                      │  bulk-export · cohort · import · reprocess         │
                      └────────────────────────┼────────────────────────────┘
                                               │
═══════════════════════════ internal network ═══╪══════════════════════════
                                               │
    ┌──────────────────────────────────────────┼──────────────────────────┐
    │  DATA LAYER                              │                          │
    │                                          │                          │
    │  ┌──────────────────┐  ┌─────────────────▼──────────────────────┐  │
    │  │ Redis (:6379)    │  │ FHIR Servers                           │  │
    │  │ cache + job queue│  │                                         │  │
    │  └──────────────────┘  │ Source (:8081) ◀── PostgreSQL           │  │
    │                        │ Target (:8082) ◀── PostgreSQL           │  │
    │  ┌──────────────────┐  └─────────────────────────────────────────┘  │
    │  │ Result Storage   │                                               │
    │  │ local / S3       │                                               │
    │  └──────────────────┘                                               │
    └─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────────────┐
    │  PSEUDONYMISATION                                                   │
    │                                                                     │
    │  gPAS (TTP) (:8080) ◀── MySQL                                      │
    │  FHIR $pseudonymize · deterministic domain-scoped pseudonyms       │
    └─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────────────┐
    │  OPT-IN MICROSERVICES  (strangler fig)                              │
    │                                                                     │
    │  ┌─────────────────────────┐  ┌──────────────────────────────────┐ │
    │  │ Analytics (:8100)       │  │ NLP (:8200)                      │ │
    │  │ risk · synthetic        │  │ Presidio + spaCy PHI detection   │ │
    │  └─────────────────────────┘  └──────────────────────────────────┘ │
    └─────────────────────────────────────────────────────────────────────┘
```

---

## How It Works

Everything begins with the **operator or researcher** who needs to strip sensitive patient data from FHIR resources before they can be used for research, analytics, or cross-institution sharing. They have two ways in: a **React UI** for interactive work — browsing patients, running one-off de-identification, managing batch jobs — or the **REST API / CLI** for automated pipelines and system integration.

Both paths lead into the **MedAnon Anonymizer**, the core backend. Every request first passes through the **API layer**, which handles authentication, role-based access control, rate limiting, and audit logging. Nothing reaches the processing engine without going through this gate.

Once past the API, the request enters the **Service layer** — a set of business-logic orchestrators that are completely decoupled from HTTP. The `ProcessingService` handles single resources and streams. The `FhirServerService` coordinates multi-step workflows like fetching from a source FHIR server, de-identifying, and uploading to a target. The `JobService` manages long-running bulk operations. Separate services handle risk analysis and synthetic data generation.

The real work happens inside the **Processing Pipeline**, the heart of the system. It operates in three passes:

**Pass 1** loads the active configuration profile — one of six bundled YAML profiles (HIPAA, GDPR, research, and others) or a user-defined one — and evaluates FHIRPath expressions against each resource to determine which rules apply. Matched fields are immediately transformed by actions like `redact`, `cryptohash`, `generalize`, `scrub_text`, or `nlp_detect`. Any field that requires pseudonymisation through gPAS is not processed yet — it is collected into a deferred batch.

**Pass 2** takes all those deferred values — across every resource in the batch — deduplicates them, and sends them to gPAS in a **single HTTP call**. This is a critical optimisation: instead of making one network round trip per field per resource, the system makes exactly one call per batch, regardless of size. The returned pseudonyms are written back into each resource.

**Pass 3** handles the consequences of the identity changes. FHIR resources reference each other by ID — a Condition points to a Patient, an Observation points to an Encounter. When those IDs change, every reference must be rewritten to maintain consistency. The post-processor also scans free-text narrative fields and replaces any occurrence of the original IDs with their pseudonyms, then attaches a transformation manifest.

Below the pipeline sits the **Integration layer**, built on the port/adapter pattern. The pipeline never calls external services directly — it talks to abstract ports, and adapters handle the infrastructure. The **gPAS adapter** manages batched pseudonymisation with a tiered cache, circuit breaker, and retry logic. The **FHIR client** handles paginated reads, topologically-ordered uploads, and bulk data export. The **NLP adapter** routes PHI detection either to an in-process Presidio engine or to a remote microservice. Each adapter has its own circuit breaker to prevent cascade failures.

For operations that take minutes or hours — bulk exports, cohort processing, large imports — the **Worker** takes over. It runs the same anonymiser image but without exposing HTTP. It picks up jobs from a Redis-backed queue, processes them through the same pipeline, checkpoints progress for crash recovery, and writes results as NDJSON files.

Below the internal network boundary sits the **Data layer**. **Redis** serves two purposes: a shared L2 cache for pseudonym lookups (with an in-process L1 above it) and an event-driven job queue using Streams. The **FHIR servers** — a source holding identified data and a target receiving de-identified data — each run HAPI FHIR backed by PostgreSQL. Completed job results land in **local storage or S3**.

The **Pseudonymisation layer** is its own isolated system. **gPAS**, a Trusted Third Party service, generates and stores deterministic, domain-scoped pseudonyms. It ensures the same input always produces the same pseudonym within a domain, which is essential for longitudinal research where records must remain linkable without revealing real identities. gPAS persists everything in its own MySQL database.

Finally, two capabilities can be **extracted to dedicated microservices** without changing the core: the **Analytics** service (risk scoring and synthetic data generation) and the **NLP** service (Presidio + spaCy for named entity recognition). Setting a single environment variable redirects traffic to the external service. If that service goes down, the system degrades gracefully — the core continues to operate.
