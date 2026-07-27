---
title: "Use the Web UI & API"
sidebar_position: 14
description: "Walkthrough of the React UI, REST API quick-start, and CLI batch processing."
---

# MedAnon, User Manual

## Service URLs

| Service | URL | Purpose |
|---|---|---|
| Web UI | `http://localhost:8501` | Browser-based interface |
| Anonymizer API | `http://localhost:8000` | REST API |
| Swagger / OpenAPI docs | `http://localhost:8000/docs` | Interactive API explorer |
| Source FHIR Server | (internal only, `source-net`) | HAPI FHIR R4, identified patient data; no host port published for security isolation. Access via anonymizer proxy endpoints (`/process/from-server`, `/process/everything`, etc.) |
| Target FHIR Server | `http://localhost:8082/fhir` | HAPI FHIR R4, de-identified data |
| gPAS Web UI | `http://localhost:8080/gpas-web/` | Pseudonym management (admin only) |

---

## Getting started

```bash
cp .env.example .env   # fill in secrets (see DEPLOYMENT.md)
make build             # build anonymizer + UI images
make up                # start all services (~90 s for gPAS)
make init-domains      # create gPAS pseudonym domain
```

Verify:
```bash
curl http://localhost:8000/health    # {"status":"ok"}
curl http://localhost:8000/ready     # {"ready": true}
open http://localhost:8501
```

---

## Web UI

Open `http://localhost:8501`. The sidebar shows live health status for all services and your current config profile. There are 24 pages accessible from the sidebar navigation.

### Home (Dashboard)

Overview page with aggregate statistics: total resources processed, active jobs, service health indicators, and recent activity log.

### Patient Browser

Search patients in the source FHIR server by name and de-identify their complete record.

1. Enter a patient name (partial match supported) → **Search**
2. Results appear as expandable cards with demographics (identifiers, addresses, telecom)
3. Click a card → **De-identify $everything** to fetch all linked resources (Observations, Conditions, Encounters, Medications, etc.)
4. De-identified output appears as NDJSON, click **Download** to save
5. Optionally click **Upload to Target** to push the de-identified bundle to the target FHIR server

### Condition Browser

Find patients by diagnosis, then de-identify their records.

1. Enter an illness name (e.g. `diabetes`) or SNOMED CT code (e.g. `73211009`) → **Search**
2. Matching patients shown as cards
3. Click a card → **De-identify** to run the full pipeline

### De-identify (Per-Patient)

Fetch a single patient's complete record from the source FHIR server and de-identify it in one step.

1. Enter a Patient ID
2. Select config profile
3. Click **De-identify**, fetches `$everything` for that patient and runs the pipeline
4. Download the de-identified NDJSON or upload to the target FHIR server

### Process Resource

De-identify a single FHIR resource interactively.

1. Click **Load sample** to pre-fill with a realistic example, or paste your own JSON / NDJSON / XML
2. Select output format (`json`, `ndjson`, `xml`)
3. Click **De-identify**
4. Original input and de-identified output shown side-by-side

### Batch Processing

De-identify multiple resources from a file upload.

1. Upload a JSON, NDJSON, or XML file (or drag-and-drop)
2. Select config profile and output format
3. Click **Process**, results stream back as NDJSON
4. Download the de-identified output

### Bulk De-identify

Long-running async export jobs for de-identifying an entire FHIR server.

1. Select source FHIR server and config profile
2. Click **Start Bulk Export** → job submitted, returns immediately
3. Job cards show live progress (resources fetched / uploaded / elapsed time)
4. Click **Stop** to cancel a running job
5. Download result NDJSON when status shows `done`

### Risk Assessment

k-anonymity and l-diversity analysis on de-identified output.

1. Upload or paste de-identified NDJSON
2. Results show `min_k` value and risk level with remediation guidance

### Synthetic Data

Generate synthetic FHIR data from de-identified datasets (requires the analytics microservice).

1. Upload or paste de-identified NDJSON
2. Configure generation parameters (sample size, privacy budget)
3. Click **Generate**, synthetic resources returned as downloadable NDJSON

### Status

Health dashboard for all services: anonymizer, FHIR source, FHIR target, gPAS, Redis, NLP microservice, analytics.

### Config Profiles

