---
title: "De-identification Engine"
sidebar_position: 4
description: "FastAPI REST API, 4-stage pipeline, action registry, async job system, scoring, AI agents, NLP integration, and utilities."
---

# De-identification Engine

The anonymizer is the core service. It exposes a FastAPI REST API, runs the 4-stage de-identification pipeline, manages the async job system, scores outputs, and hosts the AI agents.

---

## REST API Endpoints

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/health` |, | Liveness, fast, no external calls |
| GET | `/ready` |, | Readiness, probes FHIR + gPAS + NLP |
| GET | `/metrics` |, | Prometheus metrics |
| POST | `/process` | analyst | De-identify a single FHIR resource or Bundle |
| POST | `/process/batch` | analyst | De-identify a JSON array or NDJSON |
| POST | `/process/ndjson` | analyst | NDJSON streaming endpoint |
| POST | `/process/raw` | analyst | Raw text scrubbing without FHIR parsing |
| POST | `/process/from-server` | analyst | Fetch from FHIR server + de-identify + stream |
| POST | `/process/everything` | analyst | Patient `$everything` de-identify |
| POST | `/process/and-upload` | analyst | Fetch + de-identify + upload to target |
| POST | `/process/round-trip` | analyst | Full round-trip: source → de-id → target |
| POST | `/v1/jobs/bulk-export` | analyst | Submit async bulk export (202 + `job_id`) |
| POST | `/v1/jobs/cohort` | analyst | Submit async cohort export |
| GET | `/v1/jobs` | analyst | List jobs (`?status=`, `?type=`, `?limit=`, `?offset=`) |
| GET | `/v1/jobs/{id}` | analyst | Poll job status |
| GET | `/v1/jobs/{id}/result` | analyst | Download completed job NDJSON |
| DELETE | `/v1/jobs/{id}` | admin | Cancel / delete job |
| POST | `/v1/score` | analyst | Score a de-identified resource ad-hoc |
| POST | `/v1/jobs/{id}/score` | analyst | Trigger scoring for a completed job |
| GET | `/v1/jobs/{id}/score` | analyst | Retrieve cached composite score |
| GET | `/v1/jobs/{id}/score/report` | analyst | Download Markdown audit report |
| GET | `/v1/configs` | viewer | List config profiles |
| POST | `/v1/configs` | admin | Create a user-defined config profile |
| PUT | `/v1/configs/{name}` | admin | Replace rules in a user-defined profile |
| DELETE | `/v1/configs/{name}` | admin | Delete a user-defined profile |
| POST | `/v1/process/tabular` | analyst | De-identify CSV / Parquet / JSON table |
| POST | `/v1/process/dicom` | analyst | De-identify a DICOM file |
| POST | `/v1/process/hl7v2` | analyst | De-identify an HL7 v2 message |
| POST | `/v1/cda/*` | analyst | De-identify CDA/CCDA documents |
| GET | `/v1/processing-runs` | analyst | List processing runs with scoring stats |
| GET | `/v1/processing-runs/{id}` | analyst | Get a run with full score breakdown |
| DELETE | `/v1/processing-runs` | admin | Purge processing run history |
| GET | `/v1/ai/status` | analyst | AI provider health + circuit breaker state |
| POST | `/v1/ai/generate-config` | analyst | Generate config from natural-language description |
| POST | `/v1/ai/chat` | analyst | Conversational config-builder assistant |
| POST | `/v1/ai/detect-pii` | analyst | Scan de-identified resources for PII leaks |
| POST | `/v1/ai/explain` | analyst | Explain config rules in plain language (SSE) |
| POST | `/v1/ai/compliance` | analyst | Regulatory gap analysis |
| POST | `/v1/ai/scan-fields` | analyst | Suggest PII-bearing FHIR paths from a sample |
| GET | `/v1/audit/events` | admin | Query recent audit events |

RBAC: `admin` (all) → `analyst` (processing + jobs + analytics + AI) → `viewer` (read-only).

---

## API Layer (`src/api/`)

| Module | Role |
|---|---|
| `main.py` | FastAPI entry point. Startup: Redis cache + job store + NLP adapter + Prometheus middleware + CORS. |
| `auth.py` | API-key auth + RBAC. `get_required_role()` resolves role for parameterized paths via `ENDPOINT_ROLE_PREFIXES`. |
| `auth_providers.py` | `AuthProvider` Protocol + `OpenProvider` / `ApiKeyProvider` / `OidcProvider`. `get_provider()` selects by `MEDANON_AUTH_PROVIDER`. |
| `deps.py` | Per-request middleware: rate limiter (slowapi), body-size cap, SSRF guard (blocks RFC-1918 ranges in `server_url`). |
| `routers/` | One file per endpoint group, `process`, `fhir_server`, `fhir_bulk`, `jobs`, `scoring`, `configs`, `analytics`, `synthetic`, `dicom`, `hl7v2`, `cda`, `tabular`, `agents`, `processing_runs`, `workflows`, `sql_source`, `audit`, `admin`, `api_keys`, `auth`, `smart`, `fhir_subscriptions`, `dashboard` |
| `schemas/` | Pydantic request/response models for each endpoint group |
| `services/` | Business logic layer, one service per domain, injected by routers |

---

## 4-Stage Pipeline (`src/pipeline/`)

The pipeline runs four named stages per batch (up to `MEDANON_BATCH_SIZE` resources, default 1000). Stages 2 and 3 run **concurrently**, NLP scrubs free text while gPAS fetches pseudonyms.

```
Input
  │
  ▼  1. match
rule_matcher.py      FHIRPath eval → per-resource rule index
action_dispatcher.py dispatch → immediate actions + deferred BatchWork + NlpWork
  │
  ├──────────────────────────────────────────────┐
  ▼  2. phi_detection                            ▼  3. pseudonymize
nlp_orchestrator.py                        gpas_orchestrator.py
  extract → dedup → batch detect              batch gPAS lookup
  → per-resource replacement                  → write pseudonyms
  └──────────────────────────────────────────────┘
  │
  ▼  4. finalize
post_processor.py    reference rewriting, text-ID replacement
manifest.py          optional meta.tag transformation summary
  │
  ▼
Output
```

| Module | Stage | Role |
|---|---|---|
| `processor.py` | Orchestrator | Entry points: `process_data`, `process_data_batch`, `process_data_stream` |
| `rule_matcher.py` | Pre | Builds per-resource rule index from FHIRPath. Cached per `(resource_type, rules_hash)`. |
| `action_dispatcher.py` | **match** | Executes stateless actions immediately. Defers NLP → `NlpWork`, gPAS → `BatchWork`. |
| `nlp_orchestrator.py` | **phi_detection** | Deduplicates, sends one batch HTTP call to NLP service, applies replacements with isolated token state. |
| `gpas_orchestrator.py` | **pseudonymize** | Deduplicates all gPAS values across resources, sends one batch HTTP call, writes pseudonyms back. |
| `post_processor.py` | **finalize** | Rewrites FHIR bundle references after ID changes. Replaces pseudonym-changed IDs in free-text. |
| `manifest.py` | Post | Attaches per-rule transformation summary to `meta.tag` when `MEDANON_MANIFEST_ENABLED=true`. |
| `validation.py` | Gate | `validate_output()` / `enforce_output()`, unified output barrier. Merges raw PII scan + score gate. |
| `gate.py` | Gate | `run_pii_gate`, `PiiLeakError`, `quarantine_info_for`, raw-PII scan primitives. |
| `config.py` | Config | Parses YAML profile. `${VAR:-default}` env interpolation. Raises `ValueError` on invalid config. |
| `config/service.py` | Config | `get_settings(profile)`, single entry point, `@lru_cache(maxsize=8)`. Warms FHIRPath cache on load. |
| `config/store.py` | Config | Config profile metadata index backed by PostgreSQL. |
| `config/rule_schema.py` | Config | Pydantic v2 rule schema validation. Returns errors, does not raise. Severity via `MEDANON_RULE_SCHEMA_STRICT`. |
| `deidentify.py` | Actions | Unified action registry. `nlp_detect_by_path` routes through `_get_nlp_adapter()` lazy singleton. |
| `io_formats.py` | I/O | Parse/serialize JSON, NDJSON, XML. `iter_ndjson(path)` streaming generator. |

---

## Action Registry (`src/actions/`)

Actions are pure functions: input value → transformed value, no side effects.

| File | Action | Reversible | Notes |
|---|---|---|---|
| `redact.py` | `redact` | No | Replaces with `replacement` param (default `""`) |
| `cryptohash.py` | `cryptohash` | No | HMAC-SHA3-256 (keyed) or plain SHA3-256. Warns if no key set. |
| `encrypt.py` | `encrypt` | Yes (RSA key) | RSA public-key encryption |
| `decrypt.py` | `decrypt` |, | RSA private-key decryption |
| `perturb.py` | `perturb` | No | Bounded random noise via `secrets.randbelow()` (CSPRNG) |
| `substitute.py` | `substitute` | No | Replace with fixed `substitute_with` value |
| `generalize.py` | `generalize` | No | Strategies: `date_year`, `date_year_month`, `zip_prefix`, `age_bracket`, `number_round`, `category` |
| `date_shift.py` | `date_shift` | No | Deterministic per-subject date offset (consistent within a patient) |
| `tokenize.py` | `tokenize` | Yes (key) | Format-preserving tokenization |
| `mask.py` | `mask` | No | 5 masking strategies: `partial`, `full`, `first_n`, `last_n`, `character` |
| `scrub_text.py` | `scrub_text` | No | 16 regex patterns for structured PHI (phone, SSN, email, NPI, MRN…) |
| `deidentify.py` | `nlp_detect_act` | Configurable | Entity-specific conditional NLP with per-entity-type action routing |

---

## Async Job System (`src/pipeline/jobs/`)

Long-running operations (bulk export, cohort) are handled asynchronously.

| Module | Role |
|---|---|
| `store.py` | `SqliteJobStore` (WAL mode, 2s polling). Fallback when Redis and PostgreSQL are unavailable. |
| `worker.py` | Async executor. `RedisJobStore`: XREADGROUP blocking read. `PostgresJobStore`: LISTEN/NOTIFY. `max_concurrent` semaphore. |
| `worker_main.py` | Standalone worker entrypoint. Prometheus metrics on port 9091. |
| `executors.py` | Bulk-export and cohort executors. Cross-chunk gPAS dedup via `seen_values`. |
| `checkpoint.py` | Saves processing position for crash recovery, failed jobs resume from last successful page. |
| `staged_worker/` | Two-phase staged bulk-export. Phase 1: fetch + stage to PostgreSQL. Phase 2: claim partitions (`FOR UPDATE SKIP LOCKED`), de-identify, write NDJSON. |

**Backend selection at startup (priority order):**

```
MEDANON_REDIS_URL set    → RedisJobStore    (Streams + consumer groups, cross-replica, at-least-once)
MEDANON_APP_DB_URL set   → PostgresJobStore (LISTEN/NOTIFY, multi-worker)
Neither                  → SqliteJobStore   (polling, local dev only)
```

The dedicated `worker` container always refuses SQLite, it shares `/output` with the API container and SQLite WAL is unsafe across container boundaries.

---

## Scoring System (`src/pipeline/scoring/`)

| Module | Role |
|---|---|
| `engine.py` | Composite scorer: `privacy_norm × utility × quality`. Hard privacy gate. |
| `privacy.py` | k-anonymity, l-diversity, HIPAA 18-identifier check, text-risk scan. Blocks output if risk exceeds threshold. |
| `utility.py` | Field retention rate, date precision, clinical code coverage, structural completeness. |
| `quality.py` | FHIR structural validity, required fields, reference integrity, valid code values. |
| `audit.py` | Markdown audit report builder. Per-resource findings formatted for human review. |
| `models.py` | `ScoringResult`, `PrivacyScore`, `UtilityScore`, `QualityScore`, `AuditFinding`. |
| `constants.py` | Scoring weights, thresholds, action classifications (utility-preserving vs. destructive). |

See [Scoring System](../../explanation/scoring-system.md) for the full model documentation.

---

## AI Agents (`src/integrations/ai/`)

Activated with `MEDANON_AI_ENABLED=true`. All agents degrade gracefully to static fallbacks when AI is unavailable.

| Module | Role |
|---|---|
| `provider.py` | `LLMProvider` singleton, litellm wrapper with circuit breaker, TTL response cache, SSE streaming. |
| `agents/config_generator.py` | Few-shot config generation. Validates output through the Settings loader. Keyword fallback when AI unavailable. |
| `agents/config_chat.py` | Conversational config-builder assistant (`/v1/ai/chat`). |
| `agents/pii_detector.py` | 3-layer PII detection: regex → NER → LLM. LLM layer **must use a local provider**, PHI must not reach external APIs. |
| `agents/rule_explainer.py` | Plain-language rule explanation via SSE streaming. Static descriptions as fallback. |
| `agents/compliance.py` | Regulatory gap analysis vs HIPAA, GDPR, and other frameworks. Static HIPAA fallback. |
| `agents/field_scanner.py` | Suggests PII-bearing FHIR paths + recommended actions from a sample resource. |

---

## NLP Integration (`src/integrations/nlp/`)

| Module | Role |
|---|---|
| `adapter.py` | `RemoteNlpAdapter`, delegates all NLP inference to the NLP microservice. Supports `detect_batch()`. |
| `remote_detector.py` | HTTP client for the NLP microservice. **Fail-closed:** returns `[NLP_UNAVAILABLE]` on any failure, no PHI leaks via unscrubbed text. |
| `utils.py` | Pure-Python NLP utilities: `HEALTHCARE_ENTITIES`, `_tokenize`, `_scrub_xhtml_text_nodes`. No Presidio dependency. |

---

## Utilities (`src/utils/`)

| Module | Role |
|---|---|
| `cache.py` | `CacheBackend` Protocol. `LocalLruCache` (50K entries, 10% eviction). `RedisCache` (L2, errors swallowed). |
| `circuit_breaker.py` | Reusable three-state circuit breaker. Thread-safe. Used by gPAS, NLP, analytics, and AI integrations. |
| `bulkhead.py` | Per-upstream named semaphores. Fast-fail with `UpstreamSaturated`. |
| `fhirpath.py` | FHIRPath traversal: `find_nodes`, `error`, `not_implemented`. LRU-cached per expression. |
| `crypto.py` | RSA encrypt/decrypt. `bounded_random` uses `secrets.randbelow()` (CSPRNG). Path-traversal guard on key file paths. |
| `metrics.py` | Prometheus counters + histograms: requests, gPAS calls/latency/cache, FHIR calls/latency. |
| `audit.py` | Centralized audit logging, structured JSON to file + optional Redis Stream. PHI is never logged. |
| `tasks.py` | `retain_task()`, keeps fire-and-forget `asyncio.Task` references alive so the GC cannot cancel them mid-flight. |
