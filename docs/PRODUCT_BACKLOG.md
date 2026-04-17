# SPE FHIR BlackBox — Product Backlog & Sprint History

> Scrum-inspired project management artifact.
> Reconstructed from 164 commits across 26 days (2026-03-20 → 2026-04-14).

---

## Product Vision

Build a **rule-driven FHIR de-identification and pseudonymization engine** that accepts FHIR R4 resources, applies configurable match-action rules from YAML profiles, integrates with gPAS for pseudonymization and Presidio NLP for entity detection, and outputs privacy-safe data — compliant with HIPAA Safe Harbor, GDPR Art. 4(5), and IRB research protocols.

## Product Goal

Deliver a production-ready, containerized medical data privacy toolkit that:
1. De-identifies FHIR resources via a REST API and CLI
2. Supports 7+ compliance profiles out of the box
3. Scales horizontally (gPAS, NLP, workers)
4. Provides async bulk processing with crash recovery
5. Includes a scoring engine (privacy + utility + quality)
6. Offers a React SPA for operator workflows

---

## Epics (Product Backlog — ordered by delivery)

### EPIC-1: Foundation & Core Engine
**Goal:** Establish working environment, core pipeline, and initial Docker stack.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-1.1 | **Research & Environment Setup** — VM provisioning, Python 3.12 selection, Docker Compose baseline, HAPI FHIR R4 evaluation, gPAS TTP evaluation | XL | P0 |
| PBI-1.2 | **Core De-identification Pipeline** — FHIRPath rule matcher, action dispatcher (redact, cryptohash, encrypt, perturb, substitute, generalize), YAML config loader with `${VAR:-default}` interpolation | XL | P0 |
| PBI-1.3 | **FastAPI REST Service** — `/process` endpoint (JSON, NDJSON, XML), input/output format handling (defusedxml), Pydantic schemas, health/ready endpoints | L | P0 |
| PBI-1.4 | **gPAS Integration** — HTTP client with retry + exponential backoff, pseudonymization batch API, domain template import script | L | P0 |
| PBI-1.5 | **Docker Compose Stack** — 5-service architecture (anonymizer, fhir-server, hapi-db, gpas, gpas-db), networking, healthchecks, volume mounts | L | P0 |
| PBI-1.6 | **HMAC Cryptographic Hashing** — SHA3-256 + HMAC keyed hashing action, RSA encrypt/decrypt actions, CSPRNG bounded random for perturbation | M | P0 |
| PBI-1.7 | **Initial Config Profiles** — `config.yaml` (minimal), `config_gpas.yaml` (production), auto-selection based on `GPAS_URL` presence | M | P0 |
| PBI-1.8 | **Test Foundation** — pytest suite, test fixtures, API endpoint tests, path configuration | M | P1 |

---

### EPIC-2: Authentication & API Hardening
**Goal:** Secure the API, simplify auth, establish CI/CD.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-2.1 | **Keycloak OIDC Authentication** — Dual API key + OIDC bearer token support, Keycloak proxy integration | L | P0 |
| PBI-2.2 | **Auth Simplification** — Remove Keycloak dependency, simplify to API-key-only auth, RBAC (admin/analyst/viewer), open-mode for dev | L | P0 |
| PBI-2.3 | **API Router Modularization** — Extract monolithic endpoint file into focused routers (process, fhir_server, fhir_bulk, jobs, configs, analytics, synthetic) | M | P1 |
| PBI-2.4 | **SSRF Protection** — Hostname validation against private network ranges, DNS resolution guard in `deps.py` | M | P0 |
| PBI-2.5 | **Rate Limiting** — slowapi integration with configurable limits, Redis-backed cross-replica storage | M | P1 |
| PBI-2.6 | **Audit Logging** — RotatingFileHandler + optional Redis Stream, method/path/status/subject/request_id per request | M | P1 |
| PBI-2.7 | **CI Pipeline** — GitHub Actions: ruff lint, bandit SAST, pytest, Helm lint, ESLint, Docker build | L | P1 |

---

### EPIC-3: Bulk Processing & FHIR Server Integration
**Goal:** Enable end-to-end FHIR server workflows and bulk data operations.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-3.1 | **FHIR Server Read Integration** — Paginated fetch, `$everything` operation, search queries, absolute URL handling | L | P0 |
| PBI-3.2 | **FHIR Bulk Export** — `$export` kick-off/poll/download, system/patient/group-level exports, NDJSON streaming | XL | P0 |
| PBI-3.3 | **Cohort Export** — Patient-list-based export with server-side filtering, batch patient export | L | P1 |
| PBI-3.4 | **CLI Bulk Operations** — `batch_process.sh`, `fetch.sh` push/pull, argument parsing for export type and config profile | M | P1 |
| PBI-3.5 | **FHIR Server Configuration** — Pagination limits, bulk export enablement, server-side config tuning | S | P1 |
| PBI-3.6 | **Kubernetes Deployment** — K3s values override, Helm umbrella chart, sub-charts for each service | L | P2 |