Browse, select, and manage de-identification config profiles.

1. View all available profiles (built-in and user-defined) with descriptions
2. Click a profile to inspect its full YAML rule set
3. Set the active profile for subsequent processing requests

### Config Builder

Create custom de-identification profiles through a guided UI.

1. Start from a blank profile or clone an existing one
2. Add, edit, and reorder rules with match expressions and actions
3. Save the profile, available immediately via `?config_profile=<name>` on any endpoint

**AI-assisted config generation (requires `MEDANON_AI_ENABLED=true`, role: `admin`):**

1. Click the **AI** button in the Config Builder toolbar
2. Describe your use case in plain language (e.g. "GDPR-compliant profile for cardiovascular research, retain LOINC codes and measurement values, generalize dates to year-month")
3. The AI agent generates a complete YAML config using existing configs as few-shot context
4. Review the generated rules, adjust if needed, then save

The AI panel also shows the rationale for each generated rule and which bundled profile it was derived from.

### Processing History

View a running log of all de-identification processing runs (requires `MEDANON_SCORING_ENABLED=true`).

1. Shows all past runs: endpoint, config profile, resource count, composite score, timestamp
2. Click a row to see the full score breakdown (privacy / utility / quality)
3. Runs are grouped by endpoint, filter by `/v1/process`, `/v1/jobs/bulk-export`, etc.
4. Aggregate statistics at the top: total runs, average composite score, runs by profile
5. Use **Purge** to delete all run history (analyst role, admin enforcement pending)

### Jobs Monitor

Fleet view of all server-side async jobs.

1. Browse all jobs with status pills and live progress bars
2. Filter by status and job type
3. Click a job row to open the detail drawer (progress, resource breakdown, error, cancel/reprocess actions)
4. Dead-letter panel shows jobs with `status=dead`, requeue via the action menu

### Analytics Dashboard

Scoring analytics for all processing runs (requires `MEDANON_SCORING_ENABLED=true`).

1. KPI strip: total runs, scored runs, resources processed, average composite score
2. Score trend chart (area), score histogram (distribution), threshold alert panel (configurable slider)
3. Filter by config profile
4. Sortable, paginated runs table with score breakdown per row

### Audit Report

Per-run audit trail.

1. View privacy gate status, score bars, and expandable Markdown audit report for each run
2. Drill into individual runs for per-resource-type breakdowns and remediation recommendations

### Target FHIR Browser

Browse de-identified resources that have been uploaded to the target FHIR server.

1. Select a resource type (Patient, Observation, Condition, etc.)
2. Browse paginated results with expandable JSON detail
3. Verify de-identification quality by inspecting individual resources

---

## REST API

### Authentication

When `MEDANON_API_KEY` is set, all endpoints (except `/health`, `/ready`, `/metrics`, `/docs`) require:
```
X-API-Key: <your-api-key>
```

### De-identify a single resource

```bash
curl -X POST http://localhost:8000/process \
  -H "Content-Type: application/json" \
  -d '{
    "resourceType": "Patient",
    "id": "123",
    "name": [{"family": "Smith", "given": ["John"]}],
    "birthDate": "1985-03-12",
    "gender": "male"
  }'
```

Response: de-identified resource. Name redacted, birthDate → `"1985"`, id → pseudonym.

### Override config profile per request

Append `?config_profile=<name>` to any endpoint:

```bash
curl -X POST "http://localhost:8000/process?config_profile=hipaa" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

Profile names: `auto`, `minimal`, `gpas`, `gdpr`, `hipaa`, `research`, `structural`, `value-masking`.

### NDJSON batch

```bash
curl -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @input.ndjson \
  -o output.ndjson
```

One resource per line in, one de-identified resource per line out. Also accepts FHIR Bundle JSON.

### Fetch from FHIR server and de-identify

> **Note:** The source FHIR server has no host port published (security isolation). `server_url` defaults to `FHIR_SOURCE_URL` set in `.env`. Omit it to use the configured default, or pass the Docker-internal URL when calling from another container.

```bash
curl -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{
    "resource_types": ["Patient", "Observation", "Condition"]
  }'
```

Streams de-identified NDJSON back. Fetches all pages for each resource type.

### Patient `$everything` de-identify

```bash
curl -X POST http://localhost:8000/process/everything \
  -H "Content-Type: application/json" \
  -d '{
    "patient_id": "123"
  }'
