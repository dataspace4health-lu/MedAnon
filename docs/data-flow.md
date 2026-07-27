# MedAnon  Data Flow Reference

## Network layout

```
┌──────────────── host network ──────────────────────────────────────────┐
│                                                                        │
│  Browser ──► :8501 (UI)  :8000 (API)  :8082 (FHIR-target)  :8080 (gateway → gPAS)
│                                                                        │
│  (source FHIR: no host port, but the UI proxies /fhir/* to it, unauth) │
│  (gateway → NLP: :8200  host-published for direct access if needed)  │
└────────────────────────────────────────────────────────────────────────┘
                 │             │
        ┌────────▼─────────────▼──── processing-net ──────────────────────┐
        │                                                                  │
        │  medanon-ui:8501  (nginx reverse proxy)                          │
        │    / → React SPA                                                 │
        │    /api/* → anonymizer:8000  (least_conn, keepalive 16)          │
        │    /fhir/* → hapi-fhir:8080  (300 s timeout, dynamic DNS)        │
        │    /fhir-target/* → hapi-fhir-target:8080                        │
        │    /healthz → 200 OK                                             │
        │                                                                  │
        │  anonymizer:8000  (FastAPI)                                      │
        │    → hapi-fhir-target:8080  writes de-identified data            │
        │    → gpas-lb:8080           pseudonymization (Traefik gateway)   │
        │    → nlp-lb:8200            NLP batch detection (Traefik gateway)│
        │    → analytics:8100         risk analysis + synthetic data        │
        │    → app-db:5432            jobs, configs, subscriptions, staging │
        │    → redis:6379             job queue + gPAS L2 cache + audit    │
        │                                                                  │
        │  worker:8000/9091  (dedicated job executor)                      │
        │    → hapi-fhir-target:8080  bulk upload                          │
        │    → gpas-lb:8080           pseudonymization (Traefik gateway)   │
        │    → nlp-lb:8200            NLP batch (Traefik gateway)          │
        │    → app-db:5432            job state                            │
        │    → redis:6379             Redis Streams job queue            │
        │    :9091                    Prometheus metrics + health probe     │
        │                                                                  │
        │  gateway  (Traefik v3  joins network as gpas-lb + nlp-lb aliases)│
        │    :8080  → gpas replicas:8080  (round-robin; sticky for /gpas-web│
        │              JSF ViewState binding)                             │
        │    :8200  → nlp replicas:8200   (round-robin)                    │
        │    Discovery: Docker provider, read-only socket mount             │
        │    Joins network as gpas-lb + nlp-lb aliases (backward compat)    │
        │                                                                  │
        │  hapi-fhir-target:8080 → hapi-target-postgres:5432              │
        │  analytics:8100        (risk + synthetic  no external deps)     │
        │  app-db:5432           (jobs, configs, subscriptions, runs)      │
        │  redis:6379            (gPAS cache + job queue DB0, NLP cache DB2, audit stream)│
        │                                                                  │
        └──────────────────────────────────────────────────────────────────┘
        │
        ┌──── source-net (isolated) ─────────────────────┐
        │  hapi-fhir:8080                                │
        │    → hapi-postgres:5432                        │
        │  anonymizer and worker bridge both networks    │
        └────────────────────────────────────────────────┘
```

Two networks: `processing-net` (all services) + `source-net` (source FHIR + `hapi-postgres`). The source FHIR server publishes **no host port**.