---

### EPIC-4: Compliance Profiles & Advanced Actions
**Goal:** Ship compliance-ready profiles for HIPAA, GDPR, and research use cases.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-4.1 | **HIPAA Safe Harbor Profile** — 18 PHI identifier categories, dates→year-only, ZIP→3-digit prefix, `config_hipaa_safe_harbor.yaml` | L | P0 |
| PBI-4.2 | **GDPR Art. 4(5) Profile** — HMAC pseudonymization per EU regulation, `config_gdpr_eu.yaml` | M | P0 |
| PBI-4.3 | **Research Pseudonymous Profile** — IRB-grade: dates→year-month, IDs cryptohashed for longitudinal linkage, `config_research_pseudonymous.yaml` | M | P1 |
| PBI-4.4 | **Structure-Preserving Profile** — Full FHIR structure retained, IDs via gPAS, PII→REDACTED, dates→year, `config_structure_preserving.yaml` | M | P1 |
| PBI-4.5 | **Value-Masking Profile** — Entity-specific conditional NLP (`nlp_detect_act`), encrypt+generalize combos, `config_value_masking.yaml` | L | P1 |
| PBI-4.6 | **Generalize Action Strategies** — `date_shift`, `date_year`, `date_year_instant`, `age_bracket`, `zip_3digit`, `address_state_only`, `name_initial`, `date_year_month` | M | P1 |
| PBI-4.7 | **Text Scrubbing (NLP)** — Presidio NER integration, deterministic `[[TYPE_N]]` tokenization, XHTML-safe replacement, `scrub_text` action | L | P0 |

---

### EPIC-5: NLP Detection Service
**Goal:** Extract NLP/Presidio into an opt-in scalable microservice.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-5.1 | **Local Presidio Adapter** — In-process spaCy `en_core_web_lg` + Presidio AnalyzerEngine, lazy singleton init | L | P0 |
| PBI-5.2 | **NLP Microservice** — Standalone FastAPI (POST /v1/detect, /v1/detect/batch), ~800MB image, Presidio + spaCy | L | P1 |
| PBI-5.3 | **Remote NLP Adapter** — HTTP client for NLP microservice, `detect_batch_remote()`, fail-closed `[NLP_UNAVAILABLE]` | M | P1 |
| PBI-5.4 | **NLP Load Balancer** — nginx least-conn balancing across NLP replicas, 120s batch timeout, 10m body limit | M | P1 |
| PBI-5.5 | **NLP Batch Orchestrator** — Pass 1.5: extract texts, dedup, single batch HTTP call, per-resource replacement with `token_state` isolation | L | P0 |
| PBI-5.6 | **Conditional NLP** — `nlp_detect_act`: entity-specific actions (e.g., redact names but pseudonymize MRNs), `nlp_detect_by_path` for FHIRPath-scoped NLP | M | P1 |

---

### EPIC-6: Frontend — React SPA
**Goal:** Replace Streamlit prototype with a production React SPA.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-6.1 | **Streamlit Prototype** — Initial data viewing UI for FHIR resources | M | P2 |
| PBI-6.2 | **React SPA Migration** — React 19 + TypeScript 5.9, Vite 8 + Tailwind CSS 4 + Shadcn/ui, 13 lazy-loaded routes | XL | P0 |
| PBI-6.3 | **Patient Browser** — Paginated FHIR Patient list, PII field summary, resource type discovery, export-by-type | L | P1 |
| PBI-6.4 | **Bulk De-identify Page** — Job cards with progress bars, field summary, cancel/stop, checkpoint display | L | P1 |
| PBI-6.5 | **Config Profile Management** — CRUD grid for config profiles, visual rule editor (Config Builder) | L | P1 |
| PBI-6.6 | **Status Dashboard** — Service health grid, system metrics, worker status | M | P1 |
| PBI-6.7 | **Risk Assessment & Synthetic Data** — Analytics proxy pages for k-anonymity risk and SDV generation | M | P2 |
| PBI-6.8 | **Target FHIR Browser** — Navigate de-identified data in the target FHIR server | M | P2 |
| PBI-6.9 | **Bulk Import** — Upload NDJSON→target FHIR server with progress tracking and error handling | M | P1 |
| PBI-6.10 | **Scoring UI** — Quality score integration in export panel and per-resource views | M | P1 |

---