```

De-identifies all resources for one patient (the FHIR `$everything` operation).

### Fetch, de-identify, and upload

```bash
curl -X POST http://localhost:8000/process/and-upload \
  -H "Content-Type: application/json" \
  -d '{
    "target_url": "http://localhost:8082/fhir",
    "resource_types": ["Patient", "Observation"],
    "config_profile": "structural"
  }'
```

### Async bulk export

For large datasets that take longer than an HTTP timeout:

```bash
# Submit - returns immediately
curl -X POST http://localhost:8000/v1/jobs/bulk-export \
  -H "Content-Type: application/json" \
  -d '{"config_profile":"gdpr"}'
# → {"job_id":"abc123","status":"pending"}

# Poll status
curl http://localhost:8000/v1/jobs/abc123
# → {"status":"running","progress":{"processed":5000,"total":20000,"elapsed_s":45}}

# List all jobs
curl "http://localhost:8000/v1/jobs?status=running&limit=10"

# Download result when done
curl http://localhost:8000/v1/jobs/abc123/result -o result.ndjson

# Cancel a running job
curl -X DELETE http://localhost:8000/v1/jobs/abc123
```

### Risk assessment

```bash
curl -X POST http://localhost:8000/v1/analyse/risk \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{"resources": [...], "quasi_identifiers": ["gender","birthDate","address.postalCode"]}' \
  | python3 -m json.tool
```

| `k_anonymity` | Risk level | Action |
|---|---|---|
| ≥ 5 | low | No action required, meets basic k-anonymity |
| 3-4 | medium | Consider broader date generalization or additional field suppression |
| 2 | high | Suppress records that form pairs |
| 1 | critical | Unique records exist, do not share without remediation |

### Score a de-identified resource

```bash
curl -X POST http://localhost:8000/v1/score \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{
    "original": {"resourceType":"Patient","id":"p1","name":[{"family":"Smith"}],"birthDate":"1985-03-12"},
    "deidentified": {"resourceType":"Patient","id":"5e2f1a9c...","name":[],"birthDate":"1985"},
    "config_profile": "gdpr"
  }'
```

Returns a composite score (0-100) split across privacy, utility, and quality dimensions.
Add `?include_audit=true` to include a Markdown audit report in the response.

### AI agent endpoints (requires `MEDANON_AI_ENABLED=true`)

```bash
# Check AI availability
curl http://localhost:8000/v1/ai/status -H "X-API-Key: $MEDANON_API_KEY"

# Detect PII in a text snippet (local model only - never sent to external API)
curl -X POST http://localhost:8000/v1/ai/detect-pii \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{"text": "Patient Hans Müller, DOB 1951-08-14, admitted to Charité Berlin."}'

# Explain a config rule in plain language
curl -X POST http://localhost:8000/v1/ai/explain \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{"config_profile": "gdpr", "rule_name": "pseudonymize patient ID"}'

# Generate a config profile from a description (admin only)
curl -X POST http://localhost:8000/v1/ai/generate-config \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ADMIN_KEY" \
  -d '{"description": "GDPR profile for cardiovascular research. Retain LOINC codes. Dates to year-month."}'

# Compliance gap analysis
curl -X POST http://localhost:8000/v1/ai/compliance \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{"config_profile": "gdpr", "frameworks": ["GDPR", "HIPAA"]}'
```

---

## CLI

```bash
# From repo root
make setup    # create venv, install anonymizer Python deps

cd services/anonymizer

# De-identify a local file
python3 -m cli.main process input.ndjson output.ndjson \
  --config config/config_hipaa_safe_harbor.yaml

# Fetch from FHIR server and de-identify
# (run inside the container - source FHIR has no host port)
python3 -m cli.main fetch \
  --server http://hapi-fhir:8080/fhir \
  --resource-type Patient,Observation \
  --output output/all.ndjson \
  --config config/config_value_masking.yaml
```

**Note on NLP in the CLI:** Presidio + spaCy no longer run in the anonymizer process. NLP de-identification delegates to the NLP microservice (`nlp-lb:8200`). When using the CLI outside Docker, set `NLP_SERVICE_URL=http://localhost:8200` (or omit NLP rules if the microservice is not running, text fields will not be scrubbed).

