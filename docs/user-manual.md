# MedAnon User Manual

## Table of Contents

1. [Getting Started](#1-getting-started)
2. [Streamlit UI Guide](#2-streamlit-ui-guide)
3. [REST API Reference](#3-rest-api-reference)
4. [CLI Reference](#4-cli-reference)
5. [Config File Format](#5-config-file-format)
6. [Actions Reference](#6-actions-reference)
7. [Dynamic Rule Overrides](#7-dynamic-rule-overrides)
8. [Environment Variables](#8-environment-variables)
9. [gPAS Domain Setup](#9-gpas-domain-setup)
10. [Input and Output Formats](#10-input-and-output-formats)
11. [Authentication](#11-authentication)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Getting Started

### Starting the Stack (Docker Compose)

```bash
cp .env.example .env      # fill in your secrets (see DEPLOYMENT.md)
make build                 # build anonymizer + UI images
make up                    # start all eight services
```

Wait approximately 90 seconds for all services to become healthy, then verify:

```bash
curl http://localhost:8000/health    # {"status":"ok"}
curl http://localhost:8000/ready     # {"ready": true}
```

### Service URLs

| Service | URL | Purpose |
|---|---|---|
| Streamlit UI | `http://localhost:8501` | Browser-based interface |
| Anonymizer API | `http://localhost:8000` | REST API |
| Swagger UI | `http://localhost:8000/docs` | Interactive API explorer |
| FHIR Server | `http://localhost:4180/fhir` | HAPI FHIR (through auth proxy) |
| gPAS Web UI | `http://localhost:8082/gpas-web/` | Pseudonym management (through auth proxy) |
| Keycloak Admin | `http://localhost:8180` | Identity provider admin console |

### Local Development (no Docker)

```bash
make setup                                     # create venv, install deps, download spaCy model
cd services/anonymizer
uvicorn src.api.main:app --reload --port 8000  # start API only
```

This uses `config.yaml` (cryptohash mode, no gPAS or FHIR server required).

---

## 2. Streamlit UI Guide

Open `http://localhost:8501` in your browser. When Keycloak is configured, you will be prompted to log in. The sidebar shows the current anonymizer health status and navigation links.

### 2.1 Patient Browser

Search patients stored in the HAPI FHIR server and de-identify all linked resources.

1. Enter a patient name (partial match supported) and click **Search**.
2. Results appear as expandable cards showing patient demographics.
3. Click any card to see full patient details (identifiers, addresses, telecom).
4. Click **De-identify $everything** to fetch and de-identify all resources linked to that patient (Observations, Conditions, Medications, etc.).
5. The de-identified result is displayed as NDJSON. Click **Download** to save.

### 2.2 Condition Browser

Find patients by diagnosis, then de-identify their records.

1. Enter an illness name (e.g., `diabetes`) or SNOMED CT code (e.g., `73211009`).
2. Click **Search**. Matching patient cards appear.
3. Click any card to expand, then click **De-identify** to run the full pipeline.

### 2.3 Process Resource

De-identify a single FHIR resource by pasting it directly.

1. Click **Load sample Patient** to pre-fill with a realistic example, or paste your own FHIR JSON, NDJSON, or XML.
2. Select an output format: `json`, `ndjson`, or `xml`.
3. Click **De-identify**.
4. The original input and de-identified output appear side-by-side for comparison.
5. Click **Download** to save the result.

### 2.4 Batch Processing

Process an entire file of FHIR resources at once.

1. Click **Browse files** and select a file:
   - `.ndjson` (one FHIR resource per line)
   - `.json` (FHIR Bundle)
   - `.xml` (FHIR XML Bundle or single resource)
2. Maximum file size: 10 MB (configurable via `MEDANON_MAX_BODY_BYTES`).
3. Click **De-identify batch**. A progress bar shows per-resource status.
4. Download the de-identified result file.

### 2.5 Status Dashboard

Live health monitoring for all backend services. Auto-refreshes every 30 seconds.

Displays:
- Anonymizer API status
- HAPI FHIR server status
- gPAS service status
- NLP engine status (when `MEDANON_NLP_MODEL` is configured)

### 2.6 Risk Assessment

Measure residual re-identification risk of de-identified data.

1. Upload an NDJSON file containing de-identified Patient resources (optionally with Condition resources for l-diversity analysis).
2. Click **Analyse Risk**.
3. View results:
   - **k-anonymity (min k)**: the smallest equivalence class size
   - **Prosecutor risk (1/k)**: probability of re-identifying a targeted individual
   - **Journalist risk**: risk for the easiest-to-identify patient
   - **Marketer risk**: expected risk for a randomly drawn record
   - **l-diversity**: distinct Condition codes per group (requires Condition resources)
   - **Bar chart**: group size distribution
   - **Per-group breakdown table** (expandable)
   - **Recommendations** derived from the metrics
4. Click **Download** to save the full JSON report.

**Important**: Run this step *after* de-identification to measure the risk that remains in the output data.

### 2.7 Synthetic Data

Generate synthetic FHIR Patient resources that preserve the statistical distributions of a de-identified dataset without copying any individual record.

1. Upload a de-identified NDJSON file (use Batch Processing first to de-identify real data).
2. Set the number of synthetic patients to generate (1 to 10,000).
3. Optionally set a random seed for reproducible output.
4. Click **Generate**. Each synthetic Patient is tagged with `meta.tag[code=SYN]`.
5. Download the synthetic NDJSON.

---

## 3. REST API Reference

Base URL: `http://localhost:8000`

All endpoints accept and return `application/json` by default. Streaming endpoints return `application/x-ndjson`.

### Authentication

When Keycloak is configured (`KEYCLOAK_URL` set), all endpoints except health/metrics/docs require a JWT Bearer token or legacy API key.

```bash
# JWT Bearer token (Keycloak)
curl -H "Authorization: Bearer <jwt>" http://localhost:8000/process ...

# Legacy API key
curl -H "X-API-Key: <key>" http://localhost:8000/process ...
```

### Health and Readiness

#### `GET /health`

Liveness probe. Always returns `{"status": "ok"}` if the process is running.

#### `GET /ready`

Readiness probe. Checks connectivity to gPAS, FHIR server, and NLP engine.

```json
{"ready": true, "checks": {"gpas": "ok", "fhir": "ok", "nlp": "ok"}}
```

The `checks` object is only visible to authenticated users.

#### `GET /metrics`

Prometheus-format metrics (request counts, latencies, gPAS call stats, FHIR call stats).

### Processing Endpoints

#### `POST /process`

De-identify a single FHIR resource or Bundle. Requires role: `analyst`.

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Patient","id":"p-001","name":[{"family":"Smith"}],"birthDate":"1980-05-12"}'
```

#### `POST /process/raw`

Multi-format endpoint. Accepts JSON, NDJSON, or XML; returns any format.

| Parameter | Values | Default |
|---|---|---|
| `input_format` | `auto`, `json`, `ndjson`, `xml` | `auto` |
| `output_format` | `json`, `ndjson`, `xml` | `json` |

```bash
# XML in, XML out
curl -X POST "http://localhost:8000/process/raw?output_format=xml" \
  -H "Content-Type: application/xml" --data-binary @patient.xml

# NDJSON in, JSON array out
curl -X POST "http://localhost:8000/process/raw?input_format=ndjson&output_format=json" \
  -H "Content-Type: application/x-ndjson" --data-binary @patients.ndjson
```

#### `POST /process/ndjson`

Stream-process NDJSON (one FHIR resource per line). Lines starting with `//` are skipped as comments.

```bash
curl -X POST http://localhost:8000/process/ndjson \
  -H "Content-Type: application/x-ndjson" --data-binary @patients.ndjson
```

Returns streaming NDJSON.

#### `POST /process/batch`

Unified batch endpoint. Accepts NDJSON, JSON Bundle, or XML. Auto-detects format from `Content-Type`.

| Content-Type | Format |
|---|---|
| `application/x-ndjson` | NDJSON |
| `application/json` | JSON Bundle |
| `application/fhir+xml` | FHIR XML |

```bash
curl -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/x-ndjson" --data-binary @patients.ndjson
```

Returns streaming NDJSON.

#### `POST /process/from-server`

Fetch resources from a FHIR server, de-identify, return NDJSON.

```bash
curl -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{
    "server_url": "http://localhost:4180/fhir",
    "resource_types": ["Patient", "Observation", "Condition"],
    "params": {"_count": 200},
    "token": "optional-bearer-token"
  }'
```

Omit `resource_types` to auto-discover from the server's `/metadata`.

#### `POST /process/everything`

Fetch all resources for a subject via FHIR `$everything`, then de-identify.

```bash
curl -X POST http://localhost:8000/process/everything \
  -H "Content-Type: application/json" \
  -d '{"server_url":"http://localhost:4180/fhir","resource_type":"Patient","resource_id":"p-001"}'
```

#### `POST /process/and-upload`

De-identify a resource and upload to a target FHIR server. Requires role: `admin`.

```bash
curl -X POST http://localhost:8000/process/and-upload \
  -H "Content-Type: application/json" \
  -d '{
    "target_server_url": "http://target-fhir:8080/fhir",
    "resource": {"resourceType":"Patient","id":"p-001","name":[{"family":"Test"}]}
  }'
```

Returns: `{"uploaded": 1, "errors": 0, "results": [...]}`

#### `POST /process/round-trip`

Fetch from source, de-identify, upload to target in one call. Requires role: `admin`.

```bash
curl -X POST http://localhost:8000/process/round-trip \
  -H "Content-Type: application/json" \
  -d '{
    "source_server_url": "http://source-fhir:8080/fhir",
    "target_server_url": "http://target-fhir:8080/fhir",
    "resource_types": ["Patient", "Observation"]
  }'
```

Returns streaming NDJSON with one status line per resource.

### Analytics Endpoints

#### `POST /analyse/risk`

Compute re-identification risk metrics on de-identified Patient resources. Requires role: `analyst`.

Input: NDJSON body (Patient resources for k-anonymity; optionally Condition resources for l-diversity).

```bash
curl -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" --data-binary @deidentified.ndjson
```

Response:

```json
{
  "summary": {
    "total_records": 42,
    "total_groups": 8,
    "min_k": 3,
    "max_k": 15,
    "mean_k": 5.25,
    "prosecutor_risk": 0.333,
    "journalist_risk": 0.333,
    "marketer_risk": 0.19,
    "risk_level": "medium",
    "qi_fields": ["gender", "birth_year", "zip_prefix_3"],
    "singleton_groups": 0,
    "records_with_missing_qi": 3
  },
  "l_diversity": {"computed": true, "min_l": 2, "max_l": 6, "violations": 0},
  "groups": [...],
  "warnings": [],
  "meta": {"computed_at": "2026-03-20T14:22:00Z", "input_lines": 42}
}
```

| Risk Level | Condition | Meaning |
|---|---|---|
| `low` | k >= 5 | Meets basic k-anonymity standards |
| `medium` | k = 3 or 4 | Consider further generalization |
| `high` | k = 2 | Significant re-identification risk |
| `critical` | k = 1 | Unique records; immediate action required |

#### `POST /generate/synthetic`

Generate synthetic FHIR Patient resources. Requires role: `analyst`.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `count` | int | 100 | Number of synthetic patients (1 to 10,000) |
| `seed` | int | random | Random seed for reproducible output |

Input: De-identified NDJSON.

```bash
curl -X POST "http://localhost:8000/generate/synthetic?count=200&seed=42" \
  -H "Content-Type: application/x-ndjson" --data-binary @deidentified.ndjson
```

Each output Patient is tagged with `meta.tag[code=SYN]`. Preserves: gender ratio, birth year spread, 3-digit zip distribution. Does NOT preserve: clinical data, exact ages, identifiers.

### Error Codes

| HTTP | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request (unknown action, invalid Content-Length) |
| `401` | Authentication required |
| `403` | Insufficient role |
| `413` | Body exceeds size limit (default 10 MB) |
| `422` | Unprocessable (invalid JSON, missing required field, SSRF blocked) |
| `500` | Unexpected processing error |
| `502` | Source FHIR server unreachable |

### Transformation Manifest

When `MEDANON_MANIFEST_ENABLED=true`, every processed resource includes a `meta.tag` entry documenting which rules fired:

```json
{
  "meta": {
    "tag": [{
      "system": "https://medanon.local/transformation-manifest",
      "code": "transformation-manifest",
      "display": "[{\"rule\":\"hash_patient_id\",\"action\":\"cryptohash\",\"path\":\"Patient.id\"}]"
    }]
  }
}
```

---

## 4. CLI Reference

Run all CLI commands from the `services/anonymizer/` directory:

```bash
cd services/anonymizer
python3 -m cli.main <subcommand> [options]
```

### `process` -- Transform a Local File

```bash
# JSON in, JSON out
python3 -m cli.main process patient.json output.json --config config/config_gpas.yaml

# NDJSON in, NDJSON out
python3 -m cli.main process patients.ndjson output.ndjson --config config/config.yaml

# XML in, XML out
python3 -m cli.main process patient.xml output.xml --config config/config.yaml
```

| Flag | Description |
|---|---|
| `--config`, `-c` | YAML config file path |
| `--input-format` | Force input format (`json`, `ndjson`, `xml`) |
| `--output-format` | Force output format |
| `--strip-line-prefix` | Strip prefix from each NDJSON line (default `//`) |

### `fetch` -- Fetch from FHIR Server and De-identify

```bash
python3 -m cli.main fetch \
  --server http://localhost:4180/fhir \
  --resource-type Patient,Observation \
  --output output/patients.ndjson \
  --config config/config_gpas.yaml
```

| Flag | Description |
|---|---|
| `--server` | FHIR base URL (or `FHIR_SOURCE_URL` env var) |
| `--resource-type` | Comma-separated types; omit to auto-discover |
| `--output` | Output NDJSON file path |
| `--config`, `-c` | YAML config file |
| `--count` | Page size (`_count`), default 200 |
| `--since` | Only resources updated after this date |
| `--token` | Bearer token for FHIR auth |
| `--discover-only` | Print resource types from `/metadata` and exit |

### `everything` -- Fetch $everything for One Resource

```bash
python3 -m cli.main everything \
  --server http://localhost:4180/fhir \
  --resource-type Patient --id p-001 \
  --output output/patient-p001.ndjson \
  --config config/config_gpas.yaml
```

### `push` -- Upload a Local File to a FHIR Server

```bash
python3 -m cli.main push \
  --server http://localhost:4180/fhir \
  --input output/patients.ndjson
```

### Batch Scripts

```bash
# Fetch all resources from HAPI FHIR, de-identify, write NDJSON
HAPI_URL=http://localhost:4180/fhir bash scripts/batch_fetch.sh

# Process local NDJSON + generate analytics report
bash scripts/batch_process.sh

# Import Synthea test data into HAPI FHIR
bash scripts/import_testbase.sh

# Initialize gPAS domains
make init-domains
```

---

## 5. Config File Format

Configuration files are YAML. Environment variables are interpolated using `${VAR:-default}` syntax.

```yaml
general:
  appname: MyProject
  rewrite_references: true    # rewrite FHIR References after ID changes
  rewrite_text_ids: true      # replace original IDs in free-text fields

rules:
  - name: "pseudonymize patient ID"
    match: "Patient.id"               # FHIRPath expression
    action: gpas_pseudonymize
    params:
      gpas_url: ${GPAS_URL}
      gpas_domain: ${GPAS_DOMAIN}
      gpas_operation: ${GPAS_OPERATION:-pseudonymizeAllowCreate}

  - match: "Patient.name"
    action: redact

  - match: "Patient.birthDate"
    action: generalize
    params:
      strategy: date_year
```

Rules are evaluated in order. All matching rules apply to a resource.

### FHIRPath Wildcards

| Pattern | Matches |
|---|---|
| `Patient.id` | `id` on Patient resources only |
| `*.id` | `id` on every resource type |
| `*.identifier.value` | `identifier.value` on every resource |
| `*.meta.lastUpdated` | `lastUpdated` inside `meta` on every resource |

### Config Auto-Selection

The API auto-selects a config profile based on environment:
- `GPAS_URL` set -> `config_gpas.yaml`
- `GPAS_URL` not set -> `config.yaml`

Override in the CLI with `--config <file>`.

### Bundled Profiles

| Profile | Use Case |
|---|---|
| `config.yaml` | Local dev/testing, no external services |
| `config_gpas.yaml` | Production with reversible gPAS pseudonymization |
| `config_gdpr_eu.yaml` | GDPR Art. 4(5) HMAC pseudonymization |
| `config_hipaa_safe_harbor.yaml` | HIPAA Safe Harbor (all 18 PHI categories) |
| `config_research_pseudonymous.yaml` | IRB-grade research (year-month dates) |

---

## 6. Actions Reference

### `redact`

Removes the matched field entirely, or replaces it with a fixed value.

```yaml
- match: Patient.name
  action: redact
  params:
    replacement: "REDACTED"    # optional: set a value instead of deleting
```

### `cryptohash`

One-way SHA3-256 hash. When `MEDANON_HASH_KEY` is set, uses HMAC-SHA3-256 (production-safe). Without the key, falls back to plain SHA3-256 (warns at startup).

```yaml
- match: "*.id"
  action: cryptohash
  params:
    hash_type: sha3_256
    secret_key_env: MEDANON_HASH_KEY
```

### `gpas_pseudonymize`

Reversible pseudonymization via the gPAS TTP-FHIR gateway.

```yaml
- match: "*.id"
  action: gpas_pseudonymize
  params:
    gpas_url: ${GPAS_URL}
    gpas_domain: ${GPAS_DOMAIN}
    gpas_operation: pseudonymizeAllowCreate    # or: pseudonymize, dePseudonymize
    gpas_timeout_sec: 30
```

- `pseudonymizeAllowCreate`: creates the domain entry if absent (recommended)
- `pseudonymize`: fails on unknown IDs
- `dePseudonymize`: reverses a pseudonym to the original value

### `gpas_depseudonymize`

Reverses a gPAS pseudonym to the original value.

```yaml
- match: "Patient.id"
  action: gpas_depseudonymize
  params:
    gpas_url: ${GPAS_URL}
    gpas_domain: ${GPAS_DOMAIN}
```

### `encrypt`

RSA-OAEP encrypt. Reversible with the corresponding private key.

```yaml
- match: Patient.identifier.value
  action: encrypt
  params:
    algorithm: RSA
    public_key: ${MEDANON_RSA_PUBLIC_KEY}    # path to PEM file
```

### `decrypt`

RSA-OAEP decrypt.

```yaml
- match: Patient.identifier.value
  action: decrypt
  params:
    algorithm: RSA
    private_key: ${MEDANON_RSA_PRIVATE_KEY}
```

### `generalize`

Reduces precision to lower re-identification risk.

| Strategy | Input | Output |
|---|---|---|
| `date_year` | `"1980-05-12"` | `"1980"` |
| `date_year_month` | `"1980-05-12"` | `"1980-05"` |
| `age_bracket` | `"1980-05-12"` | `"40-49"` |
| `number_round` | `94` (precision 10) | `90` |
| `zip_prefix` | `"10115"` (len 3) | `"101"` |
| `category` | `"M"` | configured mapping value |

```yaml
- match: Patient.birthDate
  action: generalize
  params:
    strategy: date_year

- match: Patient.address.postalCode
  action: generalize
  params:
    strategy: zip_prefix
    prefix_len: 3
```

### `perturb`

Shifts dates or numbers by a bounded CSPRNG random offset.

```yaml
- match: "*.effectiveDateTime"
  action: perturb
  params:
    max_days: 30          # shift +/- up to 30 days

- match: Observation.valueQuantity.value
  action: perturb
  params:
    max_offset: 5         # shift +/- up to 5 units
```

### `substitute`

Replaces the value with a fixed literal.

```yaml
- match: Patient.gender
  action: substitute
  params:
    value: "unknown"
```

### `scrub_text`

Regex-based PHI tokenization in free text and XHTML.

```yaml
- match: "*.text"
  action: scrub_text
  params:
    mode: html_tokenize          # html_tokenize | text | tokenize
    patterns: all                # all | ssn | phone | email | date | mrn
    mapping_scope: resource      # resource | bundle | global_run
```

Modes:
- `html_tokenize`: preserves HTML/XHTML structure, tokenizes text nodes only
- `text` / `tokenize`: plain text, replaces matches with `[[TYPE_N]]` tokens

### `nlp_detect`

NER-based detection using Microsoft Presidio and spaCy.

```yaml
- match: "*.note.text"
  action: nlp_detect
  params:
    mode: tokenize
    html: false
    threshold: 0.4               # confidence threshold 0.0-1.0
```

Detects: PERSON, LOCATION, AGE, and GDPR Article 9 special categories. Requires the `en_core_web_lg` spaCy model.

---

## 7. Dynamic Rule Overrides

The `/process` endpoint accepts a FHIR `Parameters` wrapper to override rule settings per-request:

```json
{
  "resourceType": "Parameters",
  "parameter": [
    {
      "name": "resource",
      "resource": {"resourceType": "Patient", "id": "p-001", "name": [{"family": "Smith"}]}
    },
    {
      "name": "settings",
      "part": [
        {"name": "gpas_domain", "valueString": "STUDY-42"},
        {"name": "gpas_url", "valueString": "http://gpas:8080/ttp-fhir/fhir/gpas"}
      ]
    }
  ]
}
```

Allowed settings: `gpas_url`, `gpas_domain`, `gpas_operation`, `gpas_token`, `gpas_basic_user`, `gpas_basic_pass`, `gpas_timeout_sec`, `gpas_retry_count`, `processing_errors`, `rewrite_references`, `rewrite_text_ids`. Unknown keys return HTTP 422.

---

## 8. Environment Variables

See [DEPLOYMENT.md](DEPLOYMENT.md#3-environment-variables-reference) for the complete reference table. Key variables:

| Variable | Purpose |
|---|---|
| `MEDANON_HASH_KEY` | HMAC key for cryptohash (generate: `openssl rand -hex 32`) |
| `MEDANON_API_KEY` | Legacy API key for backward-compatible auth |
| `GPAS_URL` | gPAS server URL (triggers auto-selection of `config_gpas.yaml`) |
| `GPAS_DOMAIN` | gPAS pseudonymization domain name |
| `KEYCLOAK_URL` | Keycloak server URL (enables OIDC authentication) |
| `MEDANON_MANIFEST_ENABLED` | Include transformation manifest in output (`true`/`false`) |
| `LOG_LEVEL` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## 9. gPAS Domain Setup

Before using `gpas_pseudonymize`, a domain must exist in gPAS.

### Via Web UI

1. Open `http://localhost:8082/gpas-web/` (through the gPAS proxy).
2. Log in as `admin@ths` with your configured password.
3. Navigate to **gPAS -> Domains -> New**.
4. Configure: Name = your `GPAS_DOMAIN` value (e.g., `TESTING`), Generator = `ReedSolomonLagrange`, Alphabet = `Symbol31`.
5. Save.

### Via Make Target

```bash
make init-domains    # imports the configured domain template into gPAS
```

### Important

Do not insert domains directly into MySQL. gPAS maintains an in-memory domain cache that is only populated when domains are created through the gPAS API.

### gRAS User Management

Connect to the MySQL container:

```bash
docker compose exec gpas-db mysql -u root -p
```

```sql
USE gras;
CALL createUser('newuser', 'their-password', 'Description');
CALL grantStandardRights('ths', 'gpas', 'newuser');

-- Change a password
CALL changePassword('admin', 'new-secure-password');
```

---

## 10. Input and Output Formats

### JSON

Standard FHIR JSON. A single resource object or an array.

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" -d @patient.json
```

### NDJSON

One JSON object per line. Lines starting with `//` are treated as comments and skipped.

```
{"resourceType":"Patient","id":"p-001","name":[{"family":"Smith"}]}
// this line is a comment
{"resourceType":"Observation","id":"o-001","status":"final"}
```

```bash
curl -X POST http://localhost:8000/process/ndjson \
  -H "Content-Type: application/x-ndjson" --data-binary @patients.ndjson
```

### XML

FHIR R4 XML with the `http://hl7.org/fhir` namespace. Protected against XXE attacks via `defusedxml`.

```bash
curl -X POST "http://localhost:8000/process/raw?output_format=xml" \
  -H "Content-Type: application/xml" --data-binary @patient.xml
```

---

## 11. Authentication

### Keycloak OIDC (Primary)

When `KEYCLOAK_URL` is configured, all API and UI access requires Keycloak authentication.

**UI (Streamlit):** Uses OIDC Authorization Code flow with PKCE (S256) via the `medanon-ui` public client. Users are redirected to the Keycloak login page.

**API:** Accepts `Authorization: Bearer <jwt>` with RS256-signed tokens from Keycloak. Roles are extracted from the `realm_access.roles` claim.

**Role hierarchy:**

| Role | Access |
|---|---|
| `viewer` | Health, readiness, metrics, status dashboard |
| `analyst` | All processing endpoints, risk analysis, synthetic generation |
| `admin` | Upload and round-trip endpoints (includes analyst + viewer) |

**Pre-seeded test users:**

| Username | Password | Role |
|---|---|---|
| `admin` | `admin` | admin |
| `analyst` | `analyst` | analyst |
| `viewer` | `viewer` | viewer |

Change these passwords before any non-local deployment.

### Legacy API Key (Fallback)

When `MEDANON_API_KEY` is set, requests with `X-API-Key: <key>` are granted admin access. This is intended for CI/CD pipelines and scripts that cannot perform OIDC flows.

### Open Mode

When neither `KEYCLOAK_URL` nor `MEDANON_API_KEY` is configured, the system runs in open mode with no authentication (backward compatibility).

---

## 12. Troubleshooting

### Anonymizer returns 500 on every request

```bash
docker compose logs anonymizer
```

Common causes:
- `config_gpas.yaml` active but gPAS unreachable. Test: `curl http://localhost:8082/ttp-fhir/fhir/gpas/metadata`
- Missing RSA key files. Verify: `docker compose exec anonymizer ls -la /code/keys/`
- `MEDANON_HASH_KEY` unset. A warning is logged; cryptohash still works with plain SHA3-256.

### gPAS circuit breaker is OPEN

**Symptom:** API returns "gPAS circuit breaker is OPEN"

**Cause:** gPAS has failed repeatedly (default: 5 failures in 60 seconds)

**Fix:**
1. Check gPAS health: `curl http://localhost:8082/ttp-fhir/fhir/gpas/metadata`
2. Review logs: `docker compose logs gpas`
3. Wait for automatic recovery (default: 30 seconds) or restart gPAS
4. Adjust thresholds via `GPAS_CB_*` environment variables

### gPAS returns "Unknown domain"

The pseudonymization domain does not exist in gPAS. Create it:
```bash
make init-domains
```
Or use `pseudonymizeAllowCreate` instead of `pseudonymize` in your config.

### gRAS login says "Invalid credentials"

Enter the username with the `@ths` suffix: `admin@ths`, not `admin`.

### gPAS container keeps restarting

MySQL is likely still initializing. Wait for it:

```bash
docker compose ps gpas-db                          # check health
docker compose logs gpas-db | tail -20             # check init progress
docker compose restart gpas                        # restart once gpas-db is healthy
```

### NLP readiness check failing

```bash
# Download spaCy model in the container
docker compose exec anonymizer python3 -m spacy download en_core_web_lg
# Or rebuild: make build
```

### Transformation manifest not appearing

Set `MEDANON_MANIFEST_ENABLED=true` in your `.env` file and restart.

### Risk endpoint returns "No Patient resources found"

The input NDJSON must contain `Patient` resources. Non-Patient resources are only used for l-diversity (Condition resources).

### k-anonymity shows all records with k=1

After de-identification with `config_gpas.yaml`, gender and address are fully redacted, leaving only birthDate (year only) as a quasi-identifier. If every patient has a unique birth year, k=1. Solutions:
- Use larger cohorts
- Include Condition resources for l-diversity analysis
- Apply broader date generalization

### File size limit (413 error)

Split large files into smaller chunks, or increase the limit:

```bash
# .env
MEDANON_MAX_BODY_BYTES=20971520    # 20 MB
```

### XML parse errors (422)

1. Validate: `xmllint --noout your_file.xml`
2. Set `Content-Type: application/fhir+xml`
3. Ensure the root element declares `xmlns="http://hl7.org/fhir"`

### CORS errors from browser

Add your origin to the allowlist:

```bash
# .env
MEDANON_CORS_ORIGINS=http://localhost:3000,http://myapp.example.com
```

### `fhirpathpy` import error in tests

`fhirpathpy==0.1.0` is incompatible with Python 3.13. Run affected tests inside Docker:

```bash
docker compose exec anonymizer sh -c "cd /code && python3 -m pytest tests/ -q"
```

These tests run locally without Docker:
```bash
cd services/anonymizer
python3 -m pytest tests/test_io_formats.py tests/test_config_profiles.py tests/test_api.py tests/test_auth.py -q
```

### Container health check fails (unhealthy)

```bash
docker compose ps                              # check which container is unhealthy
docker inspect --format='{{.State.Health.Status}}' <container-name>
docker compose logs <service-name> | tail -50  # check logs
```