### EPIC-7: Async Job System & Worker Architecture
**Goal:** Handle long-running bulk operations reliably at scale.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-7.1 | **SQLite Job Store** — WAL-mode, thread-safe, list/filter/paginate, polling-based (2s) | L | P0 |
| PBI-7.2 | **Redis Job Store** — Hash + sorted set + secondary index, BLPOP event-driven, cross-replica | L | P0 |
| PBI-7.3 | **PostgreSQL Job Store** — LISTEN/NOTIFY event-driven, `medanon.jobs` table | L | P1 |
| PBI-7.4 | **Async Job Worker** — Semaphore-bounded `max_concurrent`, status lifecycle (pending→running→done/failed), embedded + dedicated modes | L | P0 |
| PBI-7.5 | **Job Cancellation** — Cancel API endpoint, cooperative cancellation in worker loop, SQLite + Redis + Postgres support | M | P1 |
| PBI-7.6 | **Checkpoint & Resume** — Crash recovery with `update_checkpoint()`, resumable bulk exports, `_last_page`/`_processed_count` persistence | L | P1 |
| PBI-7.7 | **Dedicated Worker Container** — Separate `worker_main.py` entrypoint, Prometheus metrics on port 9091, independent scaling | M | P1 |
| PBI-7.8 | **Pluggable Result Storage** — Filesystem (default) or S3 (MinIO), `MEDANON_RESULT_STORAGE` switch, TTL-based cleanup | M | P2 |
| PBI-7.9 | **Staged Worker (Two-Phase Bulk)** — PostgreSQL staging table, `FOR UPDATE SKIP LOCKED`, batch processing with cross-chunk dedup | L | P1 |

---

### EPIC-8: Architecture Refactoring (Phase 2)
**Goal:** Decompose the monolith into maintainable, testable modules.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-8.1 | **Pipeline Decomposition** — Split monolithic `processor.py` into rule_matcher, action_dispatcher, post_processor, manifest | XL | P0 |
| PBI-8.2 | **medanon-core Shared Library** — Zero-dependency domain types (Job, JobStatus, exceptions), editable install for dev | L | P0 |
| PBI-8.3 | **Integration Layer Refactoring** — Decompose gPAS client into adapter + transport + circuit-breaker; NLP into adapter + remote detector | L | P1 |
| PBI-8.4 | **Service Layer Extraction** — Pydantic schemas in `api/schemas/`, business logic in `api/services/`, thin routers | L | P1 |
| PBI-8.5 | **Utils Consolidation** — CacheBackend Protocol, IO format helpers, FHIRPath utilities, crypto module, metrics module | M | P1 |
| PBI-8.6 | **Backend Sub-package Restructure** — Flatten `integrations/` into postgres/, redis/, gpas/, nlp/, fhir/, staging/ sub-packages | L | P1 |

---

### EPIC-9: Infrastructure & Observability (Phase 3)
**Goal:** Production-grade infrastructure: scaling, resilience, monitoring.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-9.1 | **Network Segmentation** — `processing-net` + `source-net` isolation; source FHIR has no host port | M | P0 |
| PBI-9.2 | **Container Hardening** — `read_only: true` + tmpfs, `cap_drop: ALL`, `no-new-privileges`, Redis password | M | P0 |
| PBI-9.3 | **gPAS Load Balancer** — nginx round-robin, WildFly-friendly timeouts, `/ping` liveness endpoint | M | P1 |
| PBI-9.4 | **Horizontal Scaling** — `docker compose --scale gpas=N --scale nlp=N`, LB auto-discovery via Docker DNS | M | P1 |
| PBI-9.5 | **gPAS PostgreSQL HA** — Read replica StatefulSet, streaming replication, `--profile ha` opt-in | L | P2 |
| PBI-9.6 | **Prometheus Metrics** — Request counters/histograms, gPAS calls/latency/cache, FHIR calls/latency, worker metrics | M | P1 |
| PBI-9.7 | **Circuit Breaker (3-State)** — CLOSED → OPEN → HALF_OPEN with configurable probes; applied to gPAS, NLP, FHIR | L | P0 |
| PBI-9.8 | **Tiered Caching** — L1 in-process LRU (50K entries, 10% eviction) + L2 Redis (error-swallowing), configurable per-integration | M | P1 |
| PBI-9.9 | **MinIO S3 Storage** — `--profile s3`, object storage for job results, presigned URL download | M | P2 |
| PBI-9.10 | **Helm Charts** — Umbrella chart + 7 sub-charts, HPA for worker (1-5 replicas), PDB, K3s overrides | XL | P1 |

---