**It is not, however, reachable only via the anonymizer.** The `ui` container joins `processing-net`, `source-net`, and `target-net`, and its nginx proxies `/fhir/*` straight to `hapi-fhir:8080` with no credential check. Having no host port keeps the source server off the host's network, but it does not put it behind authentication: `https://host:8501/fhir/Patient` returns identified data unauthenticated. See [security.md § 1.4](security.md#14-edge-routes-that-bypass-authentication).

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
    ├─ action_dispatcher.py  STAGE 1: match
    │                        redact:    resource["name"] = []
    │                        generalize: resource["birthDate"] = "1985"
    │                        gpas_pseudonymize: collect("123") → BatchWork
    │                        nlp_scrub/nlp_detect_act: defer → NlpWork
    │                        (neither NLP nor gPAS called yet)
    │
    │   ┌────────────── STAGES 2 & 3 RUN CONCURRENTLY ──────────────┐
    │   │ NLP scrubs free text while gPAS fetches pseudonyms;       │
    │   │ they write disjoint resource paths, so this is safe.      │
    ├───┤
    │   ├─ nlp_orchestrator.py  STAGE 2: phi_detection (if NlpWork non-empty)
    │   │                       Phase A: extract text from all NlpWork items
    │   │                       Phase B: deduplicate, batch-detect (1 HTTP call)
    │   │                       Phase C: per-resource token replacement
    │   │
    │   └─ gpas_orchestrator.py STAGE 3: pseudonymize (if BatchWork non-empty)
    │                           cache_get("123") → miss
    │                           POST gpas:8080/$pseudonymizeAllowCreate [123]
    │                           ← psn-abc456 ; cache_set ; resource["id"]="psn-abc456"
    │   └──────────────────────────────────────────────────────────┘
    │
    ├─ post_processor.py     STAGE 4: finalize
    │                        rewrite references: any {"reference":"Patient/123"}
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
              └── worker (XREADGROUP, wakes immediately)
                      │
          ┌───────────▼──────────────────────────────────┐
          │  PHASE 1  Discovery                          │
          │  GET /fhir/metadata → capability statement    │
          │  extract resource types: Patient, Condition,  │
          │  Observation, Encounter, Organization, ...    │
          └───────────┬──────────────────────────────────┘
                      │ MEDANON_FHIR_FETCH_PARALLEL=1 (sequential)
          ┌───────────▼──────────────────────────────────┐
          │  PHASE 2  Fetch + De-identify               │
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
          │  PHASE 3  Upload ordering                   │
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
          │  PHASE 4  Upload                            │
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
          │  PHASE 5  Finalise                          │
          │  Write NDJSON to MEDANON_OUTPUT_DIR          │
          │  (+ <data>.manifest.ndjson sidecar, one     │
          │   line per resource; inline meta.tag         │
          │   manifest stripped from released data)     │
          │  Score gate → publish_result                 │
          │  Update job status → "done"                  │
          └───────────┬──────────────────────────────────┘
                      │  publish_result → 3 correlated S3 artifacts
          ┌───────────▼──────────────────────────────────┐
          │  DELIVERY  dataspace S3 destination         │
          │  <key_prefix>data/<job>.ndjson[.gz]          │  de-identified data
          │  <key_prefix>manifests/<job>.manifest.ndjson │  transformation manifest
          │  <key_prefix>audit/<job>.audit.json          │  audit record (no PHI)
          │  distinct prefixes (per-type IAM) + shared   │
          │  <job> stem; keys recorded on                │
          │  job.params["delivered_to"]                  │
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

**Why cache at two levels?** L1 (per-process LRU) is fastest  no network. But across multiple anonymizer replicas, each replica has its own L1 and would call gPAS for the same patient IDs independently. L2 (Redis) shares results across replicas, dramatically reducing gPAS load during parallel bulk exports.

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

nlp_orchestrator.py (phi_detection stage):

  Phase A  Extract unique texts from all NlpWork items across all resources:
    texts = deduplicate([nw.text for nw in all_nlp_work])  # 1000 texts → 150 unique

  Phase B  Batch detect (one HTTP call via nlp-lb):
    POST http://nlp-lb:8200/v1/detect/batch
      {"texts": ["Patient John Smith...", "Dr. Jane Doe prescribed...", ...]}
    ← {"results": [
         {"text": "Patient John Smith...", "entities": [{"type":"PERSON", "start":8, "end":18}]},
         ...
       ]}

  Phase C  Per-resource replacement with isolated token_state:
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

**Fail-closed behavior:** If NLP is unavailable, all NLP-detected text is replaced with `[NLP_UNAVAILABLE]`  no PHI leaks.

---

## Routing layer

### UI nginx (`client/nginx.conf`)

```
Browser (:8501)
    │
    ├─ /                    → SPA static files (try_files → index.html)
    ├─ /api/*               → upstream anonymizer (:8000)
    │                         - least_conn, keepalive 16, 120s timeout
    │                         - client_max_body_size 20m
    ├─ /fhir/*              → hapi-fhir:8080 (source FHIR)
    │                         - proxy_read_timeout 300s
    │                         - dynamic DNS (resolver 127.0.0.11)
    ├─ /fhir-target/*       → hapi-fhir-target:8080 (de-identified)
    └─ /healthz             → 200 OK (Docker healthcheck)
```

