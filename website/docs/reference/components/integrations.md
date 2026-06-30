---
title: "Integrations & Storage"
sidebar_position: 5
description: "FHIR client, analytics microservice, NLP microservice, FHIR Subscriptions, SMART on FHIR, staging layer, PostgreSQL stores, and domain types."
---

# Integrations & Storage

---

## FHIR Client (`src/integrations/fhir/`)

| Module | Role |
|---|---|
| `client.py` | Barrel file, re-exports from all sub-modules for backward compatibility. |
| `_transport.py` | Shared HTTP transport: connection pool, retry (2×, exponential backoff), pagination, ID validation. |
| `reader.py` | Paginated fetch (`fetch_resources`), `$everything`, bulk export status polling. |
| `writer.py` | `upload_resources`, batch Bundle PUT. Topological sort (`_infer_upload_tiers`) to satisfy referential integrity. |
| `bulk.py` | FHIR `$export` operation (async kick-off + poll + NDJSON file download). |
| `adapter.py` | `FhirServerAdapter`, unified interface wrapping reader + writer + bulk. |

**Upload ordering (topological sort):** HAPI rejects a resource if it references a resource that does not exist yet. The writer builds a reference dependency graph and uses Bellman-Ford relaxation to assign upload tiers. Resources in tier 0 have no dependencies and upload first. Resources within the same tier batch into a single Bundle PUT.

---

## Analytics Microservice (`services/analytics/`)

A separate Docker service (~200 MB image). Proxied by the anonymizer at `/analyse/risk` and `/generate/synthetic` when `ANALYTICS_SERVICE_URL` is set.

| Endpoint | Method | Description |
|---|---|---|
| `/v1/analyse/risk` | POST | k-anonymity + l-diversity + re-identification risk scoring on de-identified NDJSON |
| `/v1/generate/synthetic` | POST | Synthetic FHIR patient generation (stdlib or SDV) |
| `/metrics` | GET | Prometheus metrics |

**Why a separate service?** SDV (Synthetic Data Vault), an optional dependency for advanced synthetic data, adds ~2 GB to the Docker image. Isolating it prevents this from bloating the anonymizer image and allows independent scaling.

| Module | Role |
|---|---|
| `src/risk.py` | k-anonymity, l-diversity, prosecutor/journalist/marketer attacker models |
| `src/synthetic.py` | Stdlib synthetic patient generation (no SDV dependency) |
| `src/synthetic_sdv.py` | SDV-powered synthesis (conditional, relational, time-series) |

---

## NLP Microservice (`services/nlp/`)

Always-on (~800 MB Docker image, Presidio + spaCy `en_core_web_lg`).

| Endpoint | Method | Description |
|---|---|---|
| `/v1/detect` | POST | Detect PII entities in a single text |
| `/v1/detect/batch` | POST | Batch detect across multiple texts (one HTTP call) |
| `/metrics` | GET | Prometheus metrics |

**Why isolated from the anonymizer?** The spaCy model alone is ~800 MB. Running it in-process would double memory per anonymizer replica. The NLP microservice keeps this cost fixed regardless of anonymizer scaling, and can be independently replicated for CPU-intensive workloads.

| Module | Role |
|---|---|
| `src/detector.py` | Presidio NER facade. Deterministic `[[TYPE_N]]` token substitution. XHTML-safe output. |
| `src/recognizers.py` | Custom Presidio recognizers for healthcare-specific patterns (MRN, NPI, DEA number). |
| `src/tokenizer.py` | Token state tracking + XHTML text node scrubbing. |
| `src/cache.py` | Redis L2 detection cache (DB 2). Soft-fails on Redis errors. |

---

## FHIR Subscriptions (`src/integrations/subscriptions/`)

FHIR R4 rest-hook subscriptions. When a configured criteria is matched (e.g. a new Patient resource created on the source FHIR server), the anonymizer receives a notification and triggers de-identification automatically.

| Module | Role |
|---|---|
| `store.py` | `SqliteSubscriptionStore`, FHIR R4 Subscription persistence |
| `integrations/postgres/subscription_store.py` | `PostgresSubscriptionStore`, drop-in replacement for production |

Subscriptions are managed via `/fhir/Subscription` endpoints. The anonymizer acts as both the subscription client (registering with the source FHIR server) and the webhook receiver (processing incoming notifications).

---

## SMART on FHIR (`src/api/routers/smart.py`)

Provides SMART on FHIR capability discovery and token introspection for integrations that use OAuth2/SMART for access control.

| Endpoint | Description |
|---|---|
| `GET /.well-known/smart-configuration` | RFC 8414 capability discovery |
| `POST /oauth2/introspect` | RFC 7662 token introspection |

---

## Staging Layer (`src/integrations/staging/`)

The two-phase staging store decouples FHIR fetch (slow, I/O-bound) from de-identification (CPU + gPAS-bound) in bulk operations.

| Module | Role |
|---|---|
| `store.py` | `StagingStore`, PostgreSQL table `medanon.staged_resources`. `ON CONFLICT DO NOTHING` for dedup. `SELECT ... FOR UPDATE SKIP LOCKED` for contention-free parallel workers. |

**Phase 1:** Worker fetches all resource types from source FHIR and stages them in PostgreSQL.  
**Phase 2:** Worker claims partitions, de-identifies, and writes NDJSON to output storage.

This decoupling means a FHIR server timeout in Phase 1 does not require re-uploading already-processed resources in Phase 2.

---

## PostgreSQL Stores (`src/integrations/postgres/`)

| Module | Role |
|---|---|
| `pool.py` | Shared `psycopg2.ThreadedConnectionPool` singleton (5-25 connections). One pool shared across all stores. |
| `job_store.py` | `PostgresJobStore`, `FOR UPDATE SKIP LOCKED` for contention-free job claims. `NOTIFY`/`LISTEN` for instant wake-up. |
| `config_store.py` | `PostgresConfigStore`, config profile CRUD. |
| `subscription_store.py` | `PostgresSubscriptionStore`, FHIR R4 Subscription persistence. |
| `processing_run_store.py` | `ProcessingRunStore`, scoring history with per-run metrics. |
| `api_key_store.py` | `PostgresApiKeyStore`, per-client API keys (bcrypt-hashed). |

---

## Domain Types (`src/domain/`)

Core domain types inlined into the anonymizer service:

| Module | Contents |
|---|---|
| `domain/jobs.py` | `Job`, `JobStatus` dataclass and lifecycle enum (`pending → running → done / failed / cancelled`) |
| Exceptions | `JobStoreUnavailable`, `JobNotFound`, `JobNotComplete`, `JobResultMissing` |