### EPIC-10: Performance Optimization
**Goal:** Achieve high throughput for large-scale bulk exports.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-10.1 | **Parallel Pipeline Processing** — ThreadPoolExecutor for batch resources, configurable `MEDANON_PARALLEL_WORKERS` | L | P1 |
| PBI-10.2 | **orjson Fast-Path** — 3-10x faster JSON parse/serialize for hot paths | M | P1 |
| PBI-10.3 | **Native FHIRPath Evaluator** — Bypass fhirpathpy for common `.where(url=...)` patterns, lazy import, LRU cache | L | P1 |
| PBI-10.4 | **Rule Index Optimization** — `_build_rule_index` with per-resource-type candidate caching | M | P1 |
| PBI-10.5 | **gPAS Batch Dedup** — Cross-chunk `seen_values: set[str]` in executors, avoiding redundant pseudonymization calls | M | P1 |
| PBI-10.6 | **Import Script Optimization** — Batch bundles, HTTP keep-alive, parallel tiers for `import_testbase.sh` | M | P2 |
| PBI-10.7 | **Connection Pooling** — psycopg2 `ThreadedConnectionPool` with health checks for all PostgreSQL integrations | M | P1 |
| PBI-10.8 | **Global Thread Budget** — `MEDANON_GLOBAL_MAX_THREADS` cap to prevent nested executor deadlocks | M | P1 |

---

### EPIC-11: Scoring Engine
**Goal:** Quantitative assessment of de-identification quality, privacy, and utility.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-11.1 | **Privacy Score** — HIPAA identifier check, k-anonymity estimation, re-identification attacker model, PHI field residual detection | L | P0 |
| PBI-11.2 | **Utility Score** — Schema conformance, reference integrity, required field preservation, clinical completeness | L | P1 |
| PBI-11.3 | **Quality Score** — Composite: `0.5×privacy + 0.3×utility + 0.2×quality`, letter grade (A-F), PASS/FAIL gate | L | P0 |
| PBI-11.4 | **Audit Report** — Per-resource evidence trail, manifest-based scoring, batch-level statistics | M | P1 |
| PBI-11.5 | **Reservoir Sampling** — Batch-level k-anonymity with bounded memory via reservoir sampling | M | P1 |
| PBI-11.6 | **Scoring API** — `/v1/score` endpoint, integration with processing pipeline, configurable thresholds | M | P1 |

---

### EPIC-12: Multi-Format & Protocol Support
**Goal:** Extend beyond FHIR JSON to other healthcare data formats.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-12.1 | **FHIR XML Support** — defusedxml parsing, lxml serialization, full round-trip | M | P1 |
| PBI-12.2 | **FHIR NDJSON Support** — Line-delimited JSON streaming, bulk import/export | M | P0 |
| PBI-12.3 | **DICOM De-identification** — DICOM tag-level de-identification endpoints | L | P2 |
| PBI-12.4 | **HL7v2 De-identification** — HL7v2 message parsing and de-identification | L | P2 |
| PBI-12.5 | **FHIR Subscriptions** — R4 Subscription resource management, webhook dispatch, notification bundles | L | P2 |
| PBI-12.6 | **SMART on FHIR** — Launch/callback endpoints, token exchange, EHR integration | M | P2 |
| PBI-12.7 | **CDA Support** — Clinical Document Architecture de-identification module | M | P3 |

---

### EPIC-13: Analytics & Synthetic Data
**Goal:** Provide risk assessment and synthetic data generation capabilities.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-13.1 | **Risk Analytics Engine** — k-anonymity, l-diversity, re-identification risk in `medanon-core` | L | P1 |
| PBI-13.2 | **Analytics Microservice** — Standalone FastAPI, `/v1/analyse/risk`, `/v1/generate/synthetic`, Prometheus metrics | L | P1 |
| PBI-13.3 | **Synthetic Data Generation** — Condition generation, richer distributions, optional SDV engine | L | P2 |
| PBI-13.4 | **Strangler Fig Pattern** — `ANALYTICS_SERVICE_URL` / `NLP_SERVICE_URL` env-var toggles, graceful degradation | M | P1 |

---

### EPIC-14: Target FHIR Server & Round-Trip
**Goal:** Complete the de-identify→upload→browse cycle.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-14.1 | **Target FHIR Server** — Second HAPI FHIR instance for de-identified data, separate PostgreSQL backend | M | P1 |
| PBI-14.2 | **FHIR Upload (Writer)** — Batch Bundles, topological ordering, reference rewriting, server-assigned IDs | L | P0 |
| PBI-14.3 | **Round-Trip Endpoints** — `/process/and-upload`, `/process/round-trip`, `/process/from-server` | M | P1 |
| PBI-14.4 | **Post-Processor** — Reference rewriting after ID pseudonymization, text-ID replacement, display field cleanup | L | P0 |
| PBI-14.5 | **Transformation Manifest** — `meta.tag` audit trail: action/path/timestamp per transformation | M | P1 |