### Traefik gateway (`medanon-gateway`)

gPAS and NLP routing is handled by the Traefik v3 gateway. `services/gpas/lb/nginx.conf` and `services/nlp/nginx.conf` are kept as reference only  they are not mounted by docker-compose.

```
anonymizer → gpas-lb:8080 (Traefik gateway alias)
    ├─ /gpas-web, /gras-web → gpas:8080 (sticky  JSF ViewState)
    └─ /*                   → gpas:8080 (round-robin)

anonymizer → nlp-lb:8200 (Traefik gateway alias)
    └─ /*                   → nlp:8200 (round-robin)
```

The gateway joins `processing-net` under `gpas-lb` and `nlp-lb` aliases so existing URL values in `.env` work without changes. New replicas are discovered automatically via Docker labels  no config reload needed.

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

HAPI responds with a batch-response Bundle where each entry has a `response.status` and optional `response.outcome` (OperationOutcome with diagnostics). The writer parses each entry independently  one failure doesn't abort the whole batch.

---

## Job store backend selection

```
startup (api/main.py or worker_main.py)
    │
    ├── MEDANON_REDIS_URL set?
    │       YES → RedisJobStore
    │               jobs stored as Redis hashes + sorted set by created_at
    │               queue: Redis Streams XREADGROUP (worker wakes immediately on new job)
    │               workers share queue across replicas
    │               secondary indexes: status set, type set (sinter for filtered list)
    │
    ├── NO, MEDANON_APP_DB_URL set?
    │       YES → PostgresJobStore
    │               jobs stored in medanon.jobs table
    │               queue: LISTEN/NOTIFY (event-driven, no polling)
    │               supports multiple worker replicas
    │               persists across Redis restarts
    │
    └── NO  → SqliteJobStore  (MEDANON_JOB_DB=/output/jobs.db)
                jobs stored as SQLite rows (WAL mode, thread-safe)
                worker polls every 2 s (no event-driven wake-up)
                single-instance only (file lock prevents multi-replica)
```

In the default Docker Compose stack, `MEDANON_APP_DB_URL` is auto-constructed from `MEDANON_APP_DB_PASSWORD` and `app-db`, so **PostgresJobStore is the default backend** (not SQLite). Redis is optional (adds L2 cache and cross-replica event-driven dispatch).

**Module re-export trap (resolved):** `from pipeline.jobs.store import _job_store` captures the value `None` at import time. When `init_job_store()` is later called, it writes to `pipeline.jobs.store._job_store` but the captured reference in `pipeline.jobs._job_store` stays `None` forever. Fixed: `_get_store()` reads directly from the authoritative module attribute.

---

## Scoring flow (MEDANON_SCORING_ENABLED=true)

Scoring runs as a background task after every processing call. It does not block the HTTP response.

```
POST /process  → de-identified result returned to caller (synchronous)
    │
    └── asyncio.create_task(score_and_persist(...))
              │
              ├─ scoring/engine.py (medanon-core)  compute composite score
              │      ├─ scoring/privacy.py    attacker model + HIPAA identifier check + text risk
              │      ├─ scoring/utility.py    field retention + semantic preservation + info loss
              │      └─ scoring/quality.py    success rate + rule coverage + schema + reference integrity
              │
              └─ integrations/postgres/processing_run_store.py
                     INSERT INTO medanon.processing_runs
                       (endpoint, config_profile, resource_count, composite_score,
                        privacy_score, utility_score, quality_score, created_at)
```

For bulk jobs, scoring is triggered explicitly:

```
POST /v1/jobs/{id}/score
    │
    └── ScoringService.score_job(job_id)
              │
              ├─ read NDJSON from /output/{job_id}.ndjson
              ├─ score each resource (privacy + utility + quality)
              ├─ aggregate across all resources
              ├─ write to medanon.processing_runs
              └─ generate Markdown audit report → /output/{job_id}_score_audit.md

GET /v1/jobs/{id}/score/report  → stream audit.md as text/markdown
```

---

## Governance & EHDS release flow (risk-driven export)

The risk-driven export closes the D7.2 Fig-6 loop: it does not just de-identify, it assesses the de-identified output and decides whether it may be released. A `permit_id` (and optional `recipient`, `declared_paths`, `optout_ids`) submitted with the job scopes every pseudonym to that permit and drives the disclosure gate.

