# SPE FHIR BlackBox — Architecture

## Table of Contents

1. [Overview](#1-overview)
2. [Components](#2-components)
   - 2.1 [Common Components](#21-common-components)
   - 2.2 [Governance Authority — gPAS (Trusted Third Party)](#22-governance-authority--gpas-trusted-third-party)
   - 2.3 [De-identification Service — Anonymizer](#23-de-identification-service--anonymizer)
   - 2.4 [FHIR Data Layer — HAPI FHIR Server](#24-fhir-data-layer--hapi-fhir-server)
   - 2.5 [Operator Tooling — Streamlit Dashboard](#25-operator-tooling--streamlit-dashboard)
3. [Data Flows](#3-data-flows)
   - 3.1 [Core Processing Pipeline](#31-core-processing-pipeline)
   - 3.2 [Inline De-identification](#32-inline-de-identification)
   - 3.3 [Server-to-Server Round-Trip](#33-server-to-server-round-trip)
   - 3.4 [Risk Assessment Chain](#34-risk-assessment-chain)
   - 3.5 [Synthetic Data Generation (Adjacent Capability)](#35-synthetic-data-generation-adjacent-capability)
4. [Security Architecture](#4-security-architecture)
5. [Deployment Architecture](#5-deployment-architecture)
   - 5.1 [Docker Compose (Single-Node)](#51-docker-compose-single-node)
   - 5.2 [Kubernetes / Helm (Multi-Node)](#52-kubernetes--helm-multi-node)
6. [Configuration Profiles](#6-configuration-profiles)
7. [API Surface](#7-api-surface)
8. [Trust Boundaries](#8-trust-boundaries)
9. [Data State Lifecycle](#9-data-state-lifecycle)
10. [Operational Roles](#10-operational-roles)

---

## 1. Overview

**MedAnon** (also referred to as *SPE FHIR BlackBox*) is a rule-driven FHIR de-identification and pseudonymization engine for healthcare data. It accepts HL7 FHIR R4 resources in JSON, NDJSON, or XML format, applies configurable match-action rules from YAML profiles, and outputs transformed data in the same or a different format.

> **Scope note:** MedAnon is a privacy-processing stack for FHIR data built around three core components: the Anonymizer, the gPAS trusted third party, and the FHIR data layer.

### Purpose

The system enables healthcare organisations and researchers to:

- **De-identify** patient records for regulatory compliance (GDPR, HIPAA Safe Harbor)
- **Pseudonymize** identifiers via a Trusted Third Party (gPAS) for reversible, longitudinal studies
- **Assess re-identification risk** using k-anonymity and l-diversity metrics

### Key Properties

| Property | Value |
|---|---|
| Input formats | JSON, NDJSON, FHIR Bundle, XML |
| Output formats | JSON, NDJSON, XML |
| FHIR version | R4 |
| Rule language | FHIRPath expressions in YAML |
| Interfaces | REST API, Operator UI, CLI |
| Auth | API key + RBAC (required in production; see §4) |
| Deployment | Docker Compose, Kubernetes (Helm) |

### High-Level Architecture Diagram

The **Anonymizer service** is the orchestrator. The Rule Engine is internal logic within the Anonymizer — it does not directly communicate with external systems. All calls to gPAS and FHIR servers are made by the Anonymizer's integration clients, not by the Rule Engine itself.

```
  ┌───────────────────────────────────────────────────────┐
  │               OPERATOR / RESEARCHER                   │
  └──────────┬──────────────────────────┬─────────────────┘
             │ Browser (operator UI)    │ REST API / CLI
             ▼                          ▼
  ┌──────────────────┐      ┌───────────────────────────────────────────────────┐
  │  Streamlit UI    │      │              MedAnon Anonymizer  (port 8000)       │
  │  operator tooling│─────▶│  ┌─────────────────────────────────────────────┐  │
  │  (port 8501)     │      │  │  API layer: auth · RBAC · audit · middleware │  │
  └──────────────────┘      │  └──────────────────────┬──────────────────────┘  │
                             │                         │                          │
                             │  ┌──────────────────────▼──────────────────────┐  │
                             │  │  Rule Engine (FHIRPath + Actions)            │  │
                             │  │  Config loader · io_formats · crypto utils  │  │
                             │  └──────────────────────┬──────────────────────┘  │
                             │                         │ (only when rules require │
                             │  ┌──────────────────────▼──────────────────────┐  │
                             │  │  Integration clients (called by Anonymizer)  │  │
                             │  │  • gPAS adapter (gpas_pseudonymize rules)    │  │
                             │  │  • FHIR client  (fetch/upload workflows)     │  │
                             │  │  • NLP detector (nlp_detect rules)           │  │
                             │  └────────┬────────────────────┬────────────────┘  │
                             └───────────┼────────────────────┼───────────────────┘
                                         │                    │
                    internal network only │                    │ optional fetch/upload
                             ┌───────────▼────┐   ┌──────────▼──────────────────┐
                             │  gPAS  (TTP)   │   │  FHIR Server(s)             │
                             │  (port 8080)   │   │  source (8081) or target    │
                             └───────────┬────┘   └─────────────────────────────┘
                             ┌───────────▼────┐
                             │  MySQL         │
                             │  (internal)    │
                             └────────────────┘
```

---

## 2. Components

### 2.1 Common Components

These cross-cutting elements are shared across all services or form the foundation on which each component is built.

#### Rule Engine

The rule engine is internal logic within the Anonymizer service. Each de-identification profile is a YAML file containing an ordered list of rules:

```yaml
rules:
  - name: "descriptive rule name"
    match: "FHIRPath expression"   # e.g. Patient.name.family, *.id
    action: "action_name"           # redact | cryptohash | substitute | generalize | ...
    params:                         # action-specific parameters (optional)
      key: value
```

**FHIRPath evaluation** is performed by `fhirpathpy`; compiled expressions are cached in a module-level dict. Wildcards (`*.id`) match the field across all resource types.

**Actions available:**

| Action | Effect | Example use |
|---|---|---|
| `redact` | Delete field or set fixed blank value | Patient.photo |
| `cryptohash` | HMAC-SHA3-256 or SHA3-256 (one-way) | IDs in minimal/HIPAA/GDPR profiles |
| `generalize` | Reduce precision — `date_year`, `date_year_month`, `zip_prefix` | birthDate, postalCode |
| `perturb` | CSPRNG random offset on dates/numbers | Lab values for research |
| `substitute` | Replace with a literal value | Name → `[REDACTED]` |
| `scrub_text` | Regex-based PHI tokenization in text/XHTML | Narrative fields |
| `nlp_detect` | Presidio NER entity detection and tokenization | Clinical notes |
| `encrypt` | RSA-OAEP encryption | Cross-study linkage |
| `decrypt` | RSA-OAEP decryption | Reverse encryption |
| `gpas_pseudonymize` | Reversible pseudonym via gPAS TTP | IDs in gPAS/structural profiles |
| `gpas_depseudonymize` | Reverse gPAS pseudonym to original | Recontact workflows |

#### gPAS Availability and Profile Strategy

`gpas_pseudonymize` requires a live gPAS instance. If gPAS is unavailable, the circuit breaker opens and calls fail-fast — there is **no automatic runtime fallback** to a local hash. The correct resilience strategy is operational: select a non-gPAS profile (`gdpr`, `hipaa`, `research`) that uses `cryptohash` instead. This is a deliberate profile choice, not a transparent failover.

| gPAS available? | Recommended profile | ID strategy |
|---|---|---|
| Yes | `gpas`, `structural` | Reversible pseudonym (TTP-held mapping) |
| No | `gdpr`, `hipaa`, `research` | HMAC-SHA3-256 cryptohash (irreversible) |

#### Reference Rewriting

When `rewrite_references: true` is set in the config profile, the engine performs a post-processing pass over the entire Bundle:

1. Collect `{old_id → new_id}` for every resource processed.
2. Rewrite all `reference` fields (e.g. `"Patient/PAT-001"` → `"Patient/PSEUDO-f25c9686"`).
3. If `rewrite_text_ids: true`, replace literal ID strings in free-text fields.

This preserves FHIR referential integrity without adding explicit rules for every reference type.

#### Transformation Manifest

When `MEDANON_MANIFEST_ENABLED=true`, each processed resource carries a `meta.tag` entry that records which rules fired, which actions were applied, and which FHIRPath paths were matched. PHI values are never included. This supports GDPR Art. 30 accountability.

#### Shared Libraries

| Module | Role |
|---|---|
| `pipeline/io_formats.py` | Parse JSON / NDJSON / XML; serialize output; XXE-safe via `defusedxml` |
| `pipeline/config.py` | YAML loader; `${VAR:-default}` env-var interpolation; rule conflict detection |
| `utils/crypto.py` | RSA key management; `bounded_random()` (CSPRNG via `secrets.randbelow`) |
| `utils/fhirpath.py` | FHIRPath tree traversal helpers |
| `utils/metrics.py` | Prometheus counters + histograms |
| `utils/logging.py` | Request-ID context-var propagation |

#### Networking

All services communicate over an internal Docker bridge network (`fhir-net`). External ports are bound to `127.0.0.1` only. A reverse proxy (nginx, Caddy, or Traefik) is required for remote access and TLS termination.

---

### 2.2 Governance Authority — gPAS (Trusted Third Party)

gPAS is the **Trusted Third Party** (TTP) that holds the authoritative mapping between real identifiers and their pseudonyms. It is the governance boundary of the system: only gPAS holds the mapping table, and only authorised callers with gRAS credentials can request reversal.

#### Role

- Stores and manages pseudonym domains (e.g., `TESTING`, `STUDY-42`)
- Accepts a real identifier; returns a consistent, opaque pseudonym
- Supports reversal (`dePseudonymize`) for authorised re-contact workflows only
- Provides a web UI for domain administration

#### Architecture

```
MedAnon Anonymizer
  └─ integrations/gpas/dispatcher.py   ← adapter (pipeline calls)
       └─ integrations/gpas/client.py  ← HTTP client
            │  Retry (exp. backoff, default 2× @ 0.2 s)
            │  LRU cache (thread-safe, reduces TTP load)
            │  Circuit breaker (CLOSED → OPEN → HALF_OPEN)
            ▼
     gPAS WildFly container  (port 8080)
       └─ /ttp-fhir/fhir/gpas/$pseudonymizeAllowCreate
     gPAS MySQL container    (port 3306, internal only)
       └─ schemas: gpas (mappings), gras (users/roles)
```

#### Operations

| Operation | Behaviour | Config value |
|---|---|---|
| `pseudonymizeAllowCreate` | Create domain entry if missing (recommended) | Default |
| `pseudonymize` | Fail if identifier unknown to gPAS | Strict mode |
| `dePseudonymize` | Reverse pseudonym → original identifier | Re-contact |

#### Circuit Breaker States

```
CLOSED ──(threshold failures)──▶ OPEN ──(recovery timeout)──▶ HALF_OPEN
  ▲                                                                │
  └──────────────────(probe success)───────────────────────────────┘
```

Configurable via `GPAS_CB_FAILURE_THRESHOLD`, `GPAS_CB_RECOVERY_TIMEOUT_SEC`, `GPAS_CB_WINDOW_SEC`.

#### Authentication to gPAS

- **Bearer token (preferred):** `GPAS_TOKEN`
- **HTTP Basic (fallback):** `GPAS_BASIC_USER` + `GPAS_BASIC_PASS` (gRAS format: `user@ths`)

---

### 2.3 De-identification Service — Anonymizer

The Anonymizer is the core service and the single orchestrator of the stack. It exposes a FastAPI REST API, implements the rule engine, and owns all calls to external systems (gPAS, FHIR servers). Risk assessment and synthetic data generation are co-hosted but logically separate capabilities (see §3.4 and §3.5).

#### Internal Architecture

```
services/anonymizer/src/
├── api/
│   ├── main.py           ← FastAPI app; middleware stack; router registration
│   ├── auth.py           ← API-key auth; RBAC; structured audit log
│   ├── deps.py           ← Rate limiter; SSRF protection; config loader; shared helpers
│   └── routers/
│       ├── process.py    ← /process, /process/raw, /process/ndjson, /process/batch,
│       │                    /process/from-server, /process/everything,
│       │                    /process/and-upload, /process/round-trip
│       ├── analytics.py  ← /analyse/risk
│       ├── synthetic.py  ← /generate/synthetic
│       └── fhir_server.py
├── pipeline/
│   ├── config.py         ← YAML loader + env interpolation
│   ├── processor.py      ← Rule engine: FHIRPath match → action dispatch
│   └── io_formats.py     ← Multi-format parse + serialize
├── actions/              ← One file per action type
├── integrations/
│   ├── gpas/             ← gPAS client (dispatcher + HTTP client)
│   ├── fhir/             ← FHIR server client (fetch, upload, pagination)
│   └── nlp/              ← Presidio NER detector
└── analytics/
    ├── risk.py           ← k-anonymity / l-diversity metrics
    └── synthetic.py      ← Synthetic FHIR Patient generation
```

#### Middleware Stack (applied to every request)

```
Request → [auth_middleware]
        → [audit_middleware]
        → [enforce_body_size]     ← rejects bodies > MEDANON_MAX_BODY_BYTES (10 MB)
        → [request_id_middleware] ← propagates X-Request-ID
        → [metrics_middleware]    ← Prometheus counters + latency
        → Route handler
```

#### Processing Pipeline (per request)

```
HTTP body
  │
  ▼ parse_payload_bytes()              ← detect JSON / NDJSON / XML
  │
  ▼ _unwrap_to_resources()             ← flatten Bundle, list, or single resource
  │
  ▼ get_settings(profile)              ← load & cache YAML config (lru_cache, 8 slots)
  │
  ▼ asyncio.to_thread(process_data)    ← off event loop for CPU work
  │  └─ For each resource:
  │       For each rule (ordered):
  │         • Evaluate FHIRPath match
  │         • Dispatch matched nodes to action handler
  │           (gPAS adapter called here if action = gpas_pseudonymize)
  │         • Track processed paths (dedup)
  │  Post-pass:
  │    • rewrite_references (bundle cross-references)
  │    • rewrite_text_ids   (literal IDs in free text)
  │    • manifest tagging   (if MEDANON_MANIFEST_ENABLED=true)
  │
  ▼ Serialize (JSON / NDJSON / XML)
  │
  ▼ HTTP response (sync) or StreamingResponse (async NDJSON)
```

#### SSRF Protection

User-supplied `server_url` values are validated before any HTTP call:

- Non-`http(s)` schemes rejected (file://, gopher://, etc.)
- Raw IP addresses in private/loopback ranges rejected (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, ::1, fc00::/7)
- DNS names allowed — Docker service names (e.g., `hapi-fhir`) work correctly

Environment-variable URLs (set by administrators) bypass SSRF checks.

---

### 2.4 FHIR Data Layer — HAPI FHIR Server

HAPI FHIR acts as the primary data store for FHIR R4 resources. It can serve as both the source of raw patient data and the target for de-identified output. It is a standard upstream component — no custom code is added to HAPI.

#### Role

- Store and serve HL7 FHIR R4 Patient, Observation, Condition, and other resources
- Support paginated search (`GET /fhir/Patient?_count=200`)
- Support `$everything` operations (`GET /fhir/Patient/{id}/$everything`)
- Provide CORS access to the Streamlit UI and Anonymizer

#### FHIR Client (in Anonymizer)

```
integrations/fhir/client.py
  • Paginated fetch (auto-follows Bundle.link[rel=next])
  • $everything operation
  • Bulk export ($export) kick-off, polling, and NDJSON download
  • Resource upload (POST per resource type)
  • Exponential backoff retry (default 2× @ 0.3 s)
  • Configurable page size (FHIR_PAGE_SIZE, default 200)
```

#### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FHIR_SOURCE_URL` | `http://hapi-fhir:8080/fhir` | Source server base URL |
| `FHIR_SOURCE_TOKEN` | — | Bearer token for source server |
| `FHIR_TARGET_URL` | — | Target server base URL |
| `FHIR_TARGET_TOKEN` | — | Bearer token for target server |
| `HAPI_SERVER_ADDRESS` | `http://localhost:8081/fhir` | Public HAPI URL (returned in responses) |

---

### 2.5 Operator Tooling — Streamlit Dashboard

> **Role clarification:** The Streamlit UI is **operator and researcher tooling** — it is designed for internal clinical data engineers, data stewards, and researchers who already have access to the environment. It is not a hardened end-user portal and does not have its own independent authentication layer. In production, access should be controlled at the network/proxy level; the UI trusts the MedAnon API's auth model.

The Streamlit UI wraps the MedAnon REST API and provides a browser-based interface for common workflows.

#### Pages

| Page | Purpose |
|---|---|
| **Patient Browser** | Search patients in HAPI FHIR; de-identify complete `$everything` bundles |
| **Condition Browser** | Find patients by diagnosis (SNOMED/ICD codes); bulk de-identify |
| **Process Resource** | Paste or upload a single resource; view original vs. de-identified side-by-side |
| **Batch Processing** | Upload NDJSON/JSON/XML; stream de-identified output with progress bar |
| **Risk Assessment** | Upload de-identified resources; view k-anonymity, l-diversity, risk scores |
| **Synthetic Data** | Generate synthetic FHIR Patients preserving statistical distributions |
| **Status Dashboard** | Live health check for all backend services (30-second refresh) |

---

## 3. Data Flows

### 3.1 Core Processing Pipeline

```
INPUT
  │  JSON (single resource or Bundle)
  │  NDJSON (one resource per line)
  │  XML (defusedxml, XXE-safe)
  │
  ▼ io_formats.py — parse_payload_bytes()
  │
  ▼ io_formats.py — _unwrap_to_resources()  →  List[Dict]
  │
  ▼ pipeline/config.py — Settings(config_path)
  │  • Load YAML rules
  │  • Expand ${VAR:-default} environment variables
  │  • Check for rule conflicts
  │
  ▼ pipeline/processor.py — process_resource(resource, settings)
  │  For each rule (ordered):
  │    1. Compile / cache FHIRPath expression
  │    2. Evaluate match → list of matched nodes
  │    3. For each node: dispatch to action module
  │       (gpas_pseudonymize → gPAS TTP call, only if action requires it)
  │    4. Record (path, action_category) for dedup
  │  Post-pass:
  │    • rewrite_references (bundle cross-references)
  │    • rewrite_text_ids (literal IDs in free text)
  │    • Attach transformation manifest (if enabled)
  │
  ▼ io_formats.py — serialize(resources, format)
  │
OUTPUT
     Synchronous JSON response   OR   Streaming NDJSON
```

### 3.2 Inline De-identification

```
Client / UI
  │  POST /process   (Content-Type: application/json)
  ▼
  Anonymizer — parse → apply rules → serialize
  ▼
  HTTP 200 JSON (synchronous)
```

Best for single resources or small bundles. Config profile selected via `?config_profile=` query parameter.

### 3.3 Server-to-Server Round-Trip

```
POST /process/round-trip   (admin role required)
  { source_server_url, target_server_url, resource_types, ... }
          │
          ├── Anonymizer FHIR client fetches paginated resources from source
          │     GET /fhir/Patient?_count=200
          │     GET /fhir/Patient?_page_token=...  (follows next links)
          │
          ├── Per resource: apply rules (asyncio.to_thread)
          │     → gPAS TTP called for gpas_pseudonymize rules
          │
          └── Anonymizer FHIR client uploads to target
                POST /fhir/Patient
                POST /fhir/Observation
                ...
          │
          ▼ Streaming NDJSON status lines
            {"status":"ok","resource":"Patient/PSEUDO-xxx"}
            {"status":"error","resource":"...","detail":"..."}
```

### 3.4 Risk Assessment Chain

This is an analytics capability operating on **already de-identified** data. It does not touch raw PHI.

```
POST /analyse/risk   (Content-Type: application/x-ndjson)
          │  Input: de-identified NDJSON (output of a prior /process call)
          │
          ├── Extract Patient quasi-identifiers:
          │     gender, birth_year (from birthDate), zip_3 (postalCode[:3])
          │
          ├── Group records by (gender, birth_year, zip_3) tuples
          │
          ├── Compute k-anonymity:
          │     min_k = smallest group size
          │
          ├── Compute risk scores:
          │     prosecutor_risk = 1 / min_k
          │     journalist_risk = max(1/k_i for each group)
          │     marketer_risk = num_groups / total_records
          │
          └── If Condition resources present:
                Correlate via subject.reference
                Compute l-diversity per equivalence class
          │
          ▼ JSON risk report
            { "summary": { "min_k": 5, "risk_level": "low", ... },
              "groups": [...] }
```

### 3.5 Synthetic Data Generation (Adjacent Capability)

> **Note:** Synthetic data generation is an analytics utility, not part of the core de-identification pipeline. It operates on already de-identified data and produces statistically representative but entirely fabricated records. It does not interact with gPAS or FHIR servers.

```
POST /generate/synthetic?count=200&engine=auto
  (body: de-identified NDJSON of Patient resources — no raw PHI)
          │
          ├── Extract distributions from de-identified Patients:
          │     gender_distribution, birth_year_distribution, zip_prefix_distribution
          │
          ├── Engine selection:
          │     auto   → SDV (GaussianCopulaSynthesizer) if installed, else stdlib
          │     sdv    → GaussianCopulaSynthesizer (multivariate correlations)
          │     stdlib → Weighted per-attribute sampling (zero dependencies)
          │
          ├── Generate `count` synthetic Patients
          │     • New UUIDs as IDs
          │     • Sampled attributes with ±2-year jitter on birth year
          │     • meta.tag[code=SYN] added to every synthetic resource
          │
          └── Optional: generate linked Conditions (include_conditions=true)
          │
          ▼ Streaming NDJSON (synthetic records only — no real data)
```

---

## 4. Security Architecture

### Authentication & Authorization

API key authentication is controlled by the `MEDANON_API_KEY` environment variable:

| `MEDANON_API_KEY` set? | Behaviour |
|---|---|
| **Yes** | `X-API-Key` header required on all endpoints except open paths. Role assigned per key. |
| **No** | **Open mode — all callers receive `admin` role implicitly.** |

> **Production requirement:** Open mode (`MEDANON_API_KEY` unset) is **only acceptable for local development**. In any network-accessible or shared deployment, `MEDANON_API_KEY` **must** be set. Granting unauthenticated callers admin-level access to upload, round-trip, and data-export endpoints is a critical security risk. This default exists purely for developer convenience and must be explicitly overridden before go-live.

Role hierarchy (each role includes all roles below it):

```
admin
  └─ analyst
       └─ viewer
```

| Role | Permitted endpoints |
|---|---|
| `viewer` | `/health`, `/ready`, `/metrics`, `/docs`, `/openapi.json` |
| `analyst` | All `/process/*`, `/analyse/risk`, `/generate/synthetic` |
| `admin` | `/process/and-upload`, `/process/round-trip` |

### Security Layers

| Layer | Mechanism |
|---|---|
| **API auth** | `X-API-Key` header; required in production |
| **RBAC** | Three-tier role system enforced per endpoint |
| **Audit log** | Structured JSON, `/output/audit.log`; rotating (10 MB × 5 backups) |
| **Body size limit** | `MEDANON_MAX_BODY_BYTES` (default 10 MB); both Content-Length and chunked |
| **SSRF protection** | Private IP blocklist + scheme whitelist (http/https only) |
| **XML safety** | `defusedxml` — prevents XXE injection |
| **Container hardening** | Non-root `appuser` (UID 1000); read-only filesystem; no-new-privileges; capabilities dropped; `/tmp` tmpfs |
| **Rate limiting** | `slowapi` (30 req/min per IP on most endpoints); optional via env var |
| **CORS** | Configured allowlist via `MEDANON_CORS_ORIGINS`; credentials: false |
| **Cryptographic RNG** | `secrets.randbelow()` (CSPRNG) for all random offsets |
| **Path traversal** | `_validate_key_path()` restricts RSA key access to allowlisted directories |
| **PHI in logs** | Processing errors logged without resource content; raw keys never logged |
| **Hash key warning** | Startup warning if `MEDANON_HASH_KEY` unset (SHA3-256 without HMAC is rainbow-table vulnerable) |
| **Network isolation** | All ports bind to `127.0.0.1`; only reverse proxy port exposed externally |

---

## 5. Deployment Architecture

### 5.1 Docker Compose (Single-Node)

```
Services (fhir-net bridge network):

  gpas-db        port 3306  INTERNAL ONLY   MySQL 8.0
  fhir-server    port 8081  → 127.0.0.1     HAPI FHIR JPA
  gpas           port 8080  → 127.0.0.1     WildFly + gPAS
  anonymizer     port 8000  → 127.0.0.1     FastAPI (MedAnon)
  ui             port 8501  → 127.0.0.1     Streamlit
```

**Startup sequence** (health-check driven):
1. `gpas-db` — MySQL ready (~15 s)
2. `fhir-server` — HAPI ready (~30 s)
3. `gpas` — WildFly + gPAS ready (~90 s; depends on MySQL)
4. `anonymizer` — FastAPI ready (~10 s; depends on gPAS + FHIR)
5. `ui` — Streamlit ready (~10 s; depends on anonymizer)

**Resource requirements:**

| Service | RAM limit | RAM floor |
|---|---|---|
| anonymizer | 2 GB | 512 MB |
| fhir-server | 3 GB | 512 MB |
| gpas | 6 GB | 1 GB |
| gpas-db | 4 GB | 512 MB |
| ui | 512 MB | 128 MB |
| **Total** | **15.5 GB** | **~2.7 GB** |

Add ~800 MB to anonymizer if NLP model (`en_core_web_lg`) is loaded.

### 5.2 Kubernetes / Helm (Multi-Node)

**Chart layout:**

```
helm/
├── medanon/            ← umbrella chart
│   ├── Chart.yaml
│   ├── values.yaml     ← global overrides
│   └── templates/
│       └── ingress.yaml
└── charts/
    ├── anonymizer/     ← Deployment, Service, ConfigMap, Secret
    ├── fhir-server/    ← Deployment, Service, ConfigMap
    └── gpas/           ← Deployment + StatefulSet (MySQL), Services, Secrets
```

**Ingress path routing:**

| Path | Target service |
|---|---|
| `/` | `anonymizer` (port 8000) |
| `/fhir` | `fhir-server` (port 8080) |
| `/gpas-web` | `gpas` web UI (port 8080) |
| `/ttp-fhir` | `gpas` TTP-FHIR API (port 8080) |

**Security posture:**
- Secret checksums: Pods roll automatically when Secrets change
- Network policies: Each sub-chart restricts ingress to expected callers (requires a CNI that enforces NetworkPolicy, e.g. Cilium — not enforced with default Flannel/K3s)
- Non-root: Anonymizer UID 1000; `runAsNonRoot: true` on all pods
- Persistence: MySQL StatefulSet with PVC (survives pod restarts and upgrades)
- Monitoring: All pods annotated for Prometheus scraping (`/metrics` on anonymizer and gPAS; `/actuator/prometheus` on HAPI FHIR)

---

## 6. Configuration Profiles

Each profile is a YAML file under `services/anonymizer/config/`. The active profile is selected via the `?config_profile=` query parameter (API) or `--config` flag (CLI). If not specified, auto-selection applies: use `config_gpas.yaml` when `GPAS_URL` is set, else `config.yaml`.

| Profile key | File | Use case | ID strategy | Date strategy | gPAS required |
|---|---|---|---|---|---|
| `auto` | (auto-selected) | Default; selects based on env | — | — | — |
| `minimal` | `config.yaml` | Dev/test; no external services | cryptohash | regex scrub | No |
| `gpas` | `config_gpas.yaml` | Production; reversible pseudonyms | gpas_pseudonymize | year-only | **Yes** |
| `gdpr` | `config_gdpr_eu.yaml` | GDPR Art. 4(5) compliance | HMAC-SHA3-256 | year-only | No |
| `hipaa` | `config_hipaa_safe_harbor.yaml` | HIPAA Safe Harbor (45 CFR §164.514) | cryptohash | year-only; ZIP→3-digit | No |
| `research` | `config_research_pseudonymous.yaml` | IRB-grade longitudinal research | cryptohash | year-month | No |
| `structural` | `config_structure_preserving.yaml` | Preserve full FHIR structure; mask PII | gpas_pseudonymize | year-only | **Yes** |

### Structural Profile — Key Properties

The `structural` profile is designed for downstream consumers that require a complete FHIR structure:

- **Fields are never removed** — `action: substitute` replaces PII text with `[REDACTED]`; the field and its array structure remain
- **IDs are pseudonymized** via gPAS; cross-references are rewritten by `rewrite_references: true`
- **Clinical data is untouched** — no rules target codes, values, observations, conditions, or non-birthDate dates
- **birthDate** generalised to year only (`generalize: date_year`)
- **Binary blobs** (photo, attachment) are redacted (no meaningful text substitute)
- **Free text** (narrative, notes, comments) scrubbed inline via `scrub_text` + `nlp_detect`

---

## 7. API Surface

### Open Paths (no authentication required)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe — always 200 |
| GET | `/ready` | Readiness probe — checks gPAS, FHIR, NLP |
| GET | `/metrics` | Prometheus metrics |
| GET | `/docs` | Swagger UI |
| GET | `/openapi.json` | OpenAPI 3.0 schema |

### Processing Endpoints (analyst role minimum)

| Method | Path | Input | Output | Notes |
|---|---|---|---|---|
| POST | `/process` | JSON | JSON | Synchronous; single resource or Bundle |
| POST | `/process/raw` | Any format | Specified format | `input_format` + `output_format` query params |
| POST | `/process/ndjson` | NDJSON | NDJSON | Streaming; one resource per line |
| POST | `/process/batch` | JSON/NDJSON/XML | NDJSON | Unified batch endpoint |
| POST | `/process/from-server` | JSON params | NDJSON | Fetch from FHIR server; stream de-identified output |
| POST | `/process/everything` | JSON params | NDJSON | `$everything` for a single patient |

### Upload Endpoints (admin role required)

| Method | Path | Input | Output |
|---|---|---|---|
| POST | `/process/and-upload` | JSON (resource + target params) | JSON status |
| POST | `/process/round-trip` | JSON (source + target params) | NDJSON status stream |

### Analytics Endpoints (analyst role minimum)

| Method | Path | Input | Output |
|---|---|---|---|
| POST | `/analyse/risk` | NDJSON (de-identified resources) | JSON risk report |
| POST | `/generate/synthetic` | NDJSON (de-identified Patients) | NDJSON synthetic Patients |

### Common Query Parameters

- `config_profile` — Profile key (`auto`, `minimal`, `gpas`, `gdpr`, `hipaa`, `research`, `structural`)
- `/generate/synthetic?count=N` — Number of synthetic patients (1–10 000, default 100)
- `/generate/synthetic?engine=auto|sdv|stdlib` — Synthesis engine selection
- `/generate/synthetic?seed=N` — Random seed for reproducibility
- `/generate/synthetic?include_conditions=true` — Also generate linked Condition resources

---

## 8. Trust Boundaries

The system operates across four distinct zones. PHI exists in clear form only inside the Trusted Processing Zone. The TTP Zone is the only place where identifier mappings are held.

```
┌──────────────────────────────────────────────────────────────────────────┐
│  External Zone                                                            │
│  Operators, researchers, external clients, external FHIR servers         │
│  PHI state: NOT present (requests carry resource data at the boundary)   │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │ HTTPS + API Key (production)
                             │ or direct HTTP (dev/internal only)
┌────────────────────────────▼─────────────────────────────────────────────┐
│  API Boundary — MedAnon Anonymizer (port 8000)                           │
│  auth_middleware → RBAC → audit_middleware → SSRF protection             │
│                                                                           │
│  ┌─────────────────────────────────────────────────────────────────────┐ │
│  │  Trusted Processing Zone                                             │ │
│  │  Rule engine, Actions, Config, Crypto                                │ │
│  │  PHI state: PRESENT (resources deserialized and being transformed)  │ │
│  └─────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────┬───────────────────────────────────────────────┘
                           │ Internal network only (fhir-net / ClusterIP)
              ┌────────────┴────────────┐
              │                         │
┌─────────────▼──────────┐   ┌──────────▼──────────────────────────────────┐
│  TTP Zone              │   │  Data Zone                                   │
│  gPAS + MySQL          │   │  HAPI FHIR                                   │
│  PHI state: MAPPING    │   │  PHI state: SOURCE (raw) or SINK (output)    │
│  only — opaque tokens  │   │  Raw PHI on source side; de-identified data  │
│  and domain records    │   │  on target side (operator responsibility)    │
└────────────────────────┘   └─────────────────────────────────────────────┘
```

**Key trust rules:**

- De-pseudonymization (`dePseudonymize`) must only be performed by authorised users with gRAS credentials, not via the anonymizer API under normal operation.
- The target FHIR server should never receive raw PHI — it is the operator's responsibility to ensure the correct profile is applied before upload.
- Audit logs (`/output/audit.log`) are append-only and must be accessible only to the Audit Officer role (see §10).

---

## 9. Data State Lifecycle

This shows how identifiable data is transformed as it moves through the system. Understanding which state data is in at each stage is essential for compliance and operational governance.

```
① Raw PHI (FHIR R4 on source FHIR server or in API request body)
  │  All patient identifiers, dates, names, addresses,
  │  clinical codes, and narrative text present in clear form.
  │
  ▼ Anonymizer receives resource — PHI enters Trusted Processing Zone
  │
② In-Process (Anonymizer working memory only — never persisted)
  │  Resources deserialized as Python dicts.
  │  Rules applied sequentially. PHI fields overwritten in-place.
  │  gPAS called for IDs matching gpas_pseudonymize rules.
  │
  ▼ Processing complete — output leaves Trusted Processing Zone
  │
  ├──▶ ③a Pseudonymized Output  (reversible — gPAS profiles)
  │       IDs: opaque tokens held as domain mappings in gPAS MySQL
  │       Dates: generalized (year-only)
  │       Names/addresses: redacted or [REDACTED]
  │       Reversal: possible only via gPAS dePseudonymize, by authorised TTP admin
  │
  └──▶ ③b De-identified Output  (irreversible — cryptohash profiles)
          IDs: HMAC-SHA3-256 hash (one-way; no mapping stored anywhere)
          Dates: generalized (year-only or year-month depending on profile)
          Names/addresses: redacted
          Reversal: computationally infeasible

④ Identifier Mapping  (TTP Zone — gPAS MySQL only)
     original_id ↔ pseudonym mapping
     Never leaves gPAS. Accessed only via authenticated gRAS API.
     Survives pod/container restarts via PVC.

⑤ Output at Rest  (Data Zone — target FHIR server, or caller's storage)
     Contains only ③a or ③b data.
     Operator is responsible for ensuring raw PHI never reaches this layer.
```

---

## 10. Operational Roles

These are the human roles required to operate the system securely. Role separation prevents any single actor from both administering pseudonymization and accessing its reversal.

| Role | Responsibilities | Access |
|---|---|---|
| **MedAnon Operator** | Configure profiles; set `MEDANON_API_KEY`; issue API keys to researchers; manage `MEDANON_HASH_KEY` and RSA keys | Anonymizer config files, `.env`, container management |
| **gPAS Domain Administrator** | Create and manage pseudonymization domains; approve and execute de-pseudonymization requests for re-contact workflows | gPAS web UI (`/gpas-web`); gRAS credentials |
| **FHIR Server Administrator** | Control read/write access to source and target FHIR servers; ensure target servers contain only de-identified data | HAPI FHIR admin; database |
| **Audit Officer** | Review structured audit logs for policy compliance and anomaly detection; cannot modify logs | Read-only access to `/output/audit.log` |
| **Researcher / Analyst** | Call the anonymizer API to process, analyse, and generate synthetic data | API key with `analyst` role only; no access to raw FHIR, gPAS admin, or audit logs |

> **Separation of duty:** The gPAS Domain Administrator must be a different person from the Researcher/Analyst. If the same person controls both pseudonymization and its reversal, the TTP model is defeated.
