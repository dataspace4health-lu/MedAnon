# MedAnon — Architecture

## What it does

MedAnon is a FHIR R4 de-identification engine. It accepts FHIR resources (JSON / NDJSON / XML), applies configurable match-action rules from a YAML profile, and returns transformed data with PII removed or pseudonymized. It is designed to be the privacy layer between an identified FHIR source and any downstream consumer — research databases, dataspace connectors, analytics pipelines.

---

## System topology

```
┌─────────────────────────────────────────────────────────────┐
│  Browser                                                     │
└──────────────────────┬──────────────────────────────────────┘
                       │ http://host:8501
                       ▼
┌─────────────────────────────────────────────────────────────┐
│  nginx (medanon-ui)                                          │
│   /api/*   → strips prefix → anonymizer:8000                 │
│   /fhir/*  → passthrough  → hapi-fhir:8080   (300 s timeout)│
│   /        → React SPA                                       │
└──────────────┬─────────────────────┬────────────────────────┘
               │                     │
               ▼                     ▼
┌──────────────────────┐   ┌──────────────────────────────────┐
│  anonymizer :8000    │   │  hapi-fhir (source) :8080        │
│  (FastAPI)           │◄──┤  PostgreSQL-backed FHIR R4       │
│                      │   │  Stores IDENTIFIED patient data  │
│  reads  ──────────►  │   └──────────────────────────────────┘
│  de-identifies       │
│  writes ──────────►  │   ┌──────────────────────────────────┐
│                      │──►│  hapi-fhir-target :8082          │
└──────┬───────────────┘   │  PostgreSQL-backed FHIR R4       │
       │                   │  Stores DE-IDENTIFIED data only  │
       │                   └──────────────────────────────────┘
       │
       ├──► gpas:8080 (WildFly + gPAS)
       │     └──► gpas-mysql:3306
       │
       └──► redis:6379
             ├── job queue  (BLPOP event-driven)
             └── gPAS cache (L2, cross-replica)
```

**Two separate FHIR servers** — identified and de-identified data never share a database. This is a deliberate design: it prevents accidental joins, satisfies physical separation requirements under GDPR Art. 25 (data minimization by design), and allows different access controls per server.

---

## Services

| Container | Image | Host Port | Role |
|---|---|---|---|
| `anonymizer` | `medanon:latest` | 8000 | FastAPI de-identification engine |
| `ui` | `medanon-ui:latest` | 8501 | React SPA served by nginx |
| `fhir-server` | `hapiproject/hapi:v7.6.0` | 8081 | Source FHIR R4 (identified data) |
| `fhir-server-target` | `hapiproject/hapi:v7.6.0` | 8082 | Target FHIR R4 (de-identified data) |
| `gpas` | WildFly + gPAS | 8080 | Reversible pseudonymization (TTP) |
| `gpas-db` | `mysql:8.0` | internal | gPAS pseudonym store |
| `redis` | `redis:7` | internal | Shared job queue + gPAS L2 cache |

Opt-in profiles (not started by default):

| Container | Profile | Host Port | Role |
|---|---|---|---|
| `analytics` | `analytics` | 8100 | Risk analysis + synthetic data |
| `nlp` | `nlp` | 8200 | Presidio NLP microservice (~800 MB) |
| `gpas-db-replica` | `ha` | internal | MySQL read replica for gPAS HA |

---

## De-identification pipeline

Every resource passes through a fixed sequence of stages:

```
Input (JSON / NDJSON / XML)
    │
    ▼
io_formats.py          Parse. XML uses defusedxml to prevent entity expansion attacks.
    │
    ▼
config_service.py      Load YAML profile. @lru_cache(maxsize=8) — loaded once per profile
                       per process. Env vars interpolated with ${VAR:-default} syntax.
    │
    ▼
rule_matcher.py        Build per-resource-type rule index using FHIRPath expressions.
                       Results cached per (resource_type, rules_hash) to avoid repeat
                       FHIRPath evaluation on identical inputs.
    │
    ▼
action_dispatcher.py   Pass 1: Iterate matched rules. Apply stateless actions immediately
                       (redact, cryptohash, generalize, substitute, perturb, scrub_text,
                       nlp_detect, encrypt). Collect gPAS-bound values into BatchWork
                       WITHOUT calling gPAS yet — batching is critical for throughput.
    │
    ▼
gpas_orchestrator.py   Pass 2: Send one HTTP request to gPAS for all collected values.
                       Checks cache first (local LRU → Redis L2 → live call).
                       Results written back into the resource tree.
    │
    ▼
post_processor.py      Rewrite FHIR references (Patient/123 → Patient/p-123) and
                       replace pseudonym-changed IDs that appear in text fields.
    │
    ▼
manifest.py            Tag meta.tag with a per-rule transformation summary if
                       MEDANON_MANIFEST_ENABLED=true. Required for the UI Resource
                       Summary view. Stripped before FHIR server upload (the display
                       field would exceed HAPI's varchar(200) column limit).
    │
    ▼
io_formats.py          Serialize. Output format matches input or as requested.
```