```
POST /v1/jobs/risk-driven-export   { permit_id, recipient, declared_paths, optout_ids, ... }
    │
    ├─ api/deps.py::resolve_active_permit()   422 if the permit is unknown/inactive
    ├─ require_admin_for_reversal()           admin-only if the profile reverses pseudonyms
    │
    └── worker → pipeline/jobs/staged_worker/_risk.py   (MEDANON_OUTPUT_MODE=stream;
        │                                                 shards/AMQP fail closed if permit set)
        │   with permit_scope(permit_id):        ← contextvar; submit_with_context into pool workers
        │
        ├─ 1. exclusion.py            drop opt-out subjects + linked resources (Art 71)
        │                             matched on ORIGINAL identifiers, before gPAS
        ├─ 2. lattice solve           k / l / t generalisation  (keys + gPAS domains permit-scoped)
        ├─ 3. analytics/privacy_risk  re-id (k-anon) + DCR/NNDR + attribute-inference on the OUTPUT
        ├─ 4. disclosure/decision.py  R1-R8 output-check rules → APPROVE / REFER / REFUSE
        │        REFUSE  → delete the written NDJSON, raise → job fails (nothing exposed)
        │        REFER   → escalated to REFUSE in regulated mode
        ├─ 5. transformation_passport.py   anonymous passport (intent + achieved k/l/t + risk + verdict)
        │        └─ PostgresPassportStore.save()  after assert_pii_safe() structural guard
        └─ 6. save_result + checkpoint

GET /v1/reports            list durable passports
GET /v1/reports/{job_id}   full passport
```

Permit-scoped pseudonymisation (D7.2 §4.4): the same source subject yields **unrelated** pseudonyms across permits, stable within one permit.

```
cryptohash / tokenize / date_shift        gPAS
    │                                        │
    └─ derive_permit_key(HKDF, permit_id)    └─ domain suffixed  __permit-{id}
```

Advisory endpoints (`/v1/minimise/assess`, `/v1/export/decision`, `/v1/exposure/assess`, `/v1/export/statistical`, `/v1/catalog/descriptor`) run the same pure functions synchronously, evaluate-only, with no transformation and no dependency on the analytics/gPAS microservices.

---

## AI agent flow (MEDANON_AI_ENABLED=true)

AI endpoints use the `LLMProvider` singleton which wraps litellm. All calls go through a 3-state circuit breaker and a TTL-keyed response cache.

```
POST /v1/ai/generate-config
    │
    ├─ integrations/ai/provider.py  LLMProvider (litellm, circuit breaker, TTL cache)
    │      ├─ cache hit? → return cached response (MEDANON_AI_CACHE_TTL_SEC)
    │      └─ cache miss → litellm.completion(model, messages)
    │              ↓
    │          MEDANON_AI_PROVIDER=ollama/llama3.1  (model id)
    │          MEDANON_AI_API_BASE=http://ollama:11434  (--profile ai)
    │                        OR
    │          MEDANON_AI_API_BASE=https://api.openai.com  (external)
    │
    ├─ integrations/ai/agents/config_generator.py
    │      all 8 bundled YAML profiles injected as few-shot prompt context
    │      prompt: user description → LLM → YAML config
    │      validation: load_config()  rejects syntactically invalid YAML
    │      keyword fallback: if LLM unavailable, select closest bundled profile
    │
    └─ response: {config_yaml, rationale, profile_basis}

POST /v1/ai/detect-pii
    │
    ├─ integrations/ai/agents/pii_detector.py
    │      Layer 1: regex (SSN, phone, email, MRN)
    │      Layer 2: Presidio NER via NLP microservice (nlp-lb:8200)
    │      Layer 3: LLM  MUST use MEDANON_AI_PII_PROVIDER (local-only model)
    │               WARNING: no code-level enforcement of local-only  operator responsibility
    │
    └─ response: {entities: [{type, text, start, end, confidence}], layers_used}

POST /v1/ai/explain  (SSE streaming)
    │
    ├─ integrations/ai/agents/rule_explainer.py
    │      static fallback: pre-written explanations for each action type
    │      streaming: litellm.completion(stream=True) → SSE chunks
    │
    └─ response: text/event-stream  OR  application/json
```
