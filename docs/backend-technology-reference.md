# MedAnon — Backend Technology Reference

## Runtime Stack

| Layer | Technology | Version | Notes |
|---|---|---|---|
| Language | Python | 3.12 (Docker), 3.13 (local dev) | `typing.io` removed in 3.13 — conftest shim patches it for FHIRPath tests |
| Web Framework | FastAPI + Uvicorn | 0.115+ | ASGI; startup/shutdown lifecycle events |
| Serialization | orjson | 3.10+ | 3-10x faster than stdlib `json` for hot paths |
| FHIR Evaluation | fhirpathpy | 2.x | FHIRPath expression engine; lazy-imported with LRU cache |
| Task Queue | Redis Streams / SQLite | Redis 7+ | Event-driven (BLPOP/XREAD) with Redis; polling fallback with SQLite |
| Cache | In-process LRU + Redis L2 | 50K entries / error-swallowing | `utils/cache.py` — `CacheBackend` Protocol |
| Pseudonymization | gPAS (TTP WildFly) | 2024.1+ | HTTP SOAP/REST; circuit breaker + retry + cache |
| NLP | Presidio + spaCy | `en_core_web_lg` | Optional — NLP microservice or in-process; fail-open on error |
| Metrics | prometheus_client | 0.20+ | Counters, Histograms, Gauges per subsystem |
| HTTP | urllib3 | 2.x | Connection pooling; shared transport for FHIR/gPAS/analytics |
| XML | defusedxml | — | XXE-safe XML parsing for FHIR XML and DICOM |
| Config | PyYAML | — | `${VAR:-default}` env interpolation; `@lru_cache` service |

## Docker Services (14 containers, 4 profiles)

| Service | Image | Profile | Port | Purpose |
|---|---|---|---|---|
| anonymizer | `python:3.12-slim` | always | 8000 | FastAPI de-identification engine |
| worker | same image | always | — | Dedicated async job worker (no HTTP) |
| fhir-server | HAPI FHIR JPA | always | 8081 | Source FHIR R4 server |
| fhir-target | HAPI FHIR JPA | always | 8082 | Target FHIR R4 server (de-identified) |
| gpas | WildFly | always | 8080 | gPAS TTP pseudonymization |
| redis | Redis 7 | always | — | Cache + job queue |
| ui | nginx + React SPA | always | 8501 | Patient browser, batch export, risk UI |
| analytics | `python:3.12-slim` | `analytics` | 8100 | Risk analysis + synthetic data |
| nlp | `python:3.12-slim` | `nlp` | 8200 | Presidio + spaCy NER (800 MB) |
| hapi-db | PostgreSQL 16 | always | — | HAPI source backend |
| hapi-target-db | PostgreSQL 16 | always | — | HAPI target backend |
| gpas-db | MySQL 8 | always | — | gPAS backend |
| gpas-db-replica | MySQL 8 | `ha` | — | Read replica for gPAS |
| minio | MinIO | `s3` | 9000 | S3-compatible object storage |

## Processing Pipeline

```
Input → io_formats.py → config_service.py → processor.py
                                               ├── rule_matcher.py        (FHIRPath eval + per-resource rule index)
                                               ├── action_dispatcher.py   (Pass 1: per-rule actions + BatchWork)
                                               ├── gpas_orchestrator.py   (Pass 2: batch gPAS pseudonymisation)
                                               ├── post_processor.py      (reference rewriting + text-ID replacement)
                                               ├── manifest.py            (transformation manifest → meta.tag)
                                               └── scoring/engine.py      (scoring gate: privacy → utility → quality)
                                             → io_formats.py → Output
```

## Scoring Engine (Phase 3)

Constraint-based, non-compensatory scoring engine. Privacy is a hard gate; utility and quality are continuous multipliers.

| Module | Type | Weight | Gate |
|---|---|---|---|
| Privacy Risk | Hard constraint | — | `risk > 0.3` → FAIL (composite = 0) |
| Utility | Continuous [0,1] | multiplicative | — |
| Quality | Continuous [0,1] | multiplicative | `error_rate > 20%` → cap 0.20; `> 5%` → cap 0.60 |

**Composite** = `(1 - risk/threshold) × utility × quality × 100`

Privacy sub-evaluators:
- Attacker model (prosecutor/journalist/marketer via k-anonymity)
- Direct identifier detection (manifest coverage vs HIPAA-sensitive paths)
- Text risk (regex + optional NER residual PII scan)

**Config:**
- `MEDANON_SCORING_ENABLED=false` (opt-in, zero overhead when off)
- `MEDANON_SCORE_ATTACH=false` (FHIR extension attachment)
- `MEDANON_SCORE_RISK_THRESHOLD=0.3`

**API endpoints:**
- `POST /v1/score` — score a single de-identified resource
- `GET /v1/jobs/{id}/score` — retrieve batch-level aggregate score

## Async Job System

| Backend | Trigger | Used When |
|---|---|---|
| Redis Streams | XREAD (event-driven) | `MEDANON_REDIS_URL` set |
| SQLite WAL | 2s polling | Redis unavailable |

Job types: `bulk-export`, `cohort`, `patient-export`, `batch-patient-export`, `bulk-import`, `reprocess`

Features: max-concurrent semaphore, checkpoint/resume, crash recovery, poison-job protection (max 3 retries), result TTL auto-cleanup, SIGTERM graceful drain.

## Config Profiles

| Profile | Use Case | Key Actions |
|---|---|---|
| `config.yaml` | Minimal (default, no gPAS) | HMAC hash + regex scrub |
| `config_gpas.yaml` | Production (default when `GPAS_URL` set) | gPAS pseudonymisation + dual-pass NLP |
| `config_gdpr_eu.yaml` | GDPR Art. 4(5) | HMAC pseudonymisation |
| `config_hipaa_safe_harbor.yaml` | HIPAA Safe Harbor | 18 PHI categories, year-only dates, 3-digit zip |
| `config_research_pseudonymous.yaml` | IRB-grade research | Year-month dates, cryptohash IDs |
| `config_structure_preserving.yaml` | Full FHIR structure | gPAS IDs, `[REDACTED]` PII, year-only dates |

## Authentication

| Mode | Config | Behavior |
|---|---|---|
| Open | `MEDANON_API_KEY` unset | All endpoints open |
| Key | `MEDANON_API_KEY` set | `X-API-Key` required; RBAC: admin/analyst/viewer |

See `docs/auth-security-roadmap.md` for Phase 4 auth hardening plan.

## Key Integration Patterns

- **Circuit breaker**: gPAS client (`failure_threshold=5, recovery_timeout=30s, window=60s`)
- **Retry + backoff**: gPAS (`count=2, backoff=0.2s`), FHIR (`count=2, backoff=0.3s`)
- **Connection pooling**: urllib3 pools shared across FHIR reader/writer/transport
- **Cache hierarchy**: L1 in-process LRU (50K entries) → L2 Redis (error-swallowing)
- **Strangler fig**: NLP and Analytics can live in-process or as separate microservices