---

### EPIC-15: CI/CD & Supply Chain Security
**Goal:** Automated quality gates, vulnerability scanning, deployment pipeline.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-15.1 | **GitHub Actions CI** — Lint (ruff), format, SAST (bandit), unit tests, integration tests, Helm lint | L | P0 |
| PBI-15.2 | **Trivy Container Scanning** — CVE scanning of Docker images, `.trivyignore` for unfixable OS CVEs | M | P1 |
| PBI-15.3 | **Container CPU Limits** — Resource limits for 2-CPU GitHub Actions runners | S | P1 |
| PBI-15.4 | **Integration Test Environment** — Redis password, env file generation, medanon-core pre-install | M | P1 |

---

### EPIC-16: Documentation
**Goal:** Comprehensive documentation for operators, developers, and auditors.

| ID | Backlog Item | Size | Priority |
|---|---|---|---|
| PBI-16.1 | **Architecture Documentation** — Core architecture, pipeline design, 5-layer architecture overview, ASCII diagrams | L | P1 |
| PBI-16.2 | **Deployment Guide** — Docker Compose + Kubernetes/Helm, K3s instructions, env var catalog | L | P1 |
| PBI-16.3 | **Operations Runbook** — Monitoring, backup, troubleshooting, go-live checklist | L | P1 |
| PBI-16.4 | **User Manual** — Web UI guide, REST API reference, CLI reference, actions reference | L | P1 |
| PBI-16.5 | **Scoring System Reference** — Privacy × utility × quality model, API reference, thresholds | M | P1 |
| PBI-16.6 | **Component Catalog** — Service reference, module reference, integration reference | M | P2 |
| PBI-16.7 | **Technology Stack Audit** — PlanF.MD evolution roadmap, vulnerability assessment | M | P1 |

---

## Sprint History (Completed)

### Sprint 0 — Foundation (Pre-Project, before 2026-03-20)
**Sprint Goal:** Research, evaluate technologies, provision infrastructure.

| Item | Description | Status |
|---|---|---|
| Research FHIR R4 specification | HL7 FHIR R4 resource model, FHIRPath, Bundle structure, reference semantics | Done |
| Evaluate gPAS TTP | WildFly deployment, FHIR-based pseudonymization API, domain template format | Done |
| Evaluate HAPI FHIR Server | JPA backend, PostgreSQL, bulk export support, pagination | Done |
| Evaluate Presidio NLP | Entity recognition for PHI, spaCy models, custom recognizers | Done |
| Provision development VM | Linux environment, Docker, Docker Compose, Python 3.12 | Done |
| Design rule-driven architecture | FHIRPath match → named action dispatch, YAML configuration, env interpolation | Done |
| Design initial Docker stack | 5-service layout: anonymizer, fhir-server, hapi-db, gpas, gpas-db | Done |

**Sprint 0 Outcome:** Technology stack selected, architecture designed, VM ready.

---

### Sprint 1 — Core Engine & MVP API (2026-03-20)
**Sprint Goal:** Deliver a working de-identification pipeline with REST API.

| Item | Epic | PBI | Status |
|---|---|---|---|
| Core pipeline: matcher + dispatcher + actions | EPIC-1 | PBI-1.2 | Done |
| FastAPI REST service with `/process` | EPIC-1 | PBI-1.3 | Done |
| gPAS HTTP client with retry/backoff | EPIC-1 | PBI-1.4 | Done |
| Docker Compose 5-service stack | EPIC-1 | PBI-1.5 | Done |
| HMAC + RSA crypto actions | EPIC-1 | PBI-1.6 | Done |
| Initial config profiles (minimal + gPAS) | EPIC-1 | PBI-1.7 | Done |
| pytest suite foundation | EPIC-1 | PBI-1.8 | Done |
| Keycloak OIDC authentication | EPIC-2 | PBI-2.1 | Done |
| Performance baseline optimization | EPIC-1 | — | Done |

**Velocity:** 9 items | **Increment:** Working pipeline + API + Docker stack with auth

---

### Sprint 2 — Auth Simplification & Bulk Export (2026-03-23 → 2026-03-26)
**Sprint Goal:** Simplify auth, add bulk FHIR operations, establish CI.

