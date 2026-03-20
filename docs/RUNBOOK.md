# MedAnon Operational Runbook

This runbook covers the end-to-end workflow for deploying and operating MedAnon
to de-identify FHIR patient data.

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Docker Engine | 24+ | Required for full stack |
| Docker Compose | v2 | Bundled with Docker Desktop |
| Python | 3.11+ | Local dev / tools only |
| GNU Make | 4+ | For make targets |

### 1. Clone and configure

```bash
git clone <repo-url> && cd fhir-diet
cp .env.example .env
# Edit .env — set GPAS_BASIC_PASS and any custom ports/domains
```

### 2. Start the stack

```bash
make up          # production stack (anonymizer + HAPI FHIR + gPAS + MySQL)
make dev         # hot-reload dev stack (source volume mounted)
```

Wait ~30 seconds for all services to be healthy:
```bash
curl -s http://localhost:8000/health | python3 -m json.tool
curl -s http://localhost:8000/ready  | python3 -m json.tool
```

Expected `/ready` response when all services are up:
```json
{ "ready": true, "checks": { "gpas": true, "fhir": true } }
```

---

## Config Selection

Choose the profile that matches your use case:

| Use case | Config file |
|---|---|
| Local dev / testing (no external deps) | `config.yaml` |
| Production with reversible pseudonymization | `config_gpas.yaml` |
| EU patient data — GDPR compliance | `config_gdpr_eu.yaml` |
| US patient data — HIPAA Safe Harbor | `config_hipaa_safe_harbor.yaml` |
| Internal research under IRB approval | `config_research_pseudonymous.yaml` |

The active config is controlled by the `MEDANON_CONFIG` environment variable
(set in `.env`). Default: `config.yaml`.

See [docs/policies.md](policies.md) for a full comparison of all profiles.

---

## Processing Workflow

### Option A — Streamlit UI

1. Open `http://localhost:8501` in a browser
2. Navigate to **Batch Processing** (page 3)
3. Upload a FHIR file (NDJSON, JSON Bundle, or XML — max 10 MB)
4. Click **Process** — resources stream as they are de-identified
5. Download the resulting de-identified NDJSON file
6. Navigate to **Risk Assessment** (page 6) and upload the output file
7. Review k-anonymity metrics and recommendations

### Option B — REST API (curl)

```bash
# Single resource (JSON)
curl -s -X POST http://localhost:8000/process/raw \
  -H "Content-Type: application/json" \
  -d '{"resourceType":"Patient","id":"p1","name":[{"family":"Smith"}],"birthDate":"1985-03-12"}' \
  | python3 -m json.tool

# NDJSON batch (streaming)
curl -s -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @input.ndjson

# JSON Bundle
curl -s -X POST http://localhost:8000/process/batch \
  -H "Content-Type: application/json" \
  --data-binary @bundle.json

# Risk assessment
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @deid_output.ndjson | python3 -m json.tool
```

### Option C — CLI (bulk processing)

```bash
# Process a directory of NDJSON files
make batch

# Or directly
cd services/anonymizer
python3 -m src.cli.main process \
  --input  ../../data/input/ \
  --output ../../data/output/ \
  --config config/config_hipaa_safe_harbor.yaml
```

---

## Verifying Output Quality

Run the analytics tool to compare input vs. output and generate an evidence report:

```bash
cd services/anonymizer

python3 tools/analyze_results.py \
  --input  tests/data/TestBase/Patient.000.ndjson \
  --output /tmp/Patient.000.deid.ndjson \
  --operator "Your Name" \
  --config config/config_hipaa_safe_harbor.yaml \
  --report-json /tmp/evidence.json \
  --report-md   /tmp/evidence.md

cat /tmp/evidence.md
```

