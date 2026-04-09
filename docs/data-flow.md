# MedAnon — Data Flow Reference

## Network layout

```
┌──────────────── host network ─────────────────────────────────┐
│                                                               │
│  Browser ──► :8501 (UI)   :8000 (API)   :8081   :8082 :8080  │
│                                         HAPI   HAPI  gPAS    │
└──────────────────────────────────────────────────────────────┘
                 │             │
        ┌────────▼─────────────▼──── fhir-net bridge ──────────┐
        │                                                       │
        │  medanon-ui:8501                                      │
        │    nginx reverse proxy                                │
        │    /api/* → medanon:8000  (strips /api prefix)        │
        │    /fhir/* → hapi-fhir:8080  (300 s timeout)          │
        │    / → React SPA (index.html fallback)                │
        │                                                       │
        │  medanon:8000  (anonymizer / FastAPI)                 │
        │    → hapi-fhir:8080        reads source data          │
        │    → hapi-fhir-target:8080 writes de-identified data  │
        │    → gpas:8080             pseudonymization           │
        │    → redis:6379            job queue + cache          │
        │                                                       │
        │  hapi-fhir:8080   → hapi-postgres:5432               │
        │  hapi-fhir-target:8080 → hapi-target-postgres:5432   │
        │  gpas:8080        → gpas-mysql:3306                   │
        │                                                       │
        └───────────────────────────────────────────────────────┘
```

All services communicate on `fhir-net`. The databases are internal-only — no host ports exposed.

---

## Single-resource de-identification flow

```
POST /process  {"resourceType":"Patient","id":"123","name":[{"family":"Smith"}],...}
    │
    ├─ [auth]        API key check (if MEDANON_API_KEY set)
    ├─ [middleware]  Body size cap (MEDANON_MAX_BODY_BYTES = 10 MB)
    │                Rate limiter (slowapi)
    │                SSRF guard (blocks private IP ranges in server_url params)
    │
    ├─ io_formats.py         detect format, parse JSON/NDJSON/XML
    │
    ├─ config_service.py     load YAML profile
    │                        ?config_profile=hipaa → config_hipaa_safe_harbor.yaml
    │                        (cached per profile name, loaded once per process)
    │
    ├─ rule_matcher.py       evaluate FHIRPath rules against resource type
    │                        Patient.name matches → action=redact
    │                        Patient.birthDate matches → action=generalize
    │                        Patient.id matches → action=gpas_pseudonymize
    │
    ├─ action_dispatcher.py  Pass 1
    │                        redact:    resource["name"] = []
    │                        generalize: resource["birthDate"] = "1985"
    │                        gpas_pseudonymize: collect("123") → BatchWork
    │                        (gPAS NOT called yet)
    │
    ├─ gpas_orchestrator.py  Pass 2 (only if BatchWork is non-empty)
    │                        cache_get("123") → miss
    │                        POST gpas:8080/$pseudonymizeAllowCreate [123]
    │                        ← psn-abc456
    │                        cache_set("123" → "psn-abc456")
    │                        resource["id"] = "psn-abc456"
    │
    ├─ post_processor.py     rewrite references: any {"reference":"Patient/123"}
    │                        in OTHER resources → {"reference":"Patient/psn-abc456"}
    │                        replace IDs that appear in text fields
    │
    ├─ manifest.py           meta.tag = [{system: "medanon", display: "Patient.name:redact ..."}]
    │                        (only if MEDANON_MANIFEST_ENABLED=true)
    │
    └─ io_formats.py         serialize → JSON response
```

---

## Bulk export flow

This is the most complex flow. It runs entirely in the background after returning a 202.