| Item | Epic | PBI | Status |
|---|---|---|---|
| Remove Keycloak, simplify to API-key auth | EPIC-2 | PBI-2.2 | Done |
| Router modularization (7 router files) | EPIC-2 | PBI-2.3 | Done |
| Synthetic data generation engine | EPIC-13 | PBI-13.3 | Done |
| Config profile selector (Streamlit) | EPIC-4 | — | Done |
| Structure-preserving profile | EPIC-4 | PBI-4.4 | Done |
| Makefile targets + Docker healthchecks | EPIC-9 | — | Done |
| CLI + FHIR client + integration + SSRF tests | EPIC-2 | PBI-2.4 | Done |
| GitHub Actions CI pipeline | EPIC-15 | PBI-15.1 | Done |
| Bulk export + cohort export CLI | EPIC-3 | PBI-3.1, 3.2 | Done |
| FHIR bulk export endpoints (streaming) | EPIC-3 | PBI-3.2, 3.3 | Done |
| K3s deployment values | EPIC-3 | PBI-3.6 | Done |
| Documentation update (architecture, deployment, runbook) | EPIC-16 | PBI-16.1–16.4 | Done |

**Velocity:** 12 items | **Increment:** Simplified auth + bulk export + CI pipeline

---

### Sprint 3 — React SPA & Frontend Migration (2026-03-27)
**Sprint Goal:** Replace Streamlit with a production React SPA.

| Item | Epic | PBI | Status |
|---|---|---|---|
| React 19 + TypeScript SPA (replace Streamlit) | EPIC-6 | PBI-6.2 | Done |
| React UI Helm sub-chart | EPIC-9 | PBI-9.10 | Done |
| Docker Compose + nginx for React UI | EPIC-9 | — | Done |
| Targeted FHIRPath rules (replace blanket redaction) | EPIC-4 | — | Done |
| `/ready` auth fix for open-path routes | EPIC-2 | — | Done |

**Velocity:** 5 items | **Increment:** React SPA serving via nginx with API proxy

---

### Sprint 4 — Architecture Refactoring (Phase 2) (2026-03-30)
**Sprint Goal:** Decompose the monolith into focused, testable modules.

| Item | Epic | PBI | Status |
|---|---|---|---|
| medanon-core shared library (Job, JobStatus, exceptions) | EPIC-8 | PBI-8.2 | Done |
| Pipeline decomposition (processor → 5 sub-modules) | EPIC-8 | PBI-8.1 | Done |
| Async job queue + NLP adapter routing | EPIC-7 | PBI-7.1, 7.4 | Done |
| Pydantic schemas + service layer extraction | EPIC-8 | PBI-8.4 | Done |
| gPAS client decomposition (adapter + transport + CB) | EPIC-8 | PBI-8.3 | Done |
| NLP adapter layer + remote detector | EPIC-5 | PBI-5.3 | Done |
| FHIR adapter + Redis job store + analytics proxy | EPIC-7, 13 | PBI-7.2, 13.4 | Done |
| Utils consolidation (cache, IO, FHIRPath, metrics) | EPIC-8 | PBI-8.5 | Done |
| Analytics + NLP microservices | EPIC-5, 13 | PBI-5.2, 13.2 | Done |
| Helm analytics sub-chart + gPAS HA templates | EPIC-9 | PBI-9.5, 9.10 | Done |
| Checkpoint support for resumable bulk jobs | EPIC-7 | PBI-7.6 | Done |
| DICOM, HL7v2, CDA, Subscription modules | EPIC-12 | PBI-12.3–12.7 | Done |
| DICOM, HL7v2, FHIR Bulk, Subscriptions, SMART endpoints | EPIC-12 | PBI-12.3–12.6 | Done |
| React SPA updates (async job queue, batch ops) | EPIC-6 | PBI-6.4 | Done |
| Test suite update for refactored modules | EPIC-1 | — | Done |
| Bulk export UI + FHIR table visualization | EPIC-6 | PBI-6.4 | Done |
| gPAS circuit breaker implementation | EPIC-9 | PBI-9.7 | Done |

**Velocity:** 17 items | **Increment:** Fully modularized backend, microservice extraction, multi-format support

---

### Sprint 5 — Integration & Stabilization (Phase 2 → main) (2026-03-31)
**Sprint Goal:** Merge Phase-2 to main, fix CI, add config CRUD.