**What to check in the report:**
- `ids_changed` should equal `comparable_resource_ids` (all IDs transformed)
- `reference_changes` should be > 0 if `rewrite_references: true` is set
- `token_counts` should show NLP entity tokens (e.g. `PERSON`, `GPE`) if
  `nlp_detect` rules are active
- `config_sha256` provides an audit-proof record of the exact config used

---

## Risk Assessment

After de-identification, always run a risk assessment to measure residual
re-identification risk:

```bash
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @deid_output.ndjson | python3 -m json.tool
```

**Risk levels:**

| Level | Condition | Action required |
|---|---|---|
| Low | min_k ≥ 5 | None — meets basic k-anonymity |
| Medium | min_k = 3 or 4 | Consider broader date generalization |
| High | min_k = 2 | Suppress records in singleton/pair groups |
| Critical | min_k = 1 | Immediate action — unique records exist |

---

## Troubleshooting

### gPAS connection failure

**Symptom:** `/ready` returns `"gpas": false`; requests with `gpas_pseudonymize`
action return 500 errors.

**Steps:**
1. Check `docker compose ps` — is the `gpas` container healthy?
2. Verify env vars: `GPAS_URL`, `GPAS_BASIC_USER`, `GPAS_BASIC_PASS`
3. Test connectivity: `curl -v $GPAS_URL/metadata`
4. Check gPAS domain exists: the domain must be pre-created in the gPAS UI
   or via `scripts/init_gpas_domains.sh`
5. Review logs: `make logs` → look for `gpas` error lines

### spaCy model missing

**Symptom:** `nlp_detect` action returns errors; startup log shows
`OSError: [E050] Can't find model 'en_core_web_lg'`

**Fix:**
```bash
make setup   # downloads en_core_web_lg inside the container
# or manually:
docker compose exec anonymizer python3 -m spacy download en_core_web_lg
```

### Port conflicts

**Symptom:** `docker compose up` fails with `address already in use`

**Fix:** edit `.env` and change the port mappings, or stop the conflicting process:
```bash
lsof -i :8000   # find what's using the port
```

Default ports: anonymizer `8000`, HAPI FHIR `8081`, gPAS `8080`, MySQL `3306`.

### XML parse errors

**Symptom:** uploading an XML file returns `422 Could not parse input`

**Steps:**
1. Validate the XML is well-formed: `xmllint --noout your_file.xml`
2. Confirm the `Content-Type` header is `application/fhir+xml` (the UI sets
   this automatically from the file extension)
3. Check for namespace issues — the root element must declare the FHIR namespace:
   `xmlns="http://hl7.org/fhir"`

### File size limit

**Symptom:** upload returns `413 Request body exceeds the 10 MB limit`

**Fix:** split the input file into smaller chunks, or increase the limit:
```bash
# .env
MEDANON_MAX_BODY_BYTES=20971520   # 20 MB
```

---

## Quick-Reference: make Commands

| Command | Description |
|---|---|
| `make up` | Start full Docker stack |
| `make dev` | Start with hot-reload (source mounted) |
| `make down` | Stop and remove containers |
| `make build` | Rebuild Docker images |
| `make logs` | Tail all container logs |
| `make setup` | Create venv, install deps, download spaCy model |
| `make lint` | Run ruff linter |
| `make format` | Run ruff formatter |
| `make batch` | Run batch_process.sh + generate analytics |

---

## Security Checklist

Before going to production:

- [ ] Set a strong `MEDANON_HASH_KEY` in `.env` (do not use the default)
- [ ] Set `MEDANON_API_KEY` to restrict API access
- [ ] Set `GPAS_BASIC_PASS` to a non-default password
- [ ] Enable TLS termination in front of the anonymizer (nginx / traefik)
- [ ] Restrict network access to the HAPI FHIR and gPAS ports (internal only)
- [ ] Review the Helm chart (`helm/`) for Kubernetes hardening settings
- [ ] Run risk assessment on every batch output before sharing data
- [ ] Archive the evidence report alongside every de-identified dataset