**Why two passes for gPAS?** gPAS has a non-trivial per-call overhead (TTP-FHIR HTTP round-trip). Processing 300 resources each calling gPAS individually would be ~300 HTTP calls. Batching all values from a resource page into one call reduces this to 1. The action dispatcher collects values in Pass 1; Pass 2 sends the single batch.

---

## Async job queue

Long-running operations (`bulk-export`, `cohort`) run as background jobs:

```
POST /v1/jobs/bulk-export  ──► 202 Accepted  {"job_id": "abc123"}
                                      │
                               worker loop (background)
                                      │
                               ┌──────▼──────────────────────────┐
                               │ 1. Fetch resource types         │
                               │    (paginated REST, page=500)   │
                               │ 2. De-identify each page        │
                               │ 3. Topological sort by deps     │
                               │ 4. Rewrite references           │
                               │ 5. Upload in tier order         │
                               │    (200 resources/batch bundle) │
                               └─────────────────────────────────┘
                                      │
GET /v1/jobs/abc123         ◄── poll status (pending/running/done/failed)
GET /v1/jobs/abc123/result  ◄── download NDJSON when done
```

**Backend selection at startup:**
- `MEDANON_REDIS_URL` set → `RedisJobStore`: BLPOP event-driven (no polling), cross-replica safe
- `MEDANON_REDIS_URL` unset → `SqliteJobStore`: polls every 2 s, single-instance only

**Why not always Redis?** Local dev has no Redis. SQLite fallback works fine for single-instance dev and CI. Redis is required for multi-replica production deployments where multiple anonymizer instances share a job queue.

---

## Upload ordering (topological sort)

**Problem:** FHIR referential integrity. HAPI rejects a resource if it references another resource that doesn't exist yet. For example, uploading an `Encounter` before its `Organization` and `Practitioner` causes HAPI-1094.

**Why a naive 2-tier approach fails:** A simple "base types first, rest second" doesn't work when there are 3+ levels of dependency:
- `Organization` (tier 0 — no deps)
- `Practitioner` (tier 0 — no deps)
- `Encounter` (tier 1 — references Organization + Practitioner)
- `Observation` (tier 2 — references Encounter)
- `DiagnosticReport` (tier 3 — references Observation)

**Solution:** Bellman-Ford relaxation over the reference graph. `tier[A] = 1 + max(tier[B] for B in deps[A])`. Converges in at most N passes where N = number of distinct resource types. Resources within the same tier upload in a single batch Bundle.

---

## Reference rewriting after ID sanitization

**Problem:** HAPI FHIR rejects purely numeric resource IDs (HAPI-0960). The de-identification pipeline may produce numeric IDs (e.g. gPAS pseudonyms are sometimes numeric, or the source FHIR server uses numeric auto-increment IDs). These are prefixed with `p-` before upload (e.g. `Patient/123` → `Patient/p-123`).

**Consequence:** Any resource that contains `{"reference": "Patient/123"}` will now point to a non-existent resource — causing HAPI-1094 on upload.

**Fix:** Before uploading, `_compute_id_map()` collects all `(resourceType, originalId) → sanitisedId` mappings, and `_rewrite_references()` walks every resource recursively to update matching `reference` fields. This must happen after topological sort (sort is read-only) and before chunking into batch Bundles.

---

## FHIR R4 code binding constraint

`Patient.gender` and `Practitioner.gender` are bound to the `AdministrativeGender` value set: `male | female | other | unknown`. Substituting `[REDACTED]` causes HAPI to reject the resource with HAPI-1821 ("not a valid code").

**Fix in `config_structure_preserving.yaml`:** The gender rules use `substitute_with: "unknown"` instead of the `*redacted` YAML anchor (which resolves to `"[REDACTED]"`). This keeps the field valid while removing its identifying content.

This is a fundamental FHIR constraint: de-identification must produce *valid* FHIR resources, not just *transformed* ones.

---

## gPAS pseudonymization

