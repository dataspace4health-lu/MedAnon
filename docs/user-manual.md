# MedAnon — User Manual

## Service URLs

| Service | URL | Purpose |
|---|---|---|
| Web UI | `http://localhost:8501` | Browser-based interface |
| Anonymizer API | `http://localhost:8000` | REST API |
| Swagger / OpenAPI docs | `http://localhost:8000/docs` | Interactive API explorer |
| Source FHIR Server | `http://localhost:8081/fhir` | HAPI FHIR R4 — identified patient data |
| Target FHIR Server | `http://localhost:8082/fhir` | HAPI FHIR R4 — de-identified data |
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

Open `http://localhost:8501`. The sidebar shows live health status for all services and your current config profile.

### Patient Browser

Search patients in the source FHIR server by name and de-identify their complete record.

1. Enter a patient name (partial match supported) → **Search**
2. Results appear as expandable cards with demographics (identifiers, addresses, telecom)
3. Click a card → **De-identify $everything** to fetch all linked resources (Observations, Conditions, Encounters, Medications, etc.)
4. De-identified output appears as NDJSON — click **Download** to save
5. Optionally click **Upload to Target** to push the de-identified bundle to the target FHIR server

### Condition Browser

Find patients by diagnosis, then de-identify their records.

1. Enter an illness name (e.g. `diabetes`) or SNOMED CT code (e.g. `73211009`) → **Search**
2. Matching patients shown as cards
3. Click a card → **De-identify** to run the full pipeline

### Process Resource

De-identify a single FHIR resource interactively.

1. Click **Load sample** to pre-fill with a realistic example, or paste your own JSON / NDJSON / XML
2. Select output format (`json`, `ndjson`, `xml`)
3. Click **De-identify**
4. Original input and de-identified output shown side-by-side

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

### Status

Health dashboard for all services: anonymizer, FHIR source, FHIR target, gPAS, Redis.

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

Profile names: `auto`, `minimal`, `gpas`, `gdpr`, `hipaa`, `research`, `structural`.

### NDJSON batch

```bash
curl -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @input.ndjson \
  -o output.ndjson
```

One resource per line in, one de-identified resource per line out. Also accepts FHIR Bundle JSON.

### Fetch from FHIR server and de-identify

```bash
curl -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{
    "server_url": "http://localhost:8081/fhir",
    "resource_types": ["Patient", "Observation", "Condition"]
  }'
```

Streams de-identified NDJSON back. Fetches all pages for each resource type.

### Patient `$everything` de-identify

```bash
curl -X POST http://localhost:8000/process/everything \
  -H "Content-Type: application/json" \
  -d '{
    "server_url": "http://localhost:8081/fhir",
    "patient_id": "123"
  }'
```

De-identifies all resources for one patient (the FHIR `$everything` operation).

### Fetch, de-identify, and upload

```bash
curl -X POST http://localhost:8000/process/and-upload \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://localhost:8081/fhir",
    "target_url": "http://localhost:8082/fhir",
    "resource_types": ["Patient", "Observation"],
    "config_profile": "structural"
  }'
```

### Async bulk export

For large datasets that take longer than an HTTP timeout:

```bash
# Submit — returns immediately
curl -X POST http://localhost:8000/v1/jobs/bulk-export \
  -H "Content-Type: application/json" \
  -d '{"source_url":"http://localhost:8081/fhir","config_profile":"gdpr"}'
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
curl -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @output.ndjson | python3 -m json.tool
```

| `min_k` | Risk level | Action |
|---|---|---|
| ≥ 5 | low | No action required — meets basic k-anonymity |
| 3–4 | medium | Consider broader date generalization or additional field suppression |
| 2 | high | Suppress records that form pairs |
| 1 | critical | Unique records exist — do not share without remediation |

---

## CLI

```bash
# From repo root
make setup    # create venv, install deps, download spaCy model

cd services/anonymizer

# De-identify a local file
python3 -m cli.main process input.ndjson output.ndjson \
  --config config/config_hipaa_safe_harbor.yaml

# Fetch from FHIR server and de-identify
python3 -m cli.main fetch \
  --server http://localhost:8081/fhir \
  --resource-type Patient,Observation \
  --output output/all.ndjson \
  --config config/config_gpas.yaml
```

---

## Config file format

Rules are evaluated in order — more specific rules before wildcards.

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
    match: "*.text.div"            # wildcard — matches any resourceType
    action: redact
```

### Actions reference

| Action | Description | Key params |
|---|---|---|
| `redact` | Remove field value | `replacement` (default `""`) |
| `cryptohash` | HMAC-SHA3-256 when `MEDANON_HASH_KEY` set; plain SHA3-256 otherwise | `hash_type`, `secret_key_env` |
| `generalize` | Coarsen the value | `strategy`: `date_year`, `date_year_month`, `zip_prefix`, `age_bracket`, `number_round` |
| `substitute` | Replace with a fixed value | `substitute_with` |
| `perturb` | Add random noise to numeric values | `range`, `distribution` |
| `scrub_text` | Regex-based PHI removal in free text | `mode`, `patterns` |
| `nlp_detect` | Presidio NLP entity detection (PERSON, GPE, DATE, etc.) | `mode`, `threshold` |
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