| Item | Epic | PBI | Status |
|---|---|---|---|
| Merge Phase-2 to Staging → main (PR #1) | — | — | Done |
| CI stabilization (6 fix rounds: deps, build context, ESLint) | EPIC-15 | PBI-15.1 | Done |
| Config CRUD API + SQLite-backed store | EPIC-6 | PBI-6.5 | Done |
| Config profile management pages (UI) | EPIC-6 | PBI-6.5 | Done |
| PII detection dropdowns + Config Builder | EPIC-6 | PBI-6.5 | Done |
| Bulk de-identify page redesign (job cards, progress, stop) | EPIC-6 | PBI-6.4 | Done |
| Job cancellation (SQLite + Redis + API) | EPIC-7 | PBI-7.5 | Done |
| Patient browser: resource type discovery + export-by-type | EPIC-6 | PBI-6.3 | Done |
| Rule match candidate caching (perf) | EPIC-10 | PBI-10.4 | Done |

**Velocity:** 9 items | **Increment:** Phase-2 in production, config management, job cancellation

---

### Sprint 6 — Infrastructure Hardening (Phase 3) (2026-04-02 → 2026-04-03)
**Sprint Goal:** Production infrastructure — target FHIR, staging, worker, scaling.

| Item | Epic | PBI | Status |
|---|---|---|---|
| Target FHIR server (fhir-target + hapi-target-db) | EPIC-14 | PBI-14.1 | Done |
| PostgreSQL staging layer (two-phase bulk) | EPIC-7 | PBI-7.9 | Done |
| Backend sub-package restructure | EPIC-8 | PBI-8.6 | Done |
| Client API layer split (focused modules + page subdirs) | EPIC-6 | — | Done |
| nginx body buffer fix (request disk spill) | EPIC-9 | — | Done |
| FHIR Extension value[x] config fixes | EPIC-4 | — | Done |
| HAPI-1094 cross-chunk referential integrity fix | EPIC-14 | PBI-14.2 | Done |
| Import script optimization (batch, keep-alive, parallel) | EPIC-10 | PBI-10.6 | Done |
| Supervised worker loop + tiered cache + worker separation | EPIC-7, 9 | PBI-7.7, 9.8 | Done |
| Parallel processing + prefetch buffer + rule index optim. | EPIC-10 | PBI-10.1, 10.4 | Done |
| Connection pools + pluggable result storage + NLP/gPAS hardening | EPIC-9, 10 | PBI-9.7, 10.7 | Done |
| Dedicated worker container + benchmark script | EPIC-7 | PBI-7.7 | Done |
| MinIO S3 storage integration | EPIC-9 | PBI-9.9 | Done |
| Docker: resource limits, NLP prewarm, network segmentation | EPIC-9 | PBI-9.1, 9.2 | Done |
| Phase 3 UI: StatusPage redesign, job detail, async export | EPIC-6 | PBI-6.6 | Done |
| Bulk import (UI + backend) | EPIC-6, 14 | PBI-6.9 | Done |
| orjson fast-path (3-10x JSON perf) | EPIC-10 | PBI-10.2 | Done |
| Native FHIRPath evaluator + lazy fhirpathpy + cache optim. | EPIC-10 | PBI-10.3 | Done |
| Complete Phase 3 documentation | EPIC-16 | — | Done |

**Velocity:** 19 items | **Increment:** Production infrastructure, target FHIR, staging, dedicated worker, S3

---

### Sprint 7 — Scoring, Profiles & CI Hardening (Phase 3 → main) (2026-04-09 → 2026-04-10)
**Sprint Goal:** Scoring engine, new profiles, merge Phase-3, harden CI.

| Item | Epic | PBI | Status |
|---|---|---|---|
| Merge Phase-3 to Staging → main | — | — | Done |
| Technology stack audit (PlanF.MD) | EPIC-16 | PBI-16.7 | Done |
| Wildcard path dedup fix (double rule execution) | EPIC-1 | — | Done |
| Manifest propagation fix (Bundle + process_data) | EPIC-14 | PBI-14.5 | Done |
| Value-masking de-identification profile | EPIC-4 | PBI-4.5 | Done |
| `date_year_instant` generalization strategy | EPIC-4 | PBI-4.6 | Done |
| Scoring engine (privacy + utility + quality composite) | EPIC-11 | PBI-11.1–11.3 | Done |
| Scoring UI integration (export panel + resource views) | EPIC-6 | PBI-6.10 | Done |
| Helm config profile sync | EPIC-9 | PBI-9.10 | Done |
| Pipeline hardening (batch, transport, NLP, CB) | EPIC-9 | PBI-9.7 | Done |
| Scoring system documentation | EPIC-16 | PBI-16.5 | Done |
| CI fix (ruff, bandit, ESLint, helm, tests — 3 passes) | EPIC-15 | PBI-15.1 | Done |
| Remove SQLite ConfigStore (migrate to PostgreSQL) | EPIC-8 | — | Done |
| Reservoir sampling for batch-level k-anonymity | EPIC-11 | PBI-11.5 | Done |
| HL7v2 + staging unit tests | EPIC-12 | — | Done |
| Trivy container scanning integration | EPIC-15 | PBI-15.2 | Done |
| CVE remediation (purge unused packages, .trivyignore) | EPIC-15 | PBI-15.2 | Done |
| CI resource limits for 2-CPU runners | EPIC-15 | PBI-15.3 | Done |

**Velocity:** 18 items | **Increment:** Scoring engine live, 7 profiles shipped, CI fully green with Trivy

---

### Sprint 8 — Current (Phase 3 Polish) (2026-04-11 → 2026-04-14)
**Sprint Goal:** Architecture review, backlog consolidation, stabilization.

| Item | Epic | PBI | Status |
|---|---|---|---|
| Full backend architecture review (5-area deep dive) | — | — | Done |
| Product backlog reconstruction (this document) | — | — | Done |
| Remaining Phase-3 branch items (uncommitted) | Multiple | Multiple | In Progress |

---

## Remaining Product Backlog (Unprioritized — Future Sprints)

### Hardening & Security

| ID | Item | Size | Epic |
|---|---|---|---|
| PBI-F.1 | Deny-by-default RBAC for unlisted endpoints | M | EPIC-2 |
| PBI-F.2 | Encrypt credentials in job store (bearer tokens) | L | EPIC-7 |
| PBI-F.3 | Atomic job claim (prevent double-claim across workers) | M | EPIC-7 |
| PBI-F.4 | SSRF validation on Subscription webhooks | M | EPIC-12 |
| PBI-F.5 | SQL parameterization in staging layer | S | EPIC-7 |
| PBI-F.6 | FileResponse path validation against output directory | S | EPIC-7 |
| PBI-F.7 | SSRF blocklist expansion (RFC 6598, IPv6 link-local) | S | EPIC-2 |
| PBI-F.8 | Rate limiting on all job submission endpoints | M | EPIC-2 |

### Reliability & Correctness

| ID | Item | Size | Epic |
|---|---|---|---|
| PBI-F.9 | Fix shared params race condition in action dispatcher | L | EPIC-1 |
| PBI-F.10 | PHI exposure guard on gPAS skip-mode failure | M | EPIC-1 |
| PBI-F.11 | NLP span offset sorting (prevent text corruption) | M | EPIC-5 |
| PBI-F.12 | Circuit breaker HALF_OPEN probe slot leak fix | M | EPIC-9 |
| PBI-F.13 | Background task retention (prevent GC cancellation) | S | EPIC-7 |
| PBI-F.14 | Checkpoint thread sentinel escape fix | S | EPIC-7 |
| PBI-F.15 | Staging recovery: use processing time not insert time | M | EPIC-7 |
| PBI-F.16 | `resource.clear()` guard in gPAS orchestrator | S | EPIC-1 |

### Performance & Scalability

| ID | Item | Size | Epic |
|---|---|---|---|
| PBI-F.17 | `lifespan` context manager (replace deprecated `on_event`) | M | EPIC-2 |
| PBI-F.18 | Async audit logging (QueueHandler or `to_thread`) | M | EPIC-2 |
| PBI-F.19 | Global thread budget enforcement | M | EPIC-10 |
| PBI-F.20 | Redis sorted-set TTL for job index | S | EPIC-7 |
| PBI-F.21 | Connection pool reuse in health probes | S | EPIC-9 |

### Compliance & Scoring

| ID | Item | Size | Epic |
|---|---|---|---|
| PBI-F.22 | Audit log: add client IP for HIPAA/GDPR compliance | S | EPIC-2 |
| PBI-F.23 | Scoring: fix false-positive risk 1.0 for sparse resources | M | EPIC-11 |
| PBI-F.24 | Scoring: SSN direct identifier should not pass at 0.15 | M | EPIC-11 |
| PBI-F.25 | Scoring: resource-type-aware quality field checks | M | EPIC-11 |
| PBI-F.26 | Implement `redact_if_rare` generalization strategy | M | EPIC-4 |

---

## Metrics Summary

| Metric | Value |
|---|---|
| **Total Epics** | 16 |
| **Total Product Backlog Items** | 120+ |
| **Completed Sprints** | 7 (+ Sprint 8 in progress) |
| **Total Commits** | 164 |
| **Project Duration** | 26 days (2026-03-20 → 2026-04-14) |
| **Avg Sprint Length** | ~3.5 days |
| **Avg Velocity** | 12.7 items/sprint |
| **Services Delivered** | 17 Docker containers |
| **Config Profiles** | 7 compliance profiles |
| **Test Count** | 418 tests |
| **React Pages** | 13 lazy-loaded routes |
| **Backend Modules** | 50+ Python modules |
| **Helm Sub-Charts** | 7 |

---

## Definition of Done

An item is considered Done when:
1. Code is implemented and passes `make lint` + `make format`
2. Unit/integration tests pass (`make test`)
3. Docker images build (`make build`) and healthchecks pass
4. Helm chart validates (`make helm-lint`)
5. Documentation updated (if user-facing)
6. Merged to `main` via Staging branch
7. CI pipeline green (ruff, bandit, pytest, ESLint, Helm lint, Trivy)