---

## Config file format

Rules are evaluated in order, more specific rules before wildcards.

```yaml
general:
  appname: MyOrg-Custom
  hash_type: sha3_256              # sha3_256 | sha256 | sha512
  rewrite_references: true         # rewrite Patient/old-id → Patient/new-id after ID changes
  rewrite_text_ids: true           # replace changed IDs that appear in text fields

rules:
  - name: "redact patient name"
    match: "Patient.name"          # FHIRPath expression
    action: redact

  - name: "hash patient id"
    match: "Patient.id"
    action: cryptohash
    params:
      secret_key_env: MEDANON_HASH_KEY   # reads from env, never from config

  - name: "generalize birth date"
    match: "Patient.birthDate"
    action: generalize
    params:
      strategy: date_year          # date_year | date_year_month | zip_prefix | age_bracket

  - name: "apply to all resource types"
    match: "*.text.div"            # wildcard - matches any resourceType
    action: redact
```

### Actions reference

| Action | Description | Key params |
|---|---|---|
| `redact` | Remove field value | `replacement` (default `""`) |
| `cryptohash` | HMAC-SHA3-256 when `MEDANON_HASH_KEY` set; plain SHA3-256 otherwise | `hash_type`, `secret_key_env` |
| `generalize` | Coarsen the value | `strategy`: `date_year`, `date_year_month`, `zip_prefix`, `age_bracket`, `number_round` |
| `substitute` | Replace with a fixed value | `substitute_with` (required) |
| `perturb` | Add random noise to numeric values | `min`, `max`, `distribution` |
| `mask` | Partial masking, keeping shape | `strategy`: `keep_prefix`, `keep_suffix`, `keep_domain`, `keep_country_code`, `full`; `keep_chars`, `mask_char` |
| `date_shift` | Deterministic per-subject date offset (preserves ordering/intervals) | `max_days`, `direction` (`past`/`future`/`both`), `anchor_path`, `preserve_age_bracket` |
| `tokenize` | Format-preserving deterministic pseudonym | `format` (`#`=digit, `@`=alpha, `*`=alnum), `namespace`, `preserve_length` |
| `scrub_text` | Regex-based PHI removal in free text | `mode`, `patterns` |
| `nlp_detect` | NLP entity detection via NLP microservice (Presidio + spaCy), PERSON, GPE, DATE, etc. | `mode`, `threshold` |
| `nlp_detect_act` | Entity-specific conditional NLP: detect first, then apply a per-entity action (e.g. dates->generalize, names->redact). No-op when nothing detected. | `threshold`, `html`, `entity_actions`, `entities` |
| `encrypt` | RSA public-key encryption | `public_key_path` |
| `gpas_pseudonymize` | Reversible gPAS pseudonym | `gpas_url`, `gpas_domain`, `gpas_operation` |

**FHIR code binding constraint:** Some fields have required value sets. `Patient.gender` and `Practitioner.gender` must be `male | female | other | unknown`. Using `substitute_with: "[REDACTED]"` on these fields causes HAPI to reject the resource (HAPI-1821). Use `substitute_with: "unknown"` instead.

### Key management

For production use of `cryptohash`:

```bash
# Generate key
openssl rand -hex 32

# Set in .env (never in the YAML config file)
MEDANON_HASH_KEY=<generated-value>

# Reference in rule
params:
  secret_key_env: MEDANON_HASH_KEY
```

After key rotation, existing pseudonymized outputs cannot be re-linked to new outputs without the original key.

---

## Evidence report

After each de-identification run, generate an evidence report for audit purposes:

```bash
python3 services/anonymizer/tools/analyze_results.py \
  --input  original_patients.ndjson \
  --output deid_patients.ndjson \
  --operator "Alice Smith" \
  --config  services/anonymizer/config/config_hipaa_safe_harbor.yaml \
  --report-json evidence.json \
  --report-md   evidence.md
```

The report includes:
- Operator name and ISO timestamp
- Config file name and SHA-256 hash (audit-proof fingerprint of the exact config used)
- Resource counts before/after
- ID change count and reference rewrite count
- NLP entity token counts (confirms text scrubbing ran)