gPAS (Generic Pseudonym Administration Service) is a TTP (Trusted Third Party) service developed by the University Medicine Greifswald. It maintains a persistent, audited mapping between original values and pseudonyms, making pseudonymization **reversible** under controlled conditions (e.g. adverse event investigation).

```
MedAnon                          gPAS
    │                               │
    │── POST $pseudonymizeAllowCreate ──►│
    │   Parameters: [id1, id2, ...]  │
    │                               │
    │◄── Parameters: [psn1, psn2, ...]──│
    │                               │
    cache result locally (LRU)      │
    optionally cache in Redis (L2)  │
```

**Why batch?** One HTTP call per resource is prohibitive. A bulk export of 20,000 resources with gPAS IDs would require 20,000 HTTP calls. Batching collects all IDs from a page (~500 resources) and sends one request.

**Circuit breaker:** If gPAS fails 5 times in 60 s, the circuit opens. Subsequent requests fail fast (no timeout wait) until a recovery probe succeeds after 30 s. This prevents a gPAS outage from cascading into anonymizer thread exhaustion.

**Domain management:** gPAS maintains an in-memory `domainLocks HashMap` populated only when domains are created through its own REST API. Direct SQL inserts work at the database level but bypass this cache, causing "domain not found" errors at runtime. **Always create domains via the gPAS web UI or `make init-domains`.**

---

## Config profiles

Six bundled profiles. Auto-selected based on environment; overridable per-request via `?config_profile=<name>`.

| Profile | ID handling | Dates | Geographic | Requires gPAS | Use case |
|---|---|---|---|---|---|
| `config.yaml` | SHA3-256 hash | Year only | Zip prefix (3-digit) | No | Local dev / testing |
| `config_gpas.yaml` | gPAS pseudonym (reversible) | Year only | Zip prefix | Yes | Production with re-linkage |
| `config_gdpr_eu.yaml` | HMAC SHA3-256 | Redacted | Redacted | No | GDPR Art. 4(5) |
| `config_hipaa_safe_harbor.yaml` | Redacted | Year only | State + 3-digit zip | No | US HIPAA Safe Harbor |
| `config_research_pseudonymous.yaml` | SHA3-256 hash | Year-month | 3-digit zip | No | IRB research |
| `config_structure_preserving.yaml` | gPAS pseudonym (reversible) | Year (birthDate only) | Preserved | Yes | Full FHIR structure downstream |

Auto-selection logic: `GPAS_URL` set → `config_gpas.yaml`; otherwise → `config.yaml`.

---

## Authentication

| `MEDANON_API_KEY` | Behaviour |
|---|---|
| Unset | All endpoints open (dev only) |
| Set | All endpoints except `/health`, `/ready`, `/metrics`, `/docs` require `X-API-Key: <key>` |

RBAC roles: `admin` (all), `analyst` (processing + jobs), `viewer` (read-only).

**Health check strategy:** Docker's `healthcheck` targets `/health` (lightweight — returns `{"status":"ok"}` immediately). The `depends_on: condition: service_healthy` chain requires this. `/ready` is more expensive — it probes FHIR and gPAS connectivity with a 5 s timeout each — and is used for readiness gates only, not for Docker's healthcheck polling.

---

## Performance tuning

| Variable | Default | Effect |
|---|---|---|
| `MEDANON_JOB_WORKERS` | 3 | Concurrent background jobs per anonymizer instance |
| `MEDANON_BATCH_SIZE` | 300 | Resources per gPAS batch (1 gPAS HTTP call per batch) |
| `FHIR_PAGE_SIZE` | 500 | Resources per FHIR paginated fetch |
| `MEDANON_FHIR_FETCH_PARALLEL` | 1 | Parallel FHIR resource-type fetch threads |
| `MEDANON_COHORT_PARALLEL` | 2 | Parallel `$everything` threads for cohort export |

**Why `FHIR_FETCH_PARALLEL=1`?** The pipeline bottleneck is gPAS (sequential HTTP calls per batch). Adding parallel FHIR fetch threads makes them compete for the GIL and the internal queue lock while gPAS is processing — this adds overhead without reducing total processing time. Sequential fetching avoids this contention. `COHORT_PARALLEL=2` is safe because cohort is I/O-bound (waiting on FHIR server), not CPU-bound.

**Memory:** Anonymizer container is allocated 6 GB / 2 CPU. A bulk export of ~20,000 resources uses ~5.7 GB peak (gPAS pseudonym cache + in-flight resource batches + NDJSON write buffer).
