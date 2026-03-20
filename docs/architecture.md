# MedAnon — Architecture

This document describes the system architecture, all components, and data flow.

---

## System Overview

MedAnon is a **five-service stack**. Users interact through a **Streamlit UI** that calls the **anonymizer** API. The anonymizer applies YAML-driven de-identification rules and optionally calls **gPAS** for reversible pseudonymization. FHIR data is stored in and read from the **HAPI FHIR** server. All services communicate over an isolated Docker bridge network.

```
[Browser]
    │  HTTP :8501
    ▼
[Streamlit UI — medanon-ui]
    │  Patient/Condition search (direct FHIR)    │  De-identification / Risk (API calls)
    ▼                                             ▼
[HAPI FHIR :8081]                  [Anonymizer API :8000 — medanon]
                                                  │
                             ┌────────────────────┤
                             │  gPAS calls (:8080) │  FHIR server calls (:8081)
                             ▼                    ▼
                       [gpas-wildfly]       [hapi-fhir]
                             │ MySQL (internal)
                             ▼
                        [gpas-mysql]
```

All services share the `fhir-net` Docker bridge network. Host port bindings are on `127.0.0.1` only.

---

## Components

### 1. Streamlit UI (`medanon-ui`)

**Technology:** Python 3.12, Streamlit, requests, pandas
**Port:** 8501
**Image:** Built from `client/Dockerfile`

Browser-based front-end. Seven pages:

| Page | Purpose |
|---|---|
| Home | Stack health overview, navigation |
| Patient Browser | Search by name; de-identify via `$everything`; download NDJSON |
| Condition Browser | Search by illness name or SNOMED code; de-identify |
| Process Resource | Paste JSON / NDJSON / XML; compare input vs. output; download |
| Batch | Upload `.ndjson` file; stream processing; download result |
| Status | Live health dashboard for all backend services (auto-refresh 30 s) |
| **Risk Assessment** | **Upload de-identified NDJSON; compute k-anonymity + re-ID risk scores** |

**Client source (`client/`):**

```
app.py                      Home page + sidebar
pages/
  1_Patient_Browser.py      Patient search + $everything de-identification
  2_Process_Resource.py     Inline de-identification, side-by-side diff
  3_Batch.py                Bulk NDJSON upload/download
  4_Status.py               Health monitoring
  5_Condition_Browser.py    Condition/diagnosis search
  6_Risk_Assessment.py      k-anonymity + l-diversity + risk metrics UI
utils/
  api.py                    HTTP client → anonymizer API (incl. analyse_risk)
  fhir.py                   HTTP client → HAPI FHIR (patient/condition search)
```

---

### 2. Anonymizer (`medanon`)

**Technology:** Python 3.12, FastAPI, uvicorn, fhirpathpy, Presidio, spaCy
**Port:** 8000
**Image:** Built from `services/anonymizer/Dockerfile` (multi-stage: `base → prod`)

The core engine. Receives FHIR resources, evaluates a YAML rule set, applies de-identification actions, and computes re-identification risk metrics. Runs as non-root `appuser` in prod.

**Source layout (`services/anonymizer/src/`):**

```
api/main.py              HTTP entry point — 11 endpoints, middleware, body-size guard
cli/main.py              CLI — process / fetch / everything / push subcommands
pipeline/
  config.py              YAML loader — ${VAR:-default} env interpolation, rule validation
  processor.py           Rule engine — FHIRPath match, action dispatch, reference rewriting
  io_formats.py          Parse JSON / NDJSON / XML; serialize output (defusedxml → XXE-safe)
  deidentify.py          Action dispatcher for de-identification flows
actions/                 One module per action
  redact.py              Delete matched fields
  cryptohash.py          HMAC-SHA3-256 or plain SHA3-256 (warns if key absent)
  encrypt.py             RSA-OAEP encrypt (algorithm validated)
  decrypt.py             RSA-OAEP decrypt (JSON fallback to raw string)
  perturb.py             CSPRNG date/number perturbation (secrets.randbelow)
  substitute.py          Replace with fixed value
  generalize.py          Truncate dates, bracket ages, trim zip codes
  scrub_text.py          Regex PHI tokenization in free text and XHTML (per-call state)
analytics/               Re-identification risk metrics (pure Python, no new deps)
  risk.py                k-anonymity, l-diversity, prosecutor/journalist/marketer risk
integrations/            Self-contained adapters — one sub-package per external service
  gpas/
    client.py            gPAS HTTP client — retry, thread-safe LRU cache, bearer-over-basic auth
    dispatcher.py        Adapter bridge: routes pipeline calls to client
  nlp/
    detector.py          Presidio NER — names / locations / GDPR Art.9 entities (per-call state)
  fhir/
    client.py            FHIR REST — paginated fetch (same-origin validated), $everything, upload
utils/
  crypto.py              RSA helpers, bounded_random (CSPRNG), path traversal guard
  fhirpath.py            FHIRPath node traversal helpers (find_nodes, error, not_implemented)
  logging.py             Request ID propagation middleware
  metrics.py             Prometheus counters + histograms
```

