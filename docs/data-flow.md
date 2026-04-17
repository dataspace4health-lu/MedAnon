# MedAnon — Data Flow Reference

## Network layout

```
┌──────────────── host network ─────────────────────────────────┐
│                                                               │
│  Browser ──► :8501 (UI)   :8000 (API)    :8082   :8080       │
│                                          HAPI    gPAS-lb      │
│                           (source FHIR: no host port — isolated)
└──────────────────────────────────────────────────────────────┘
                 │             │
        ┌────────▼─────────────▼──── processing-net ───────────┐
        │                                                       │
        │  medanon-ui:8501                                      │
        │    nginx reverse proxy                                │
        │    /api/* → medanon:8000  (strips /api prefix)        │
        │    /fhir/* → hapi-fhir:8080  (300 s timeout)          │
        │    /fhir-target/* → hapi-fhir-target:8080             │
        │    / → React SPA (index.html fallback)                │
        │                                                       │
        │  medanon:8000  (anonymizer / FastAPI)                 │
        │    → hapi-fhir:8080        reads source data          │
        │    → hapi-fhir-target:8080 writes de-identified data  │
        │    → gpas-lb:8080          pseudonymization (LB)      │
        │    → app-db:5432           jobs, configs, staging     │
        │    → redis:6379            job queue + cache          │
        │                                                       │
        │  hapi-fhir-target:8080 → hapi-target-postgres:5432   │
        │  gpas-lb:8080     → gpas replicas:8080                │
        │  gpas replicas    → gpas-postgres:5432                │
        │  app-db:5432      (jobs, configs, subscriptions)      │
        │                                                       │
        └───────────────────────────────────────────────────────┘
        |
        ┌──── source-net (isolated) ─────┐
        │  hapi-fhir:8080                │
        │    → hapi-postgres:5432        │
        │  medanon bridges both networks │
        └────────────────────────────────┘
```

Two networks: `processing-net` (all services) + `source-net` (isolated: source FHIR + its DB). Only anonymizer/worker bridge both networks. Source FHIR has no host port — accessed only through anonymizer endpoints.

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
    ├─ pipeline/config/service.py  load YAML profile
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
    │                        nlp_scrub/nlp_detect_act: defer → NlpWork
    │                        (neither NLP nor gPAS called yet)
    │
    ├─ nlp_orchestrator.py   Pass 1.5 (only if NlpWork is non-empty)
    │                        Phase A: extract text fields from all NlpWork items
    │                        Phase B: deduplicate, batch-detect (1 HTTP call)
    │                        Phase C: per-resource token replacement
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

## NLP batch detection flow

```
action_dispatcher.py collects:
  NlpWork = [
    {rule="scrub narrative", path=resource["text"]["div"], text="Patient John Smith..."},
    {rule="scrub note", path=resource["note"][0]["text"], text="Dr. Jane Doe prescribed..."},
  ]

nlp_orchestrator.py (Pass 1.5):

  Phase A — Extract unique texts from all NlpWork items across all resources:
    texts = deduplicate([nw.text for nw in all_nlp_work])  # 1000 texts → 150 unique

  Phase B — Batch detect (one HTTP call via nlp-lb):
    POST http://nlp-lb:8200/v1/detect/batch
      {"texts": ["Patient John Smith...", "Dr. Jane Doe prescribed...", ...]}
    ← {"results": [
         {"text": "Patient John Smith...", "entities": [{"type":"PERSON", "start":8, "end":18}]},
         ...
       ]}

  Phase C — Per-resource replacement with isolated token_state:
    for resource in resources:
      for nw in resource.nlp_work:
        entities = batch_results[nw.text]
        replaced = replace_entities(nw.text, entities, token_state)
        # "Patient John Smith..." → "Patient [[PERSON_1]]..."
        write back to nw.path in resource
```

**Load-balanced NLP flow:**

```
anonymizer                   nlp-lb (:8200)                NLP replicas
    │                             │                              │
    │  POST /v1/detect/batch      │                              │
    │  {"texts": [...1000...]}    │                              │
    ├────────────────────────────►│                              │
    │                             │  least_conn selects replica  │
    │                             ├─────────────────────────────►│ nlp-1
    │                             │                              │ (Presidio)
    │                             │◄─────────────────────────────┤
    │◄────────────────────────────┤                              │
    │  {"results": [...]}         │                              │
```

**Fail-closed behavior:** If NLP is unavailable, all NLP-detected text is replaced with `[NLP_UNAVAILABLE]` — no PHI leaks.

---

## Nginx load balancer routing

Three nginx instances handle different routing concerns:

### UI nginx (`client/nginx.conf`)

```
Browser (:8501)
    │
    ├─ /                    → SPA static files (try_files → index.html)
    ├─ /api/*               → upstream anonymizer (:8000)
    │                         - least_conn balancing
    │                         - keepalive 16 connections
    │                         - client_max_body_size 20m
    │                         - proxy_read_timeout 120s
    ├─ /fhir/*              → hapi-fhir:8080 (source FHIR)
    │                         - proxy_read_timeout 300s
    │                         - dynamic DNS (resolver 127.0.0.11)
    ├─ /fhir-target/*       → hapi-fhir-target:8080 (de-identified)
    └─ /healthz             → 200 OK (Docker healthcheck)
```

### gPAS LB (`services/gpas/lb/nginx.conf`)

```
anonymizer → gpas-lb (:8080)
                │
                ├─ /ping        → 200 "pong" (nginx answers, no upstream)
                │                 Used during WildFly cold start (~90s)
                └─ /*           → round-robin to gpas:8080 replicas
                                  - proxy_connect_timeout 10s
                                  - proxy_read_timeout 180s
                                  - Docker DNS re-resolves on each connect
```

### NLP LB (`services/nlp/nginx.conf`)

```
anonymizer → nlp-lb (:8200)
                │
                ├─ /health      → 200 OK (nginx answers, no upstream)
                └─ /*           → least_conn to nlp:8200 replicas
                                  - proxy_read_timeout 120s
                                  - client_max_body_size 10m
                                  - least_conn for CPU-intensive inference
```

**Why least_conn for NLP?** NLP inference is CPU-bound and request durations vary. `least_conn` routes to the replica with fewest active connections, ensuring even load distribution under variable-length requests.

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
