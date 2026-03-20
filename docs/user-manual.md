# MedAnon — User Manual

Covers all ways to use MedAnon: Streamlit UI, REST API, CLI, config authoring, all actions, dynamic overrides, risk assessment, and gPAS domain setup.

---

## Table of Contents

1. [Starting the Stack](#1-starting-the-stack)
2. [Streamlit UI](#2-streamlit-ui)
3. [REST API Reference](#3-rest-api-reference)
4. [CLI Reference](#4-cli-reference)
5. [Config File Format](#5-config-file-format)
6. [Actions Reference](#6-actions-reference)
7. [Dynamic Rule Overrides](#7-dynamic-rule-overrides)
8. [gPAS Domain Setup](#8-gpas-domain-setup)
9. [Formats](#9-formats)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. Starting the Stack

### Docker Compose (recommended)

```bash
cp .env.example .env    # fill in your secrets
make build              # build anonymizer + UI images
make up                 # start all five services
```

| Service | Port | URL |
|---|---|---|
| Streamlit UI | 8501 | `http://localhost:8501` |
| MedAnon API | 8000 | `http://localhost:8000` |
| HAPI FHIR | 8081 | `http://localhost:8081/fhir` |
| gPAS | 8080 | `http://localhost:8080/ttp-fhir/fhir/gpas` |
| gPAS web UI | 8080 | `http://localhost:8080/gpas-web/` |

### Local Development (no Docker)

```bash
make setup
cd services/anonymizer
uvicorn src.api.main:app --reload --port 8000
```

Requires Python 3.12. Does not start HAPI FHIR or gPAS — use `config.yaml` (cryptohash, no external services).

### Verifying

```bash
curl http://localhost:8000/health
# {"status":"ok"}

curl http://localhost:8000/docs
# FastAPI Swagger UI — interactive API explorer
```

---

## 2. Streamlit UI

Open `http://localhost:8501`. The sidebar shows the current anonymizer health status and navigation links to all pages.

### Patient Browser

Search patients stored in the HAPI FHIR server by name.

1. Type a name (partial match supported) and press **Search**.
2. Results appear as expandable cards — click a card to see full patient details.
3. Click **De-identify $everything** to fetch and de-identify all resources linked to that patient.
4. The de-identified NDJSON is displayed and can be downloaded.

### Condition Browser

Search patients by illness name or SNOMED CT code.

1. Enter a condition name (e.g. `diabetes`) or SNOMED code (e.g. `73211009`) and press **Search**.
2. Matching patient cards appear — click to expand.
3. Click **De-identify** on any card to run the full pipeline.

### Process Resource

De-identify a single resource by pasting it directly.

1. Click **Load sample Patient** to pre-fill with an example, or paste FHIR JSON/NDJSON/XML.
2. Choose an output format (`json`, `ndjson`, `xml`).
3. Click **De-identify**.
4. The de-identified output appears in the right panel.
5. Click **Download** to save.

### Batch

Process an entire NDJSON file at once.

1. Click **Browse files** and select an `.ndjson` file (one FHIR resource per line, max 10 MB).
2. Click **De-identify batch**.
3. A progress bar shows per-resource status.
4. Download the result file.

### Status

Live health dashboard for MedAnon, HAPI FHIR, and gPAS. Auto-refreshes every 30 seconds.

### Risk Assessment

Measure residual re-identification risk of de-identified data.

1. Upload an NDJSON file (Patient resources, optionally with Condition resources) — or paste directly.
2. Click **Analyse Risk**.
3. View:
   - **k-anonymity** (min k) — minimum equivalence class size
   - **Prosecutor risk** (1/k) — probability of re-identifying a targeted individual
   - **Journalist risk** — risk for the easiest-to-identify patient
   - **Marketer risk** — expected risk for a randomly drawn record
   - **l-diversity** — distinct Condition codes per group (requires Condition resources in input)
   - **Bar chart** of group size distribution
   - **Per-group breakdown** table (expandable)
   - **Recommendations** derived from the metrics
4. Download the full JSON report.

**Tip:** Include both Patient and Condition NDJSON in the same file for l-diversity analysis.
Run this step *after* de-identification to measure what risk remains.

**Why gPAS alone is not enough:**
gPAS pseudonymizes direct identifiers (Patient.id, MRN). k-anonymity measures the *residual* risk from quasi-identifier combinations (birth year, Condition codes) that remain in the output even after pseudonymization.

---

## 3. REST API Reference

All endpoints accept and return `application/json` by default. Streaming endpoints return `application/x-ndjson`.

If `MEDANON_API_KEY` is set, all endpoints except `/health`, `/ready`, `/metrics`, and `/docs` require the `X-API-Key: <key>` header.

### `GET /health`

Liveness probe.

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### `GET /ready`

Readiness probe — checks gPAS and FHIR server connectivity.

```bash
curl http://localhost:8000/ready
```

---

### `POST /process`

De-identify or pseudonymize a single FHIR resource or Bundle.

```bash
curl -s -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{
    "resourceType": "Patient",
    "id": "p-001",
    "name": [{"family": "Mustermann", "given": ["Max"]}],
    "birthDate": "1980-05-12",
    "address": [{"city": "Berlin", "postalCode": "10115"}]
  }'
```

---

### `POST /process/ndjson`

Process a stream of FHIR resources (one JSON object per line).

```bash
curl -s -X POST http://localhost:8000/process/ndjson \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @patients.ndjson
```

Lines starting with `//` are treated as comments and skipped. Returns streaming NDJSON.

---

### `POST /process/raw`

Multi-format endpoint — accepts JSON, NDJSON, or XML; returns any format.

| Parameter | Values | Default | Description |
|---|---|---|---|
| `input_format` | `auto`, `json`, `ndjson`, `xml` | `auto` | Force input format |
| `output_format` | `json`, `ndjson`, `xml` | `json` | Response format |

```bash
# XML in, XML out
curl -s -X POST "http://localhost:8000/process/raw?output_format=xml" \
  -H "Content-Type: application/xml" --data-binary @patient.xml

# NDJSON in, JSON array out
curl -s -X POST "http://localhost:8000/process/raw?input_format=ndjson&output_format=json" \
  -H "Content-Type: application/x-ndjson" --data-binary @patients.ndjson
```

---

### `POST /process/from-server`

Fetch resources from a FHIR server, de-identify them, and return NDJSON.

```bash
curl -s -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{
    "server_url": "http://localhost:8081/fhir",
    "resource_types": ["Patient", "Observation", "Condition"],
    "params": {"_count": 200},
    "token": "optional-bearer-token"
  }'
```

If `resource_types` is omitted, types are auto-discovered from `/metadata`.

---

### `POST /process/everything`

Fetch all resources for a specific subject via FHIR `$everything`, then de-identify.

```bash
curl -s -X POST http://localhost:8000/process/everything \
  -H "Content-Type: application/json" \
  -d '{
    "server_url":    "http://localhost:8081/fhir",
    "resource_type": "Patient",
    "resource_id":   "p-001"
  }'
```

---

### `POST /process/and-upload`

De-identify a resource and upload to a target FHIR server.

```bash
curl -s -X POST http://localhost:8000/process/and-upload \
  -H "Content-Type: application/json" \
  -d '{
    "target_server_url": "http://localhost:8081/fhir",
    "resource": {"resourceType":"Patient","id":"p-001","name":[{"family":"Test"}]}
  }'
# {"uploaded": 1, "errors": 0, "results": [...]}
```

---

### `POST /process/round-trip`

Fetch from a source FHIR server, de-identify, and upload to a target in one call.

```bash
curl -s -X POST http://localhost:8000/process/round-trip \
  -H "Content-Type: application/json" \
  -d '{
    "source_server_url": "http://source-fhir:8080/fhir",
    "target_server_url": "http://target-fhir:8080/fhir",
    "resource_types":    ["Patient", "Observation"]
  }'
```

Returns streaming NDJSON with one status line per resource:

```json
{"resourceType": "Patient", "source_id": "p-001", "target_id": "x9k2m3", "status": "ok"}
```

---

### `POST /analyse/risk`

Compute re-identification risk metrics on de-identified FHIR Patient resources.

**Input:** NDJSON body — one FHIR JSON object per line.
- Patient resources → quasi-identifier extraction (gender, birth year, zip prefix)
- Condition resources → l-diversity (correlated to Patients via `subject.reference`)
- Other resource types are silently ignored

**Returns:** JSON risk report

```bash
# Patient NDJSON only (k-anonymity only)
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @services/anonymizer/tests/data/TestBase/Patient.000.ndjson \
  | python3 -m json.tool

# Patient + Condition NDJSON (k-anonymity + l-diversity)
cat Patient.000.ndjson Condition.000.ndjson | \
  curl -s -X POST http://localhost:8000/analyse/risk \
    -H "Content-Type: application/x-ndjson" \
    --data-binary @- | python3 -m json.tool
```

**Response schema:**

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
  "l_diversity": {
    "computed": true,
    "min_l": 2,
    "max_l": 6,
    "violations": 0,
    "details": []
  },
  "groups": [
    {
      "qi": {"gender": "", "birth_year": "1980", "zip_prefix": ""},
      "size": 3,
      "k": 3,
      "risk_1_over_k": 0.333,
      "weight": 0.071
    }
  ],
  "warnings": [],
  "meta": {
    "computed_at": "2026-03-19T14:22:00Z",
    "input_lines": 42,
    "patient_lines": 42,
    "condition_lines": 0
  }
}
```

**Risk levels:**

| Level | Condition | Meaning |
|---|---|---|
| `low` | k ≥ 5 | Meets basic k-anonymity standards |
| `medium` | k = 3 or 4 | Consider further generalization |
| `high` | k = 2 | Significant re-identification risk |
| `critical` | k = 1 | Unique records — immediate action required |

---

### `POST /process/batch`

Unified batch endpoint — accepts NDJSON, JSON Bundle, or XML; streams anonymized NDJSON output. The server auto-detects the format from the `Content-Type` header.

**Accepted content types:**

| Content-Type | Format |
|---|---|
| `application/x-ndjson` | NDJSON — one FHIR resource per line |
| `application/json` | JSON Bundle (`{"resourceType": "Bundle", "entry": [...]}`) |
| `application/fhir+xml` | FHIR XML (`<Bundle>` or single resource) |

**Returns:** Streaming `application/x-ndjson` — one anonymized resource per line.

```bash
# NDJSON input
curl -s -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @patients.ndjson

# JSON Bundle input
curl -s -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/json" \
  --data-binary @bundle.json

# XML input
curl -s -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/fhir+xml" \
  --data-binary @bundle.xml
```

---

### `POST /generate/synthetic`

Generate synthetic FHIR Patient resources that preserve the statistical distributions of a de-identified input dataset — without copying any individual record.

**Input:** De-identified FHIR Patient resources (NDJSON, JSON Bundle, or XML). Use `/process/batch` first to de-identify real data before passing it here.

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `count` | int | 100 | Number of synthetic patients to generate (1–10 000) |
| `seed` | int | *(random)* | Random seed for reproducible output |

**Returns:** `application/x-ndjson` — one synthetic Patient resource per line. Each record is tagged with `meta.tag[code=SYN]` to identify it as synthetic.

**What is preserved:** gender ratio, birth year spread (±2-year jitter), 3-digit zip prefix distribution.
**What is NOT preserved:** clinical data, exact ages, exact postal codes, any identifier.

```bash
# Generate 200 synthetic patients from de-identified input (random seed)
curl -s -X POST "http://localhost:8000/generate/synthetic?count=200" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @deidentified_patients.ndjson \
  -o synthetic_patients.ndjson

# Reproducible output with fixed seed
curl -s -X POST "http://localhost:8000/generate/synthetic?count=100&seed=42" \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @deidentified_patients.ndjson \
  | python3 -m json.tool --no-ensure-ascii | head -40
```

**Example output record:**

```json
{
  "resourceType": "Patient",
  "id": "a3f2c1d4-5e6b-7f8a-9b0c-d1e2f3a4b5c6",
  "meta": {
    "tag": [
      {
        "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
        "code": "SYN",
        "display": "synthetic"
      }
    ]
  },
  "gender": "female",
  "birthDate": "1983-01-01",
  "address": [{"postalCode": "102"}]
}
```

---

### Error Codes

| HTTP | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request (unknown action, invalid Content-Length) |
| `413` | Body exceeds size limit (default 10 MB) |
| `422` | Unprocessable (invalid JSON, missing required field) |
| `500` | Unexpected processing error |
| `502` | Source FHIR server unreachable |

---

## 4. CLI Reference

Run all CLI commands from `services/anonymizer/`:

```bash
cd services/anonymizer
python3 -m cli.main <subcommand> [options]
```

### `process` — transform a local file

```bash
# JSON in, JSON out
python3 -m cli.main process patient.json output.json --config config/config_gpas.yaml

# NDJSON in, NDJSON out
python3 -m cli.main process patients.ndjson output.ndjson --config config/config_gpas.yaml

# XML in, XML out
python3 -m cli.main process patient.xml output.xml --config config/config.yaml
```

| Flag | Description |
|---|---|
| `--config`, `-c` | YAML config file path |
| `--input-format` | Force input format (`json`, `ndjson`, `xml`) |
| `--output-format` | Force output format |
| `--strip-line-prefix` | Strip prefix from each NDJSON line (default `//`) |

### `fetch` — fetch from FHIR server and anonymize

```bash
python3 -m cli.main fetch \
  --server http://localhost:8081/fhir \
  --resource-type Patient,Observation \
  --output output/patients.ndjson \
  --config config/config_gpas.yaml
```

| Flag | Description |
|---|---|
| `--server` | FHIR base URL (or `FHIR_SOURCE_URL` env) |
| `--resource-type` | Comma-separated types; omit to auto-discover |
| `--output` | Output NDJSON file |
| `--config`, `-c` | YAML config file |
| `--count` | Page size (`_count`), default 200 |
| `--since` | Only resources updated after this date |
| `--token` | Bearer token for FHIR auth |
| `--discover-only` | Print resource types from `/metadata` and exit |

### `everything` — fetch `$everything` for one resource

```bash
python3 -m cli.main everything \
  --server http://localhost:8081/fhir \
  --resource-type Patient \
  --id p-001 \
  --output output/patient-p001-everything.ndjson \
  --config config/config_gpas.yaml
```

### `push` — upload a local file to a FHIR server

```bash
python3 -m cli.main push \
  --server http://localhost:8081/fhir \
  --input output/patients.ndjson
```

### Batch scripts

```bash
# Fetch all resources from HAPI FHIR, anonymize, write NDJSON
HAPI_URL=http://localhost:8081/fhir bash scripts/batch_fetch.sh

# Process local NDJSON + generate analytics report (output/analytics/summary.md)
bash scripts/batch_process.sh

# Import Synthea test data into HAPI FHIR
bash scripts/import_testbase.sh

# Initialize gPAS domains
make init-domains
```

---

## 5. Config File Format

YAML. Environment variables use `${VAR:-default}` syntax.

```yaml
general:
  appname: MyProject
  rewrite_references: true   # rewrite FHIR References in Bundles after ID changes
  rewrite_text_ids: true     # replace original IDs in free text with pseudonyms

rules:
  - name: "pseudonymize patient ID"
    match: "Patient.id"                # FHIRPath expression
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

Rules are evaluated in order; all matching rules apply.

### FHIRPath wildcards

| Pattern | Matches |
|---|---|
| `Patient.id` | `id` on Patient resources only |
| `*.id` | `id` on every resource type |
| `*.identifier.value` | `identifier.value` on every resource |
| `*.meta.lastUpdated` | `lastUpdated` inside `meta` on every resource |

### Config auto-selection

- `GPAS_URL` set → `config_gpas.yaml`
- `GPAS_URL` not set → `config.yaml`

Override: `--config <file>` in CLI.

---

## 6. Actions Reference

### `redact`

Removes the matched field.

```yaml
- match: Patient.name
  action: redact
  params:
    replacement: "REDACTED"   # optional — set a value instead of deleting
```

---

### `cryptohash`

SHA3-256 hash. Keyed with `MEDANON_HASH_KEY` for HMAC (production-safe).

```yaml
- match: "*.id"
  action: cryptohash
  params:
    hash_type: sha3_256
    secret_key_env: MEDANON_HASH_KEY
```

A `WARNING` is logged when the key is absent (plain hash, rainbow-table reversible).

---

### `gpas_pseudonymize`

Reversible pseudonymization via gPAS TTP-FHIR gateway.

```yaml
- match: "*.id"
  action: gpas_pseudonymize
  params:
    gpas_url: ${GPAS_URL}
    gpas_domain: ${GPAS_DOMAIN}
    gpas_operation: pseudonymizeAllowCreate   # or: pseudonymize, dePseudonymize
    gpas_timeout_sec: 30
```

`pseudonymizeAllowCreate` creates the domain entry if absent. Use `pseudonymize` to fail on unknown IDs.

---

### `gpas_depseudonymize`

Reverses a gPAS pseudonym to the original value.

```yaml
- match: "Patient.id"
  action: gpas_depseudonymize
  params:
    gpas_url: ${GPAS_URL}
    gpas_domain: ${GPAS_DOMAIN}
```

---

### `encrypt`

RSA-OAEP encrypt. Reversible with the private key.

```yaml
- match: Patient.identifier.value
  action: encrypt
  params:
    algorithm: RSA
    public_key: ${MEDANON_RSA_PUBLIC_KEY}   # path to PEM file
```

---

### `decrypt`

RSA-OAEP decrypt.

```yaml
- match: Patient.identifier.value
  action: decrypt
  params:
    algorithm: RSA
    private_key: ${MEDANON_RSA_PRIVATE_KEY}
```

---

### `generalize`

Reduces precision to lower re-identification risk.

```yaml
- match: Patient.birthDate
  action: generalize
  params:
    strategy: date_year        # "1980-05-12" → "1980"

- match: Patient.address.postalCode
  action: generalize
  params:
    strategy: zip_prefix       # "10115" → "101" (default prefix_len=3)

- match: Observation.valueQuantity.value
  action: generalize
  params:
    strategy: number_round
    precision: 10
```

| Strategy | Input | Output |
|---|---|---|
| `date_year` | `"1980-05-12"` | `"1980"` |
| `date_year_month` | `"1980-05-12"` | `"1980-05"` |
| `age_bracket` | `"1980-05-12"` | `"40-49"` |
| `number_round` | `94` (precision 10) | `90` |
| `zip_prefix` | `"10115"` (len 3) | `"101"` |
| `category` | `"M"` | mapped value |

---

### `perturb`

Shifts dates or numbers by a bounded CSPRNG random offset.

```yaml
- match: "*.effectiveDateTime"
  action: perturb
  params:
    max_days: 30         # shift ± up to 30 days

- match: Observation.valueQuantity.value
  action: perturb
  params:
    max_offset: 5        # shift ± up to 5 units
```

---

### `substitute`

Replaces the value with a fixed literal.

```yaml
- match: Patient.gender
  action: substitute
  params:
    value: "unknown"
```

---

### `scrub_text`

Regex-based PHI tokenization in free text and XHTML.

```yaml
- match: "*.text"
  action: scrub_text
  params:
    mode: html_tokenize         # html_tokenize | text | tokenize
    patterns: all               # all | ssn | phone | email | date | mrn
    mapping_scope: resource     # resource | bundle | global_run
```

Modes:
- `html_tokenize` — preserves HTML/XHTML tags, tokenizes text nodes only
- `text` / `tokenize` — plain text, replaces matches with `[[TYPE_N]]` tokens

`mapping_scope` controls token numbering:
- `resource` (default) — tokens reset per resource
- `bundle` — consistent across a Bundle
- `global_run` — persist for the entire processing run

---

### `nlp_detect`

Presidio NER for names, locations, ages, and GDPR Article 9 special categories.

```yaml
- match: "*.note.text"
  action: nlp_detect
  params:
    mode: tokenize
    html: false
    threshold: 0.4       # confidence 0.0–1.0
```

Requires the `en_core_web_lg` spaCy model (`make setup` downloads it automatically).

---

## 7. Dynamic Rule Overrides

The `/process` endpoint accepts a FHIR `Parameters` wrapper to override rule settings per-request:

```json
{
  "resourceType": "Parameters",
  "parameter": [
    {
      "name": "resource",
      "resource": { "resourceType": "Patient", "id": "p-001", ... }
    },
    {
      "name": "settings",
      "part": [
        {"name": "gpas_domain", "valueString": "STUDY-42"},
        {"name": "gpas_url",    "valueString": "http://gpas:8080/ttp-fhir/fhir/gpas"}
      ]
    }
  ]
}
```

Settings override matching param keys in all rules for this request only. Unknown keys → HTTP 422.

**Allowed settings:** `gpas_url`, `gpas_domain`, `gpas_operation`, `gpas_token`, `gpas_basic_user`, `gpas_basic_pass`, `gpas_timeout_sec`, `gpas_retry_count`, `processing_errors`, `rewrite_references`, `rewrite_text_ids`.

---

## 8. gPAS Domain Setup

### Via web UI

1. Open `http://localhost:8080/gpas-web/`
2. Log in as `admin@ths` / your configured password
3. Navigate to **gPAS → Domains → New**
4. Set a name, choose `ReedSolomonLagrange`, alphabet `Symbol31`, and save.

### Via `make init-domains`

```bash
make init-domains   # imports SPE domain template into a running gPAS
```

### gRAS User Management

```sql
USE gras;
CALL createUser('newuser', 'their-password', 'Description');
CALL grantStandardRights('ths', 'gpas', 'newuser');

-- Change a password
CALL changePassword('admin', 'new-secure-password');
```

---

## 9. Formats

### JSON

Standard FHIR JSON — a single resource object or an array.

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" -d @patient.json
```

### NDJSON

One JSON object per line. `//` lines are comments and are skipped.

```
{"resourceType":"Patient","id":"p-001",...}
// this line is a comment
{"resourceType":"Observation","id":"o-001",...}
```

Use `/process/ndjson` for streaming.

### XML

FHIR R4 XML. Protected against XXE via `defusedxml`.

```bash
curl -X POST "http://localhost:8000/process/raw?output_format=xml" \
  -H "Content-Type: application/xml" --data-binary @patient.xml
```

---

## 10. Troubleshooting

### Anonymizer returns 500 on every request

```bash
docker compose logs medanon
```

Common causes:
- `config_gpas.yaml` active but `GPAS_URL` unreachable — test: `curl http://localhost:8080/ttp-fhir/fhir/gpas/metadata`
- Missing RSA key files — verify: `docker compose exec medanon ls -la /code/keys/`
- `MEDANON_HASH_KEY` unset — warning is logged; cryptohash still works with plain SHA3-256

### Risk endpoint returns "No Patient resources found"

The input NDJSON must contain `Patient` resources. Non-Patient resources (Observation, Condition, etc.) are silently ignored for k-anonymity — they are only used for l-diversity.

### k-anonymity shows all records with k=1

Common after de-identification with `config_gpas.yaml`: `gender` and `address` are fully redacted, leaving only `birthDate` (year only) as a quasi-identifier. If every patient has a unique birth year in your dataset, k=1. Provide Condition resources alongside for l-diversity, and consider using larger cohorts.

### gPAS returns "Unknown domain"

The domain does not exist yet. Use `pseudonymizeAllowCreate` or create the domain first via `make init-domains`.

### gRAS login says "Invalid credentials"

Enter the username with the `@ths` suffix: `admin@ths`, not `admin`.

### gPAS container keeps restarting

MySQL may still be initializing:

```bash
docker compose ps gpas-db
docker compose logs gpas-db | tail -20
docker compose restart gpas   # once gpas-db shows (healthy)
```

### `fhirpathpy` import error in tests

`fhirpathpy==0.1.0` is incompatible with Python 3.13. Run affected tests inside Docker:

```bash
docker compose exec medanon sh -c "cd /code && python3 -m pytest tests/ -q"
```

The following tests run locally without Docker:
```bash
cd services/anonymizer && python3 -m pytest tests/test_io_formats.py tests/test_risk.py -q
```

### CORS errors from browser

```
MEDANON_CORS_ORIGINS=http://localhost:3000,http://myapp.example.com
```

### Container health check fails (unhealthy)

```bash
docker compose exec medanon python3 -c \
  "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
```