**Config profiles (`services/anonymizer/config/`):**

```
config.yaml          Minimal: cryptohash + regex/NLP scrubbing, no gPAS (70 rules)
config_gpas.yaml     Production: gPAS pseudonymization + generalization + NLP (63 rules)
config_gdpr_eu.yaml  GDPR Art.4/5/25/32/89 HMAC profile (63 rules)
```

---

### 3. HAPI FHIR Server (`hapi-fhir`)

**Technology:** Java, Spring Boot, HAPI FHIR JPA Server
**Port:** 8081 (maps to container 8080)
**Image:** `hapiproject/hapi:latest`
**Database:** H2 in-memory (data lost on restart; use PostgreSQL for persistence)

Used as:
- **Source** — anonymizer fetches real patient data via `/process/from-server` or CLI `fetch`
- **Target** — anonymizer uploads de-identified data via `/process/and-upload` or `/process/round-trip`
- **Direct query target** — Streamlit UI searches patients and conditions directly

Configured via `services/fhir-server/config/application.yaml`.

---

### 4. gPAS (`gpas-wildfly`)

**Technology:** Java, WildFly 38, gPAS 2025.2.0
**Port:** 8080
**Image:** `mosaicgreifswald/wildfly:38`

TTP (Trusted Third Party) pseudonymization. The anonymizer calls the **TTP-FHIR gateway** (`/ttp-fhir/fhir/gpas`) to:
- Replace original IDs with pseudonyms (`$pseudonymizeAllowCreate`)
- Reverse pseudonyms to original IDs (`$dePseudonymize`)

Authentication: gRAS basic auth (FHIR API) and form-based auth (web UI at `/gpas-web/`).

**Config files** (`services/gpas/envs/`):

| File | Purpose |
|---|---|
| `ttp_commons.env` | DB hostname, logging |
| `ttp_gpas.env` | gPAS settings, auth mode (`gras`) |
| `ttp_gras.env` | gRAS DB hostname |
| `ttp_fhir.env` | TTP-FHIR gateway toggles |
| `ttp_noti.env` | Notification service (optional) |
| `wf_commons.env` | WildFly JVM settings |

---

### 5. gPAS MySQL (`gpas-mysql`)

**Technology:** MySQL 8.0
**Port:** Internal only
**Image:** `mysql:8.0`

Three schemas:

| Schema | Contents |
|---|---|
| `gpas` | Pseudonym mappings (`domain`, `psn`, `mpsn` tables) |
| `gras` | gRAS users, groups, roles, permissions |
| `notification_service` | Event notification log |

Init scripts in `services/gpas/sqls/` run on first boot.

---

## Data Flow

### Flow 1: Inline de-identification (UI Process Resource page)

```
Browser
  │  FHIR JSON/NDJSON/XML (pasted)
  ▼
Streamlit UI — Process Resource page
  │  POST /process/raw?output_format=json
  ▼
Anonymizer
  ├─ Load rules from config_gpas.yaml
  ├─ FHIRPath: "*.id"         → gpas_pseudonymize  → gPAS stores p-001 → x9k2m3
  ├─ FHIRPath: "Patient.name" → redact             (field deleted)
  ├─ FHIRPath: "Patient.birthDate" → generalize    ("1980-05-12" → "1980")
  └─ Return transformed resource
  ▼
Streamlit UI — side-by-side diff view
```

---

### Flow 2: Patient browser — search and de-identify

```
Browser
  │  Patient name search
  ▼
Streamlit UI — Patient Browser
  ├─ GET hapi-fhir:8080/fhir/Patient?name=<query>   (direct FHIR call)
  │    → render patient cards
  │
  │  User clicks "De-identify $everything"
  ├─ POST /process/everything   (anonymizer call)
  │    → anonymizer fetches Patient/$everything from HAPI FHIR
  │    → applies all rules
  │    → streams NDJSON back
  └─ UI shows / downloads de-identified NDJSON
```