```
POST /v1/jobs/bulk-export
{
  "source_url": "http://hapi-fhir:8080/fhir",
  "config_profile": "structural"
}
──► 202 Accepted  {"job_id": "abc123", "status": "pending"}
      │
      └── RedisJobStore LPUSH job to queue
              │
              └── worker (BLPOP, wakes immediately)
                      │
          ┌───────────▼──────────────────────────────────┐
          │  PHASE 1 — Discovery                          │
          │  GET /fhir/metadata → capability statement    │
          │  extract resource types: Patient, Condition,  │
          │  Observation, Encounter, Organization, ...    │
          └───────────┬──────────────────────────────────┘
                      │ MEDANON_FHIR_FETCH_PARALLEL=1 (sequential)
          ┌───────────▼──────────────────────────────────┐
          │  PHASE 2 — Fetch + De-identify               │
          │                                               │
          │  For each resource type:                      │
          │    GET /fhir/Patient?_count=500 (page 1)      │
          │      → de-identify 500 resources              │
          │      → collect gPAS batch for page           │
          │      → 1 gPAS HTTP call for whole page        │
          │    GET /fhir/Patient?_count=500&_page=2       │
          │      → repeat until no more pages             │
          │    GET /fhir/Observation?_count=500 (page 1)  │
          │      → ...                                    │
          └───────────┬──────────────────────────────────┘
                      │
          ┌───────────▼──────────────────────────────────┐
          │  PHASE 3 — Upload ordering                   │
          │                                               │
          │  Build reference graph from all resources:   │
          │    Encounter.subject → Patient               │
          │    Encounter.participant → Practitioner      │
          │    Observation.encounter → Encounter         │
          │                                               │
          │  Bellman-Ford relaxation:                    │
          │    Organization  tier 0  (no deps)           │
          │    Practitioner  tier 0  (no deps)           │
          │    Patient       tier 0  (no deps)           │
          │    Encounter     tier 1  (refs Patient+Prac) │
          │    Observation   tier 2  (refs Encounter)    │
          │                                               │
          │  Rewrite IDs in references:                  │
          │    Patient/123 → Patient/p-123               │
          │    (numeric IDs → p-prefix to satisfy FHIR)  │
          └───────────┬──────────────────────────────────┘
                      │
          ┌───────────▼──────────────────────────────────┐
          │  PHASE 4 — Upload                            │
          │                                               │
          │  For each tier (0, 1, 2, ...):               │
          │    Split into chunks of 200 resources        │
          │    POST hapi-fhir-target:8080/               │
          │      Bundle { type: batch, entry: [...] }    │
          │    Parse per-entry OperationOutcome          │
          │    Log errors with HAPI diagnostics          │
          └───────────┬──────────────────────────────────┘
                      │
          ┌───────────▼──────────────────────────────────┐
          │  PHASE 5 — Finalise                          │
          │  Write NDJSON to MEDANON_OUTPUT_DIR          │
          │  Update job status → "done"                  │
          └──────────────────────────────────────────────┘

GET /v1/jobs/abc123          → {"status": "running", "progress": {"processed":5000}}
GET /v1/jobs/abc123          → {"status": "done"}
GET /v1/jobs/abc123/result   → NDJSON download stream
```

---

## gPAS pseudonymization detail

```
action_dispatcher.py collects:
  BatchWork = {
    rule="pseudonymize patient id",
    values=["123", "456", "789"],
    paths=[resource["id"], resource["id"], resource["id"]]
  }

gpas_orchestrator.py:
  for val in values:
    key = ("pseudonymize", gpas_url, domain, operation, val)
    hit = local_lru_cache.get(key)          ← L1 (per-process, 50K entries)
    if not hit:
      hit = redis.get("medanon:gpas:" + key) ← L2 (cross-replica, 1h TTL)
    if not hit:
      uncached.append(val)

  if uncached:
    POST gpas:8080/$pseudonymizeAllowCreate
      Parameters { [123, 456, 789] }
    ← Parameters { psn-abc, psn-def, psn-ghi }

    cache all results in L1 + L2

  write pseudonyms back into resource fields
```

**Why cache at two levels?** L1 (per-process LRU) is fastest — no network. But across multiple anonymizer replicas, each replica has its own L1 and would call gPAS for the same patient IDs independently. L2 (Redis) shares results across replicas, dramatically reducing gPAS load during parallel bulk exports.

**Circuit breaker state machine:**

```
CLOSED (normal) ──► 5 failures in 60s ──► OPEN (fail fast)
                                               │
                                           30s elapsed
                                               │
                                               ▼
                              HALF-OPEN (1 probe call)
                                 success ──► CLOSED
                                 failure ──► OPEN (reset timer)
```

---

## Upload batch Bundle structure

Each upload chunk is a FHIR batch Bundle. This reduces N individual PUT requests to 1 HTTP call with per-entry outcomes:

```json
{
  "resourceType": "Bundle",
  "type": "batch",
  "entry": [
    {
      "resource": { "resourceType": "Organization", "id": "org-1", ... },
      "request": { "method": "PUT", "url": "Organization/org-1" }
    },
    {
      "resource": { "resourceType": "Organization", "id": "org-2", ... },
      "request": { "method": "PUT", "url": "Organization/org-2" }
    }
  ]
}
```

HAPI responds with a batch-response Bundle where each entry has a `response.status` and optional `response.outcome` (OperationOutcome with diagnostics). The writer parses each entry independently — one failure doesn't abort the whole batch.

---

## Job store backend selection

```
startup (api/main.py)
    │
    ├── MEDANON_REDIS_URL set?
    │       YES → RedisJobStore
    │               jobs stored as Redis hashes
    │               queue: BLPOP (worker wakes immediately on new job)
    │               status index: sorted set by created_at
    │               workers share queue across replicas
    │
    └── NO  → SqliteJobStore  (MEDANON_JOB_DB=/output/jobs.db)
                jobs stored as SQLite rows (WAL mode, thread-safe)
                worker polls every 2 s
                single-instance only
```

**Module re-export trap (resolved):** `from pipeline.jobs.store import _job_store` captures the value `None` at import time. When `init_job_store()` is later called, it writes to `pipeline.jobs.store._job_store` but the captured reference in `pipeline.jobs._job_store` stays `None` forever. This caused 503 errors on all job endpoints. Fix: `_get_store()` in `api/services/jobs.py` now reads `pipeline.jobs.store._job_store` directly from the authoritative module instead of from a re-exported alias.
