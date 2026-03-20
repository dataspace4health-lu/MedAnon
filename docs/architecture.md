# MedAnon Architecture

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Services](#2-services)
3. [End-to-End Data Flow](#3-end-to-end-data-flow)
4. [Processing Flows](#4-processing-flows)
5. [Authentication and Authorization](#5-authentication-and-authorization)
6. [Rule Engine](#6-rule-engine)
7. [Text Scrubbing Pipeline](#7-text-scrubbing-pipeline)
8. [Config Profile Selection](#8-config-profile-selection)
9. [Re-identification Risk Model](#9-re-identification-risk-model)
10. [Network Topology](#10-network-topology)
11. [Security Model](#11-security-model)
12. [Source Layout](#12-source-layout)
13. [Technology Stack](#13-technology-stack)
14. [Deployment Modes](#14-deployment-modes)

---

## 1. System Overview

MedAnon is a rule-driven FHIR de-identification and pseudonymization engine. It accepts FHIR resources (JSON, NDJSON, XML), applies match-action rules from a YAML configuration, and outputs transformed data. The system runs as an eight-service Docker stack with three user-facing interfaces: a Streamlit web UI, a FastAPI REST API, and a CLI for bulk processing.

```
                           ┌──────────────────────────────────────────────────┐
                           │                  Browser                        │
                           │                                                 │
                           │  ┌──────────┐  ┌──────────────┐  ┌──────────┐  │
                           │  │ Streamlit│  │  FHIR Proxy  │  │gPAS Proxy│  │
                           │  │  :8501   │  │   :4180      │  │  :8082   │  │
                           │  └────┬─────┘  └──────┬───────┘  └────┬─────┘  │
                           └───────┼───────────────┼───────────────┼────────┘
                                   │               │               │
                        ┌──────────┘     ┌─────────┘     ┌────────┘
      ┌─────────────────▼─────┐  ┌───────▼──────┐  ┌─────▼──────────┐
      │   Anonymizer API      │  │  HAPI FHIR   │  │  gPAS WildFly  │
      │   :8000 (FastAPI)     │  │  :8080       │  │  :8080         │
      │                       │  │  (internal)  │  │  (internal)    │
      │  ┌─────────────────┐  │  └──────────────┘  └───────┬────────┘
      │  │  Rule Engine    │  │                            │
      │  │  FHIRPath match │  │                    ┌───────▼────────┐
      │  │  → dispatch     │──┼───FHIR REST───────►│  gPAS MySQL    │
      │  │    action       │  │                    │  :3306         │
      │  └─────────────────┘  │                    │  (internal)    │
      └───────────┬───────────┘                    └────────────────┘
                  │
          ┌───────▼────────┐
          │   Keycloak     │
          │   :8180        │
          │   (OIDC IdP)   │
          └────────────────┘
```

All services communicate over the `fhir-net` Docker bridge network. HAPI FHIR and gPAS have no host ports in production; they are only reachable through their respective OAuth2 proxies or via the anonymizer service internally.

---

## 2. Services

### 2.1 Anonymizer (`anonymizer`)

The core engine. Receives FHIR resources, evaluates a YAML rule set against each resource using FHIRPath expressions, dispatches matching rules to action handlers, and returns transformed data.

| Attribute | Value |
|---|---|
| Runtime | Python 3.12, FastAPI, uvicorn |
| Port | 8000 |
| Image | `medanon:latest` (multi-stage: `base` -> `prod` / `dev`) |
| Container user | Non-root `appuser` (UID 1000) |
| Security | `read_only: true`, `no-new-privileges`, all capabilities dropped |
| Memory | Up to 2 GB (required when spaCy NLP model is loaded) |

Provides 14 REST endpoints, a CLI interface, and Prometheus metrics at `/metrics`.

### 2.2 Streamlit UI (`ui`)

Browser-based front-end built with Python Streamlit. Seven pages cover patient browsing, inline de-identification, batch processing, risk assessment, synthetic data generation, and a health dashboard.

| Attribute | Value |
|---|---|
| Runtime | Python 3.12, Streamlit |
| Port | 8501 |
| Image | `medanon-ui:latest` |
| Memory | Up to 512 MB |
| Auth | Keycloak OIDC with PKCE (S256) via `streamlit-keycloak` |

### 2.3 HAPI FHIR Server (`fhir-server`)

HL7 FHIR R4 server used as both the source of patient data and the target for uploading de-identified resources.

| Attribute | Value |
|---|---|
| Runtime | Java, Spring Boot, HAPI FHIR JPA Server |
| Image | `hapiproject/hapi:v7.6.0` |
| Port | Internal only (no host binding in production) |
| Database | H2 in-memory by default (switch to PostgreSQL for persistence) |
| Memory | Up to 3 GB |
| Access | Through `fhir-proxy` (OAuth2-proxy) for browser access; direct internal calls from the anonymizer |

### 2.4 gPAS (`gpas`)

Trusted Third Party (TTP) pseudonymization service. Manages bidirectional mapping between original identifiers and pseudonyms. The anonymizer calls the TTP-FHIR gateway at `/ttp-fhir/fhir/gpas` to create and resolve pseudonyms.

| Attribute | Value |
|---|---|
| Runtime | Java, WildFly 38, gPAS 2025.2.0 |
| Image | `mosaicgreifswald/wildfly:38` |
| Port | Internal only (no host binding in production) |
| Auth | gRAS basic auth for the FHIR API; form-based auth for the web UI |
| Memory | Up to 6 GB (JVM: `-Xms512M -Xmx4G`) |
| Access | Through `gpas-proxy` for browser access; internal `/ttp-fhir/` API is open for anonymizer service calls |

### 2.5 gPAS MySQL (`gpas-db`)

MySQL 8.0 backend for gPAS. Stores three schemas: `gpas` (pseudonym mappings), `gras` (users, roles, permissions), and `notification_service` (event logs).

| Attribute | Value |
|---|---|
| Image | `mysql:8.0` |
| Port | Internal only |
| Persistence | Docker named volume `gpas-db-data` |
| Memory | Up to 4 GB (InnoDB buffer pool: 512 MB) |

### 2.6 Keycloak (`keycloak`)

Central identity provider for the entire stack. Manages user authentication (OIDC), role-based access control, and service-account token issuance.

| Attribute | Value |
|---|---|
| Image | `quay.io/keycloak/keycloak:24.0.5` |
| Port | 8180 (host) -> 8080 (container) |
| Realm | `medanon` |
| JVM heap | 256 MB min, 512 MB max |
| Persistence | Docker named volume `keycloak-data` |

Pre-configured with three realm roles (`viewer`, `analyst`, `admin`), five clients, and three seed users.

### 2.7 FHIR Proxy (`fhir-proxy`)

OAuth2 reverse proxy that gates browser access to HAPI FHIR behind Keycloak login.

| Attribute | Value |
|---|---|
| Image | `quay.io/oauth2-proxy/oauth2-proxy:v7.6.0` |
| Port | 4180 |
| Open path | `/fhir/metadata` (FHIR capability statement) |

### 2.8 gPAS Proxy (`gpas-proxy`)

OAuth2 reverse proxy that gates browser access to gPAS behind Keycloak login.

| Attribute | Value |
|---|---|
| Image | `quay.io/oauth2-proxy/oauth2-proxy:v7.6.0` |
| Port | 8082 |
| Open path | `/ttp-fhir/` (internal API for anonymizer service calls) |

---

## 3. End-to-End Data Flow

This is the complete path data takes from input to output through the system.

```
              ┌───────────────────────────────────────────────────────────────────┐
              │                        INPUT                                     │
              │   JSON    NDJSON    XML    FHIR Server fetch    Pasted in UI     │
              └──────────────────────────┬────────────────────────────────────────┘
                                         │
                                         ▼
              ┌──────────────────────────────────────────────────────────────────┐
              │  1. PARSE                                                        │
              │     io_formats.py: parse_payload_bytes() + _unwrap_to_resources()│
              │     Detects format (JSON / NDJSON / XML via defusedxml)          │
              │     Returns a list of individual FHIR resource dicts             │
              └──────────────────────────┬───────────────────────────────────────┘
                                         │
                                         ▼
              ┌──────────────────────────────────────────────────────────────────┐
              │  2. LOAD CONFIG                                                  │
              │     config.py: Settings()                                        │
              │     Reads YAML rule file, interpolates ${VAR:-default} env vars  │
              │     Validates rules for conflicts (same path, different actions) │
              └──────────────────────────┬───────────────────────────────────────┘
                                         │
                                         ▼
              ┌──────────────────────────────────────────────────────────────────┐
              │  3. PROCESS (per resource)                                       │
              │     processor.py: process_resource()                             │
              │                                                                  │
              │     For each rule in config:                                     │
              │       a. Evaluate FHIRPath match expression against resource     │
              │          (cached compiled expressions via _fhirpath_cache)       │
              │       b. If matched → dispatch to action handler:                │
              │                                                                  │
              │          ┌────────────────────────────────────────────────┐      │
              │          │ De-identification actions:                     │      │
              │          │   redact      → delete field or set fixed val  │      │
              │          │   cryptohash  → HMAC-SHA3-256 (keyed) or      │      │
              │          │                 SHA3-256 (plain)               │      │
              │          │   generalize  → reduce precision (dates,      │      │
              │          │                 zips, numbers, ages)           │      │
              │          │   perturb     → CSPRNG random offset          │      │
              │          │   substitute  → replace with literal          │      │
              │          │   scrub_text  → regex PHI tokenization        │      │
              │          │   nlp_detect  → Presidio NER tokenization     │      │
              │          │   encrypt     → RSA-OAEP encrypt              │      │
              │          │   decrypt     → RSA-OAEP decrypt              │      │
              │          ├────────────────────────────────────────────────┤      │
              │          │ Pseudonymization actions (via gPAS):          │      │
              │          │   gpas_pseudonymize   → create/lookup pseudonym│      │
              │          │   gpas_depseudonymize → reverse to original   │      │
              │          └──────────────┬─────────────────────────────────┘      │
              │                         │                                        │
              │       c. If rewrite_references: true →                           │
              │          Update FHIR References in Bundle to use new IDs         │
              │       d. If rewrite_text_ids: true →                             │
              │          Replace original IDs in free-text fields                │
              │       e. If MEDANON_MANIFEST_ENABLED →                           │
              │          Attach transformation manifest to meta.tag              │
              └──────────────────────────┬───────────────────────────────────────┘
                                         │
                                         ▼
              ┌──────────────────────────────────────────────────────────────────┐
              │  4. SERIALIZE                                                     │
              │     io_formats.py: serialize to requested format                  │
              │     JSON / NDJSON (streaming) / XML                               │
              └──────────────────────────┬───────────────────────────────────────┘
                                         │
                                         ▼
              ┌──────────────────────────────────────────────────────────────────┐
              │                         OUTPUT                                   │
              │   JSON response    NDJSON stream    XML response    File download │
              └──────────────────────────────────────────────────────────────────┘
```

---

## 4. Processing Flows

### Flow 1: Inline De-identification (UI or API)

A user pastes a FHIR resource or sends it via `POST /process`.

```
Browser / curl
  │  FHIR JSON/NDJSON/XML
  ▼
Anonymizer API (/process, /process/raw)
  ├─ Parse input → list of resources
  ├─ For each resource:
  │   ├─ FHIRPath: "*.id"             → cryptohash or gpas_pseudonymize
  │   ├─ FHIRPath: "Patient.name"     → redact
  │   ├─ FHIRPath: "Patient.birthDate"→ generalize ("1980-05-12" → "1980")
  │   ├─ FHIRPath: "*.text.div"       → scrub_text (regex) + nlp_detect (NER)
  │   └─ rewrite references + text IDs
  └─ Return transformed resource(s)
```

### Flow 2: Patient Browser ($everything)

A user searches for a patient in the UI, then de-identifies all linked resources.

```
Browser
  │  Patient name search
  ▼
Streamlit UI (Patient Browser page)
  ├─ GET hapi-fhir:8080/fhir/Patient?name=<query>   (direct FHIR call)
  │    → display patient cards
  │
  │  User clicks "De-identify $everything"
  ├─ POST /process/everything   (anonymizer API)
  │    → anonymizer fetches Patient/$everything from HAPI FHIR
  │    → applies all rules to every resource in the Bundle
  │    → streams NDJSON back to the UI
  └─ UI displays + offers download of de-identified NDJSON
```

### Flow 3: Batch Processing (NDJSON stream)

```
Browser → Upload .ndjson file (one FHIR resource per line)
  │
  ▼
Streamlit UI (Batch page) or curl POST /process/ndjson
  │
  ▼
Anonymizer
  ├─ Parse NDJSON line by line
  ├─ For each resource: apply all rules
  ├─ Stream de-identified NDJSON back (one line per resource)
  └─ Client receives streamed results with progress updates
```

### Flow 4: Risk Assessment

```
Browser → Upload de-identified NDJSON (Patient + optional Condition resources)
  │
  ▼
Streamlit UI (Risk Assessment page) or curl POST /analyse/risk
  │
  ▼
Anonymizer (analytics/risk.py)
  ├─ Parse NDJSON
  ├─ Extract quasi-identifiers from Patient resources:
  │     gender, birth_year (first 4 chars), zip_prefix (first 3 chars)
  ├─ Build Condition code map (correlate by subject.reference)
  ├─ Compute k-anonymity:
  │     Group patients by QI tuple → Counter
  │     min_k = smallest group size
  │     prosecutor_risk = 1 / min_k
  │     journalist_risk = max(1/k_i) across all groups
  │     marketer_risk = num_groups / total_records
  │     risk_level: low (k>=5) / medium (k>=3) / high (k>=2) / critical (k=1)
  ├─ Compute l-diversity: distinct Condition codes per group
  └─ Return risk report JSON
  │
  ▼
UI: metric cards, bar chart, per-group table, recommendations, JSON download
```

### Flow 5: Round-Trip (fetch -> de-identify -> upload)

```
Source FHIR Server              Anonymizer                  Target FHIR Server
       │                            │                              │
       │◄── GET /Patient?_count=200─┤                              │
       │─── Bundle ────────────────►│                              │
       │                            │─── apply all rules ─────────►│
       │                            │                      POST /Patient
       │◄── GET /Observation ... ───│                              │
       │─── Bundle ────────────────►│─── apply all rules ─────────►│
       │                            │                      POST /Observation
       │                        stream status lines to client
```

### Flow 6: Synthetic Data Generation

```
POST /generate/synthetic?count=200
  │  De-identified NDJSON as input
  ▼
Anonymizer (analytics/synthetic.py)
  ├─ Sample statistical distributions from input:
  │     gender ratio, birth year spread (±2-year jitter), 3-digit zip distribution
  ├─ Generate synthetic Patient resources
  ├─ Tag each with meta.tag[code=SYN]
  └─ Stream NDJSON output
```

---

## 5. Authentication and Authorization

### Identity Provider: Keycloak

Keycloak is the central OIDC provider. All authenticated access flows through Keycloak.

```
                         ┌────────────────────────┐
                         │       Keycloak          │
                         │   Realm: medanon        │
                         │                         │
                         │  Roles:                 │
                         │    viewer < analyst     │
                         │      < admin            │
                         │                         │
                         │  Clients:               │
                         │    medanon-ui (PKCE)    │
                         │    medanon-api (confid.) │
                         │    medanon-anonymizer    │
                         │    fhir-proxy-oauth      │
                         │    gpas-proxy-oauth      │
                         └───────────┬────────────┘
                ┌───────────────┬────┴────┬───────────────┐
                ▼               ▼         ▼               ▼
          Streamlit UI    Anonymizer  FHIR Proxy    gPAS Proxy
          (PKCE S256)     (JWT RS256) (oauth2-proxy) (oauth2-proxy)
```

### Auth Resolution Chain (Anonymizer API)

The anonymizer resolves authentication in this order:

1. **JWT (Keycloak):** If `KEYCLOAK_URL` is configured and `Authorization: Bearer <token>` is present, validate the JWT via RS256 JWKS. Extract roles from `realm_access.roles`.
2. **Legacy API key:** If `MEDANON_API_KEY` is configured and `X-API-Key` header matches, grant `admin` role.
3. **No credentials but Keycloak required:** Return HTTP 401.
4. **No auth configured:** Grant anonymous admin access (backward compatibility).

### Role-Based Access Control

Roles are hierarchical: `admin` inherits `analyst` which inherits `viewer`.

| Role | Endpoints |
|---|---|
| `viewer` | `/health`, `/ready`, `/metrics`, `/docs` |
| `analyst` | All processing endpoints, `/analyse/risk`, `/generate/synthetic` |
| `admin` | `/process/and-upload`, `/process/round-trip` |

Open paths (no auth): `/health`, `/ready`, `/metrics`, `/docs`, `/openapi.json`, `/redoc`.

### Audit Logging

Every authenticated request is logged to `/output/audit.log` as structured JSON:

```json
{"ts":"2026-03-20T10:15:00Z","method":"POST","path":"/process","status":200,"request_id":"abc-123","subject":"analyst@medanon.local","auth_method":"jwt"}
```

Rotation: 10 MB per file, 5 backups (50 MB total cap). PHI and raw tokens are never logged.

---

## 6. Rule Engine

Rules are defined in YAML and evaluated in order. All matching rules apply to a resource.

```yaml
rules:
  - name: "redact patient name"
    match: "Patient.name"           # FHIRPath expression
    action: "redact"
    params: {}                      # action-specific parameters

  - name: "hash all IDs"
    match: "*.id"                   # wildcard matches all resource types
    action: "cryptohash"
    params:
      hash_type: sha3_256
      secret_key_env: MEDANON_HASH_KEY
```

### Evaluation Process

```
For each resource:
  processed_paths = {}
  For each rule in config (ordered):
    candidates = expand FHIRPath wildcards (*.id → Patient.id, Observation.id, ...)
    For each candidate:
      matched_elements = evaluate FHIRPath against resource (cached compilation)
      For each matched element:
        if (path, action_category) not in processed_paths:
          dispatch to action handler
          processed_paths.add((path, action_category))
```

### Reference Rewriting

When `rewrite_references: true` is set in the config:

1. After all rules have been applied, collect the mapping of `{old_id: new_id}` for every resource in the Bundle.
2. Walk all `reference` fields in the Bundle and replace any `ResourceType/old_id` with `ResourceType/new_id`.
3. If `rewrite_text_ids: true`, also scan free-text fields for old IDs and replace them.

This preserves referential integrity across Bundles after pseudonymization.

---

## 7. Text Scrubbing Pipeline

Free-text fields (clinical notes, narrative divs, comments) undergo a dual-pass scrubbing pipeline:

```
Raw text node (*.text.div, *.note.text, *.comment)
  │
  ▼
Pass 1: scrub_text (regex-based, ReDoS-safe patterns)
  ├─ SSN patterns     → [[SSN_1]]
  ├─ Phone numbers    → [[PHONE_1]]
  ├─ Email addresses  → [[EMAIL_1]]
  ├─ Date patterns    → [[DATE_1]]
  ├─ MRN patterns     → [[MRN_1]]
  └─ Name extraction  → [[NAME_1]] (resource-specific names)
  │
  ▼
Pass 2: nlp_detect (Microsoft Presidio + spaCy en_core_web_lg)
  ├─ PERSON entities  → [[PERSON_2]]
  ├─ LOCATION         → [[LOCATION_3]]
  ├─ AGE              → [[AGE_4]]
  └─ GDPR Art. 9      → [[MEDICAL_5]], etc.
  │
  ▼
Tokenized output (no PHI remains)
```

Token numbering is controlled by `mapping_scope`:
- `resource` (default): tokens reset per resource
- `bundle`: consistent numbering across a Bundle
- `global_run`: tokens persist for the entire processing run

---

## 8. Config Profile Selection

The system auto-selects a config profile based on environment variables:

```
Is GPAS_URL set in environment?
          │
    ┌─────┴──────┐
   YES            NO
    │              │
    ▼              ▼
config_gpas.yaml  config.yaml
    │                    │
    ▼                    ▼
gpas_pseudonymize    cryptohash
    │                    │
    ▼                    ▼
gPAS stores mapping  HMAC-SHA3-256(value, key)
→ reversible         → one-way
```

Five bundled profiles:

| Profile | Use Case | ID Strategy | Date Strategy |
|---|---|---|---|
| `config.yaml` | Local dev, no external services | cryptohash (SHA3-256) | regex scrubbing |
| `config_gpas.yaml` | Production with gPAS | gpas_pseudonymize (reversible) | generalize + NLP |
| `config_gdpr_eu.yaml` | GDPR Art. 4(5) compliance | HMAC pseudonymization | year-only |
| `config_hipaa_safe_harbor.yaml` | HIPAA 45 CFR SS 164.514(b) | cryptohash | year-only, all 18 PHI categories |
| `config_research_pseudonymous.yaml` | IRB-grade research | cryptohash (longitudinal) | year-month (finer granularity) |

If gPAS is configured but unreachable, the engine raises an error. There is no silent fallback.

---

## 9. Re-identification Risk Model

Two complementary mechanisms protect against re-identification:

| Attack Vector | Mitigation | Mechanism |
|---|---|---|
| Lookup by known Patient ID or MRN | gPAS pseudonymization or cryptohash | Replaces direct identifiers |
| Demographic linkage (age + zip + gender + external data) | k-anonymity on quasi-identifiers | Groups records; measures group sizes |
| Disease fingerprinting (rare diagnosis + birth year = unique person) | l-diversity on Condition codes | Measures diversity of sensitive attributes per group |

### k-anonymity Computation

1. Extract quasi-identifiers from Patient resources: `gender`, `birth_year` (first 4 chars of birthDate), `zip_prefix` (first 3 chars of postalCode).
2. Group patients by their QI tuple.
3. Compute:
   - `min_k` = smallest group size
   - `prosecutor_risk` = 1 / min_k (targeted attack against a known individual)
   - `journalist_risk` = max(1/k_i) across groups (easiest-to-identify person)
   - `marketer_risk` = num_groups / total_records (random draw success rate)

### Risk Levels

| Level | Condition | Meaning |
|---|---|---|
| `low` | k >= 5 | Meets basic k-anonymity standards |
| `medium` | k = 3 or 4 | Consider further generalization |
| `high` | k = 2 | Significant re-identification risk |
| `critical` | k = 1 | Unique records exist; immediate action required |

---

## 10. Network Topology

### Docker Compose (Production)

```
Host machine
├── 127.0.0.1:8000  → anonymizer      (FastAPI REST API)
├── 127.0.0.1:8501  → ui              (Streamlit browser UI)
├── 127.0.0.1:8180  → keycloak        (OIDC identity provider)
├── 127.0.0.1:4180  → fhir-proxy      (OAuth2-proxy → HAPI FHIR)
├── 127.0.0.1:8082  → gpas-proxy      (OAuth2-proxy → gPAS)
│
Docker bridge: fhir-net (internal)
├── anonymizer:8000
├── ui:8501
├── fhir-server:8080     (no host port)
├── gpas:8080            (no host port)
├── gpas-db:3306         (no host port)
├── keycloak:8080
├── fhir-proxy:4180
└── gpas-proxy:4180
```

HAPI FHIR and gPAS are intentionally not exposed on host ports. Browser users access them through the OAuth2 proxies which enforce Keycloak authentication.

For remote access, place a reverse proxy (nginx, Caddy, Traefik) in front and terminate TLS there.

### Kubernetes (Helm)

```
Ingress Controller
├── /           → anonymizer Service (ClusterIP :8000)
├── /fhir       → fhir-server Service (ClusterIP :8080)
├── /gpas-web   → gpas Service (ClusterIP :8080)
└── /ttp-fhir   → gpas Service (ClusterIP :8080)
```

---

## 11. Security Model

| Layer | Mechanism | Default Status |
|---|---|---|
| API authentication | Keycloak OIDC JWT (RS256) + legacy API key fallback | Configurable |
| Role-based access | Three-tier RBAC via Keycloak realm roles | Enabled when Keycloak configured |
| Audit logging | Structured JSON with rotation (`/output/audit.log`) | Enabled |
| Browser auth for FHIR | oauth2-proxy (`fhir-proxy`) gating HAPI FHIR | Enabled |
| Browser auth for gPAS | oauth2-proxy (`gpas-proxy`) gating gPAS | Enabled |
| gPAS FHIR API | gRAS basic auth | Enabled |
| Network isolation | All ports bound to `127.0.0.1`; gpas-db internal only | Enabled |
| XML parsing | defusedxml for XXE protection | Enabled |
| Body size limit | Middleware enforces `MEDANON_MAX_BODY_BYTES` (both Content-Length and chunked) | Enabled (10 MB default) |
| Container hardening | Non-root user, `no-new-privileges`, all capabilities dropped, read-only filesystem | Enabled |
| CSPRNG | `secrets.randbelow()` for all perturbation offsets | Enabled |
| Path traversal guard | `_validate_key_path()` restricts RSA key access to allowlisted directories | Enabled |
| SSRF protection | Private IP blocklist + same-origin check on FHIR pagination | Enabled |
| Regex patterns | ReDoS-safe (no nested quantifiers, bounded lengths) | Enabled |
| PHI in logs | Processing errors logged without resource content (`exc_info=False`) | Enabled |
| Cryptohash key | WARNING logged when `MEDANON_HASH_KEY` is unset (plain SHA3-256, rainbow-table vulnerable) | Enabled |
| gPAS circuit breaker | Fail-fast when gPAS is unhealthy; auto-recovery probe | Configurable |

---

## 12. Source Layout

### Anonymizer (`services/anonymizer/src/`)

```
api/
  main.py              FastAPI entry point: 14 endpoints, middleware, body-size guard,
                       SSRF validation, async processing via asyncio.to_thread()
  auth.py              Keycloak OIDC + legacy API-key dual-auth; JWKS cache,
                       RS256 JWT validation, RBAC, service-account token, audit logging

cli/
  main.py              CLI: process / fetch / everything / push subcommands

pipeline/
  config.py            YAML loader with ${VAR:-default} env interpolation, rule validation
  processor.py         Rule engine: FHIRPath match → action dispatch, reference rewriting,
                       FHIRPath expression caching, transformation manifest
  io_formats.py        Parse JSON / NDJSON / XML (defusedxml); serialize output
  deidentify.py        Action dispatcher for de-identification flows

actions/
  redact.py            Delete matched fields or set to fixed value
  cryptohash.py        HMAC-SHA3-256 (keyed) or plain SHA3-256
  encrypt.py           RSA-OAEP encrypt
  decrypt.py           RSA-OAEP decrypt
  perturb.py           CSPRNG date/number perturbation
  substitute.py        Replace with fixed literal
  generalize.py        Reduce precision (dates, zips, numbers, ages)
  scrub_text.py        Regex PHI tokenization in free text and XHTML

analytics/
  risk.py              k-anonymity, l-diversity, prosecutor/journalist/marketer risk
  synthetic.py         Synthetic FHIR Patient generation from statistical distributions

integrations/
  gpas/
    client.py          gPAS HTTP client: retry, circuit breaker, thread-safe LRU cache
    dispatcher.py      Adapter bridge: routes pipeline calls to client
  nlp/
    detector.py        Presidio NER: names, locations, GDPR Art. 9 entities
  fhir/
    client.py          FHIR REST client: urllib3 connection pooling, paginated fetch,
                       $everything, upload; with retry and exponential backoff

utils/
  crypto.py            RSA key caching (mtime-invalidated), bounded_random (CSPRNG),
                       path traversal guard
  fhirpath.py          FHIRPath node traversal helpers
  logging.py           Request ID propagation
  metrics.py           Prometheus counters + histograms
```

### UI Client (`client/`)

```
app.py                           Home page + sidebar
pages/
  1_Patient_Browser.py           Patient search + $everything de-identification
  2_Process_Resource.py          Inline de-identification, side-by-side diff
  3_Batch.py                     Bulk NDJSON/JSON/XML upload and download
  4_Status.py                    Health monitoring dashboard
  5_Condition_Browser.py         Condition/diagnosis search
  6_Risk_Assessment.py           k-anonymity + l-diversity + risk metrics
  7_Synthetic_Data.py            Synthetic patient generation
utils/
  api.py                         HTTP client → anonymizer API
  auth.py                        Keycloak OIDC with PKCE (streamlit-keycloak)
  fhir.py                        HTTP client → HAPI FHIR
  sidebar.py                     Shared sidebar component
```

### Config Profiles (`services/anonymizer/config/`)

```
config.yaml                      Minimal: cryptohash + regex/NLP scrubbing, no gPAS
config_gpas.yaml                 Production: gPAS pseudonymization + generalization + NLP
config_gdpr_eu.yaml              GDPR Art. 4/5/25/32/89 HMAC profile
config_hipaa_safe_harbor.yaml    HIPAA Safe Harbor: all 18 PHI categories
config_research_pseudonymous.yaml IRB-grade research: year-month dates, longitudinal linkage
```

---

## 13. Technology Stack

| Layer | Technology | Version |
|---|---|---|
| UI framework | Streamlit | >= 1.35 |
| UI auth | streamlit-keycloak | latest |
| Anonymizer runtime | Python | 3.12 |
| Web framework | FastAPI + uvicorn | pinned in requirements.txt |
| FHIRPath engine | fhirpathpy | 0.1.0 |
| NLP entity detection | Microsoft Presidio + spaCy | >= 2.2 / >= 3.8 |
| spaCy model | en_core_web_lg | latest (~560 MB) |
| XML parsing | defusedxml | 0.7.1 |
| Crypto | pycryptodome | 3.23.0 |
| HTTP connection pooling | urllib3 | 2.3.0 |
| FHIR server | HAPI FHIR JPA Server | v7.6.0 |
| TTP service | gPAS | 2025.2.0 |
| TTP app server | WildFly | 38 |
| TTP database | MySQL | 8.0 |
| Identity provider | Keycloak | 24.0.5 |
| Auth proxies | oauth2-proxy | v7.6.0 |
| Container runtime | Docker + Docker Compose | v2 |
| Kubernetes packaging | Helm | v3 |
| Linting / formatting | ruff | latest |
| Testing | pytest | latest |

---

## 14. Deployment Modes

### Docker Compose (local / single-node)

All eight containers on one host. Hot-reload dev mode via `make dev`.

| Service | Host Port | Container Port |
|---|---|---|
| Anonymizer API | 8000 | 8000 |
| Streamlit UI | 8501 | 8501 |
| Keycloak | 8180 | 8080 |
| FHIR Proxy | 4180 | 4180 |
| gPAS Proxy | 8082 | 4180 |
| HAPI FHIR | (internal) | 8080 |
| gPAS | (internal) | 8080 |
| MySQL | (internal) | 3306 |

### Kubernetes (Helm)

Umbrella chart at `helm/medanon/` with sub-charts for anonymizer, HAPI FHIR, and gPAS. Optional Keycloak and proxy sub-charts.

| Aspect | Docker Compose | Helm / Kubernetes |
|---|---|---|
| Images | Built locally | Pushed to a container registry |
| Config | `.env` file | `values.yaml` + Kubernetes Secrets |
| Persistence | Named Docker volumes | PersistentVolumeClaim (StatefulSet) |
| Health checks | Docker healthcheck | liveness + readiness probes |
| Startup order | `depends_on` | initContainers (TCP check) |
| Scaling | Single instance | HPA-ready |
| Monitoring | Prometheus scrape on `/metrics` | Prometheus annotations on all services |

See [DEPLOYMENT.md](DEPLOYMENT.md) for step-by-step instructions.