---

### Flow 3: Batch NDJSON processing

```
Browser → Upload .ndjson file
  │
  ▼
Streamlit UI — Batch page
  │  POST /process/ndjson   (streaming)
  ▼
Anonymizer
  ├─ Parse NDJSON line by line
  ├─ Batch gPAS calls (all IDs in one HTTP request)
  ├─ Apply all rules per resource
  └─ Stream de-identified NDJSON back
  ▼
Streamlit UI — progress bar + download button
```

---

### Flow 4: Risk assessment

```
Browser → Upload de-identified .ndjson (Patient + optional Condition resources)
  │
  ▼
Streamlit UI — Risk Assessment page
  │  POST /analyse/risk
  ▼
Anonymizer
  ├─ Parse NDJSON
  ├─ Extract quasi-identifiers from Patient resources
  │    (gender, birth_year[:4], zip_prefix[:3])
  ├─ Build Condition code map (correlate by subject.reference)
  ├─ Compute k-anonymity (group by QI tuple, Counter)
  │    → min_k, prosecutor_risk = 1/min_k
  │    → journalist_risk = max(1/k_i)
  │    → marketer_risk = num_groups / total_records
  │    → risk_level: low (k≥5) / medium (k≥3) / high (k≥2) / critical (k=1)
  └─ Compute l-diversity (distinct Condition codes per group)
  ▼
Streamlit UI — metric cards, bar chart, per-group table, recommendations, download JSON
```

---

### Flow 5: Round-trip (fetch → de-identify → upload)

```
Source FHIR server                  Anonymizer                Target FHIR server
       │                                │                              │
       │◄── GET /Patient?_count=200 ────│                              │
       │─── Bundle ────────────────────►│                              │
       │                                │──── apply rules ────────────►│◄─ POST /Patient
       │◄── GET /Observation ... ───────│                              │
       │─── Bundle ────────────────────►│──── apply rules ────────────►│◄─ POST /Observation
       │                            stream status lines to client
```

---

### Flow 6: Text scrubbing (dual-pass)

```
Raw text node (*.text, *.note.text)
  │
  ▼
Pass 1: scrub_text (regex — ReDoS-safe patterns, per-call state)
  ├─ SSN patterns     → [[SSN_1]]
  ├─ Phone numbers    → [[PHONE_1]]
  ├─ Email addresses  → [[EMAIL_1]]
  ├─ Date patterns    → [[DATE_1]]
  └─ MRN patterns     → [[MRN_1]]
  │
  ▼
Pass 2: nlp_detect (Presidio + spaCy en_core_web_lg, per-call state)
  ├─ PERSON entities  → [[PERSON_2]]
  ├─ LOCATION         → [[LOCATION_3]]
  ├─ AGE              → [[AGE_4]]
  └─ GDPR Art.9 types → [[MEDICAL_5]], etc.
  │
  ▼
Tokenized output (no PHI)
```

---

### Config / pseudonymization decision

```
Is GPAS_URL set in environment?
          │
    ┌─────┴──────┐
   YES            NO
    │              │
    ▼              ▼
config_gpas.yaml  config.yaml (or config_gdpr_eu.yaml)
    │                    │
    ▼                    ▼
gpas_pseudonymize    cryptohash
    │                    │
    ▼                    ▼
gPAS stores mapping  HMAC-SHA3-256(value, MEDANON_HASH_KEY)
→ reversible         → one-way (reversible only with key)
```

If gPAS is configured but unreachable, the engine raises an error — no silent fallback.

---

## Re-identification Risk: gPAS vs. k-anonymity

These two mechanisms are **complementary, not alternatives**:

| Attack vector | Mitigation |
|---|---|
| Lookup by known Patient ID / MRN | gPAS pseudonymization — replaces direct identifiers |
| Demographic linkage (age + zip + gender + external census/insurance data) | k-anonymity on quasi-identifiers |
| Disease fingerprinting (rare diagnosis + birth year = unique person) | l-diversity on Condition codes per group |

With `config_gpas.yaml`, `gender` and `address` are **redacted**, so the only Patient-level quasi-identifier in the output is `birthDate` (year). Residual re-identification risk comes mainly from **Condition code + birth year combinations**. The `/analyse/risk` endpoint quantifies this.

---

## Network Topology

```
Host machine
├── 127.0.0.1:8000  ─── medanon container         (anonymizer API)
├── 127.0.0.1:8080  ─── gpas-wildfly container    (gPAS FHIR API + web UI)
├── 127.0.0.1:8081  ─── hapi-fhir container       (HAPI FHIR R4)
└── 127.0.0.1:8501  ─── medanon-ui container      (Streamlit browser UI)

Docker bridge: fhir-net
├── medanon:8000         anonymizer (service name used by UI + gPAS calls)
├── medanon-ui:8501      Streamlit UI
├── hapi-fhir:8080       exposed as :8081 on host
├── gpas-wildfly:8080    exposed as :8080 on host
└── gpas-mysql:3306      internal only
```

For remote access, place a reverse proxy (nginx, Caddy, Traefik) in front and terminate TLS there.

---

## Security Model

| Layer | Mechanism | Status |
|---|---|---|
| Anonymizer API | Optional `X-API-Key` header (`MEDANON_API_KEY`) | Configurable |
| gPAS FHIR API | gRAS basic auth (`GPAS_BASIC_USER` / `GPAS_BASIC_PASS`) | Enabled |
| gPAS web UI | gRAS form-based auth (`admin@ths` / configured password) | Enabled |
| HAPI FHIR | No auth (Spring Security not configured) | Open |
| Network isolation | All ports bound to `127.0.0.1`; `gpas-mysql` internal only | Enabled |
| XML parsing | defusedxml (XXE protection) | Enabled |
| Body size | Middleware rejects > `MEDANON_MAX_BODY_BYTES` (default 10 MB) | Enabled |
| Container user | Non-root `appuser` (UID 1000) in prod stage | Enabled |
| CSPRNG | `secrets.randbelow()` for all perturbation offsets | Enabled |
| Path traversal | `_validate_key_path()` in crypto.py | Enabled |
| SSRF (FHIR pagination) | `_safe_next_url()` same-origin check | Enabled |
| SSRF (server URLs) | Private IP blocklist in api/main.py | Enabled |
| Regex patterns | ReDoS-safe (no nested quantifiers, bounded lengths) | Enabled |
| PHI in logs | `exc_info=False` on processing errors | Enabled |
| gPAS cache | Thread-safe LRU with `threading.Lock()`, 10% eviction | Enabled |
| Cryptohash key | Warns (WARNING) when `MEDANON_HASH_KEY` unset | Enabled |

---

## Technology Stack

| Layer | Technology | Version |
|---|---|---|
| UI framework | Streamlit | ≥ 1.35 |
| Anonymizer runtime | Python | 3.12 |
| Web framework | FastAPI + uvicorn | pinned in requirements.txt |
| FHIR path engine | fhirpathpy | 0.1.0 |
| NLP entity detection | Microsoft Presidio + spaCy | ≥ 2.2 / ≥ 3.8 |
| spaCy model | en_core_web_lg | latest |
| XML parsing | defusedxml | 0.7.1 (pinned) |
| Crypto library | pycryptodome | 3.23.0 (pinned) |
| FHIR server | HAPI FHIR JPA Server | latest |
| TTP service | gPAS | 2025.2.0 |
| TTP app server | WildFly | 38 |
| TTP database | MySQL | 8.0 |
| Container runtime | Docker + Docker Compose | v2 |
| Kubernetes packaging | Helm | v3 |
| Linting / formatting | ruff | latest |
| Testing | pytest | latest |

---

## Deployment Modes

### Docker Compose (local / single-node)

All containers on one host. Hot-reload dev mode via `make dev`.

| Service | Host port | Container port |
|---|---|---|
| UI (Streamlit) | 8501 | 8501 |
| Anonymizer API | 8000 | 8000 |
| gPAS | 8080 | 8080 |
| HAPI FHIR | 8081 | 8080 |
| MySQL | (internal) | 3306 |

### Kubernetes (Helm)

Chart at `helm/charts/` with three sub-charts: `anonymizer`, `gpas`, `fhir-server`.

| Aspect | Docker Compose | Helm / Kubernetes |
|---|---|---|
| Images | Built locally | Pushed to a registry |
| Config | `.env` file | `values.yaml` + K8s Secrets |
| Persistence | Named Docker volume | PersistentVolumeClaim (StatefulSet) |
| MySQL init | `docker-entrypoint-initdb.d` | ConfigMap mount |
| Health checks | Docker healthcheck | liveness + readiness probes |
| Startup order | `depends_on` | initContainers (`nc -z` TCP check) |

See [deployment.md](deployment.md) for full instructions.
