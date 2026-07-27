---
title: "REST API Reference"
sidebar_position: 2
description: "All REST endpoints, auth, error format, rate limits, and curl examples."
---

# API Reference

**System:** MedAnon  
**Base URL:** `http://localhost:8000` (Docker) · `https://<host>/api` (via UI nginx)  
**Version:** Phase 4  
**Last updated:** 2026-04-21

This document is the consolidated reference for all REST API endpoints. For deployment details see [DEPLOYMENT.md](../how-to/deploy-docker.md). For the scoring model see [scoring-system.md](../explanation/scoring-system.md). For security and auth see [security.md](../explanation/security-model.md).

---

## Authentication

All protected endpoints require the `X-API-Key` header when `MEDANON_API_KEY` is set:

```http
X-API-Key: <your-api-key>
```

Bearer tokens are also accepted  either an OIDC JWT (when `MEDANON_AUTH_PROVIDER=oidc`) or a SMART on FHIR token:

```http
Authorization: Bearer <token>
```

The active mode is selected by `MEDANON_AUTH_PROVIDER` (`auto` / `apikey` / `oidc` / `none`). The SPA discovers it at runtime via the open endpoint `GET /v1/auth/config`. See [Configure Authentication](../how-to/configure-authentication.md) for the OIDC/Keycloak flow.

Open endpoints (no auth required): `/health`, `/ready`, `/metrics`, `/docs`, `/openapi.json`, `/redoc`, `/.well-known/smart-configuration`, `/v1/auth/config`.

**Roles:** `viewer < analyst < admin`. Each endpoint notes the minimum required role.

---

## Error Format

All error responses use RFC 7807 Problem Details:

```json
{
  "detail": "Human-readable error message"
}
```

| Status | Meaning |
|---|---|
| 400 | Bad request, malformed JSON or FHIR |
| 401 | Missing or invalid API key / token |
| 403 | Insufficient role |
| 404 | Resource or job not found |
| 413 | Request body exceeds `MEDANON_MAX_BODY_BYTES` (default 10 MB) |
| 422 | Validation error, invalid parameter, unsupported setting, SSRF block |
| 429 | Rate limit exceeded |
| 500 | Processing error (check service logs) |
| 503 | Upstream unavailable (gPAS, NLP, token introspection) |

---

## Rate Limits

Default per-endpoint limits (configurable via env vars):

| Endpoint group | Default | Override env var |
|---|---|---|
| `POST /process` | 200/minute | `MEDANON_RATE_PROCESS` |
| `POST /process/ndjson` | 60/minute | `MEDANON_RATE_NDJSON` |
| `POST /process/raw` | 200/minute | `MEDANON_RATE_RAW` |
| `POST /process/batch` | 60/minute | `MEDANON_RATE_BATCH` |

Limits are per-IP, shared across replicas when Redis is configured.

---

## Config Profile Selection

All processing endpoints accept an optional query parameter:

```
?config_profile=<profile>
```

| Value | Profile used |
|---|---|
| `auto` (default) | `config.yaml`. `GPAS_URL` no longer changes profile selection - select `value-masking` (or a custom profile) explicitly for gPAS pseudonymization |
| `minimal` | `config.yaml`, HMAC-SHA3-256 hash + regex scrubbing |
| `gdpr` | `config_gdpr_eu.yaml`, GDPR Art. 4(5) |
| `hipaa` | `config_hipaa_safe_harbor.yaml`, HIPAA Safe Harbor |
| `value-masking` | `config_value_masking.yaml`, gPAS pseudonymization + fine-grained NLP masking |

---

## 1. Health & Readiness

### `GET /health`

Liveness check. Always returns 200 if the process is running.

```http
GET /health
```

**Response 200:**
```json
{"status": "ok"}
```

---

### `GET /ready`

Readiness check. Verifies connectivity to Redis, gPAS, and NLP microservice. Returns 503 if any required dependency is unavailable.

```http
GET /ready
```

**Response 200:**
```json
{"status": "ready", "redis": "ok", "gpas": "ok", "nlp": "ok"}
```

**Response 503:**
```json
{"status": "not_ready", "nlp": "unavailable"}
```

---

### `GET /metrics`

Prometheus metrics endpoint. Exposes counters and histograms for requests, gPAS calls, NLP calls, and FHIR operations.

---

## 2. De-identification

### `POST /process`

**Role:** `analyst`  
**Rate limit:** 200/min

Process a single FHIR resource or FHIR Bundle. Returns the de-identified result.

**Request:**
```http
POST /process?config_profile=gdpr
Content-Type: application/json
X-API-Key: <key>

{
  "resourceType": "Patient",
  "id": "patient-123",
  "name": [{"family": "Müller", "given": ["Hans"]}],
  "birthDate": "1951-08-14"
}
```

**Response 200:**
```json
{
  "resourceType": "Patient",
  "id": "a7f3c91b2d8e4f6a0b5c3d2e1f9a8b7c",
  "name": [],
  "birthDate": null
}
```

**Notes:**
- Accepts a single resource dict, a FHIR Bundle, or a JSON array of resources
- Also accepts a FHIR `Parameters` wrapper with dynamic rule overrides (see below)

**Dynamic settings via Parameters wrapper:**
```json
{
  "resourceType": "Parameters",
  "parameter": [
    {"name": "resource", "resource": { ... }},
    {
      "name": "settings",
      "part": [
        {"name": "gpas_url", "valueString": "http://custom-gpas:8080"}
      ]
    }
  ]
}
```

---

### `POST /process/ndjson`

**Role:** `analyst`  
**Rate limit:** 60/min

Process a stream of FHIR resources in NDJSON format (one JSON object per line).

**Request:**
```http
POST /process/ndjson?config_profile=hipaa
Content-Type: application/x-ndjson
X-API-Key: <key>

{"resourceType":"Patient","id":"p1","name":[{"family":"Smith"}]}
{"resourceType":"Observation","id":"obs1","subject":{"reference":"Patient/p1"}}
```

**Response 200** (`application/x-ndjson`):
```
{"resourceType":"Patient","id":"5e2f1a9c...","name":[]}
{"resourceType":"Observation","id":"b3d4e5f6...","subject":{"reference":"Patient/5e2f1a9c..."}}
```

The response is a streaming NDJSON response. References between resources are rewritten after all IDs are transformed.

---

### `POST /process/raw`

**Role:** `analyst`  
**Rate limit:** 200/min

Process a single FHIR resource. Accepts JSON, XML, or raw FHIR. Returns the same format as input.

**Request (XML):**
```http
POST /process/raw?config_profile=gdpr
Content-Type: application/fhir+xml
X-API-Key: <key>

<Patient xmlns="http://hl7.org/fhir">
  <id value="patient-123"/>
  <name><family value="Müller"/></name>
</Patient>
```

**Response 200** (`application/fhir+xml`): De-identified XML.

---

### `POST /process/batch`

**Role:** `analyst`  
**Rate limit:** 60/min

Process multiple resources in parallel within a single request.

**Request:**
```http
POST /process/batch
Content-Type: application/json
X-API-Key: <key>

[
  {"resourceType": "Patient", "id": "p1", ...},
  {"resourceType": "Observation", "id": "obs1", ...}
]
```

**Response 200:** Array of de-identified resources.

---

### `POST /process/from-server`

**Role:** `analyst`

Fetch a resource from a remote FHIR server, de-identify it, and return the result. Applies SSRF protection to `server_url`.

**Request:**
```json
{
  "server_url": "https://my-fhir-server.example.com/fhir",
  "resource_type": "Patient",
  "resource_id": "patient-123"
}
```

**Response 200:** De-identified resource.

---

### `POST /process/everything`

**Role:** `analyst`

Fetch a patient's full `$everything` bundle from a FHIR server, de-identify all resources, and return the result.

**Request:**
```json
{
  "server_url": "https://my-fhir-server.example.com/fhir",
  "patient_id": "patient-123"
}
```

**Response 200:** De-identified Bundle.

---

### `POST /process/and-upload`

**Role:** `admin`

Fetch, de-identify, and upload a resource to a target FHIR server in one call.

**Request:**
```json
{
  "source_url": "https://source-fhir.example.com/fhir",
  "target_url": "https://target-fhir.example.com/fhir",
  "resource_type": "Patient",
  "resource_id": "patient-123"
}
```

**Response 200:**
```json
{"status": "uploaded", "target_id": "Patient/5e2f1a9c"}
```

---

### `POST /process/round-trip`

**Role:** `admin`

Fetch all resources from source, de-identify, and upload to target. Returns a summary.

**Request:**
```json
{
  "source_url": "https://source-fhir.example.com/fhir",
  "target_url": "https://target-fhir.example.com/fhir",
  "resource_types": ["Patient", "Observation", "Condition"]
}
```

**Response 200:**
```json
{"resources_fetched": 450, "resources_uploaded": 448, "errors": 2}
```

---

### `POST /process/dicom`

**Role:** `analyst`

De-identify a single DICOM file. Removes patient demographics and burned-in PHI from pixel data (where supported).

**Request:** Multipart form with DICOM file.

**Response 200:** De-identified DICOM file.

---

### `POST /process/hl7v2`

**Role:** `analyst`

De-identify an HL7 v2.x message. Applies NLP scrubbing to message segments.

**Request:**
```http
POST /process/hl7v2
Content-Type: text/plain
X-API-Key: <key>

MSH|^~\&|...
PID|1||patient-123^^^Hospital^MR||Müller^Hans^Georg...
```

**Response 200:** De-identified HL7 v2 message.

---

## 3. Async Job Queue

Long-running operations return HTTP 202 immediately with a `job_id`. Poll status with `GET /v1/jobs/{id}`.

### `POST /v1/jobs/bulk-export`

**Role:** `admin`

Start a bulk FHIR export job: fetch all resources of specified types from the source FHIR server, de-identify, and save NDJSON results.

**Request:**
```json
{
  "source_url": "https://source-fhir.example.com/fhir",
  "resource_types": ["Patient", "Observation", "Condition", "MedicationRequest"],
  "config_profile": "gpas",
  "since": "2023-01-01T00:00:00Z"
}
```

**Response 202:**
```json
{
  "job_id": "abc123",
  "status": "pending",
  "created_at": "2026-04-21T14:30:00Z"
}
```

---

### `POST /v1/jobs/cohort`

**Role:** `analyst`

De-identify a pre-defined cohort (list of patient IDs with all their linked resources).

**Request:**
```json
{
  "source_url": "https://source-fhir.example.com/fhir",
  "patient_ids": ["patient-001", "patient-002", "patient-003"],
  "config_profile": "research"
}
```

**Response 202:** Same shape as bulk-export.

---

### `POST /v1/jobs/patient-export`

**Role:** `analyst`

Fetch all resources for a single patient (`$everything`), de-identify, and save NDJSON.

**Request:**
```json
{
  "source_url": "https://source-fhir.example.com/fhir",
  "patient_id": "patient-123",
  "config_profile": "gpas"
}
```

**Response 202:** `{ "job_id": "...", "status": "pending" }`

---

### `POST /v1/jobs/batch-patient-export`

**Role:** `analyst`

Same as `patient-export` for multiple patients in a single job.

**Request:**
```json
{
  "source_url": "https://source-fhir.example.com/fhir",
  "patient_ids": ["patient-001", "patient-002"],
  "config_profile": "research"
}
```

**Response 202:** `{ "job_id": "...", "status": "pending" }`

---

### `POST /v1/jobs/risk-driven-export`

**Role:** `analyst`

Export only patients whose computed re-identification risk exceeds a threshold. Scoring must be enabled (`MEDANON_SCORING_ENABLED=true`).

**Response 202:** `{ "job_id": "...", "status": "pending" }`

---

### `POST /v1/jobs/bulk-import`

**Role:** `admin`

Upload pre-processed NDJSON to the target FHIR server. Applies topological sort and reference rewriting before upload.

**Response 202:** `{ "job_id": "...", "status": "pending" }`

---

### `POST /v1/jobs/sql-export`

**Role:** `analyst`

De-identify rows from a registered SQL source connection and save NDJSON. Connection must exist (`POST /v1/sql-connections`) and its host must be in `MEDANON_SQL_SOURCE_ALLOWED_HOSTS`.

**Request:**
```json
{
  "connection_id": "conn-abc",
  "query": "SELECT * FROM patients",
  "config_profile": "research"
}
```

**Response 202:** `{ "job_id": "...", "status": "pending" }`

---

### `POST /v1/jobs/tabular-batch`

**Role:** `analyst`

De-identify a CSV/XLSX/Parquet upload as a background job. Useful for large files that exceed the synchronous body limit.

**Response 202:** `{ "job_id": "...", "status": "pending" }`

---

### `GET /v1/jobs`

**Role:** `analyst`

List jobs with optional filters.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Filter by `pending`, `running`, `done`, `failed` |
| `type` | string | Filter by `bulk-export`, `cohort` |
| `limit` | int | Max results (default 50) |
| `offset` | int | Pagination offset |

**Response 200:**
```json
{
  "jobs": [
    {
      "job_id": "abc123",
      "type": "bulk-export",
      "status": "done",
      "created_at": "2026-04-21T14:30:00Z",
      "completed_at": "2026-04-21T14:35:12Z",
      "resource_count": 4821
    }
  ],
  "total": 12
}
```

---

### `GET /v1/jobs/{job_id}`

**Role:** `analyst`

Get the current status of a job.

**Response 200 (running):**
```json
{
  "job_id": "abc123",
  "status": "running",
  "progress": 0.42,
  "resources_processed": 2024,
  "resources_total": 4821
}
```

**Response 200 (done):**
```json
{
  "job_id": "abc123",
  "status": "done",
  "resource_count": 4821,
  "completed_at": "2026-04-21T14:35:12Z"
}
```

---

### `GET /v1/jobs/{job_id}/result`

**Role:** `analyst`

Download the de-identified NDJSON result. Only available when `status=done`.

**Response 200** (`application/x-ndjson`): NDJSON stream of de-identified resources.

**Response 404:** Job not found or result not yet available.

---

### `DELETE /v1/jobs/{job_id}`

**Role:** `analyst`

Cancel a pending or running job and remove its result.

**Response 204:** No content.

---

### `GET /v1/jobs/{job_id}/detail`

**Role:** `analyst`

Full job metadata including stages, timing, error details, and staging partition statistics.

**Response 200:**
```json
{
  "job_id": "abc123",
  "type": "bulk-export",
  "status": "done",
  "stages": [
    {"name": "fetch", "started_at": "...", "completed_at": "...", "resources": 4821},
    {"name": "process", "started_at": "...", "completed_at": "..."}
  ]
}
```

---

### `GET /v1/jobs/{job_id}/staged-stats`

**Role:** `analyst`

Two-phase staging partition statistics: how many partitions were claimed, processed, and remaining. Only meaningful for jobs using the PostgreSQL staging layer (`MEDANON_STAGING_DB_URL`).

---

### `POST /v1/jobs/{job_id}/reprocess`

**Role:** `analyst`

Re-run a completed or failed job with the same parameters. Creates a new job record.

**Response 202:** `{ "job_id": "<new-id>", "status": "pending" }`

---

### `POST /v1/jobs/{job_id}/requeue`

**Role:** `analyst`

Push a failed job back to `pending` without creating a new record. Use when the failure was transient (e.g. gPAS outage).

**Response 202:** No body.

---

### `POST /v1/jobs/{job_id}/upload-to-target`

**Role:** `analyst`

Manually trigger upload of a completed job's NDJSON result to the target FHIR server. Used when the job was run without the upload step, or to retry a failed upload.

**Response 202:** `{ "upload_job_id": "..." }`

---

### `DELETE /v1/jobs/dead`

**Role:** `analyst`

List and purge dead-lettered (permanently failed) jobs from the queue. Returns the count of jobs removed.

**Response 200:** `{ "removed": 3 }`

---

## 4. Scoring

Scoring is active when `MEDANON_SCORING_ENABLED=true`. The composite score is `privacy × utility × quality`. Privacy acts as a hard gate: if `privacy.gate=FAIL`, the composite is 0.

### `POST /v1/score`

**Role:** `analyst`

Score a single de-identified resource ad-hoc.

**Request:**
```json
{
  "original": { "resourceType": "Patient", "id": "patient-123", ... },
  "deidentified": { "resourceType": "Patient", "id": "5e2f1a9c...", ... },
  "config_profile": "gdpr",
  "manifest_entries": ["Patient.id:cryptohash", "Patient.name:redact"]
}
```

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `include_audit` | bool | Include Markdown audit report in response |

**Response 200:**
```json
{
  "composite_score": 0.78,
  "privacy": {
    "score": 0.92,
    "gate": "PASS",
    "risk_level": "low",
    "k_anonymity": 8,
    "hipaa_identifiers_remaining": 0
  },
  "utility": {
    "score": 0.81,
    "field_retention_rate": 0.73,
    "code_retention_rate": 1.0,
    "date_precision": "year"
  },
  "quality": {
    "score": 0.96,
    "structural_validity": 0.98,
    "reference_integrity": 1.0,
    "required_fields_present": 0.94
  }
}
```

---

### `POST /v1/jobs/{job_id}/score`

**Role:** `analyst`

Trigger on-demand scoring for a completed job. Scores all resources in the job result and computes aggregate statistics.

**Response 202:** Scoring started asynchronously.

---

### `GET /v1/jobs/{job_id}/score`

**Role:** `analyst`

Retrieve cached score for a completed, scored job.

**Response 200:** Same shape as `POST /v1/score` with additional fields:

```json
{
  "job_id": "abc123",
  "resource_count": 4821,
  "config_profile": "config_value_masking.yaml",
  "composite_score": 0.78,
  "privacy": { ... },
  "utility": { ... },
  "quality": { ... }
}
```

---

### `GET /v1/jobs/{job_id}/score/report`

**Role:** `analyst`

Download a Markdown audit report for a scored job.

**Response 200** (`text/markdown`): Human-readable audit report with per-field evidence.

---

## 5. Config Profiles

### `GET /v1/configs`

**Role:** `viewer`

List all available config profiles (built-in + custom).

**Response 200:**
```json
{
  "profiles": [
    {"name": "config_gdpr_eu.yaml", "source": "builtin"},
    {"name": "my_custom.yaml", "source": "database", "created_at": "2026-04-20T10:00:00Z"}
  ]
}
```

---

### `GET /v1/configs/{name}`

**Role:** `viewer`

Get the YAML content of a config profile.

**Response 200** (`application/yaml`): YAML rule file content.

---

### `POST /v1/configs`

**Role:** `admin`

Create or update a custom config profile.

**Request:**
```http
POST /v1/configs
Content-Type: application/yaml
X-API-Key: <admin-key>

rules:
  - name: "redact patient name"
    match: "Patient.name"
    action: "redact"
```

**Response 201:** Config profile created.

---

### `DELETE /v1/configs/{name}`

**Role:** `admin`

Delete a custom config profile. Built-in profiles cannot be deleted.

**Response 204:** No content.

---

### `GET /v1/configs/{name}/conflicts`

**Role:** `viewer`

Detect rules that shadow each other due to overlapping FHIRPath expressions or priority ordering.

**Response 200:**
```json
{
  "conflicts": [
    {
      "rule_a": "redact patient name",
      "rule_b": "scrub name",
      "reason": "Both match Patient.name; rule_a fires first due to higher priority"
    }
  ]
}
```

---

### `GET /v1/configs/{name}/coverage`

**Role:** `viewer`

Analyze HIPAA Safe Harbor identifier coverage: which of the 18 identifier categories are addressed by rules in this profile.

**Response 200:**
```json
{
  "covered": ["Patient.name", "Patient.birthDate", "Patient.address"],
  "uncovered": ["Patient.telecom", "Device.identifier"],
  "coverage_pct": 0.78
}
```

---

## 6. Analytics

Analytics endpoints proxy to the analytics microservice when `ANALYTICS_SERVICE_URL` is set. Otherwise they run inline.

### `POST /v1/analyse/risk`

**Role:** `analyst`

Compute k-anonymity, l-diversity, and re-identification risk scores for a set of de-identified FHIR resources.

**Request:**
```json
{
  "resources": [
    {"resourceType": "Patient", "id": "5e2f1a9c...", "gender": "male", "birthDate": "1951"},
    ...
  ],
  "quasi_identifiers": ["gender", "birthDate", "address.postalCode"]
}
```

**Response 200:**
```json
{
  "k_anonymity": 8,
  "l_diversity": 3.2,
  "risk_level": "low",
  "at_risk_records": 12,
  "total_records": 4821
}
```

---

### `POST /v1/generate/synthetic`

**Role:** `analyst`

Generate synthetic FHIR data statistically similar to the input dataset.

**Request:**
```json
{
  "resources": [...],
  "count": 1000,
  "method": "stdlib"
}
```

`method` options: `stdlib` (always available), `sdv` (requires the `sdv` package in the analytics container).

**Response 200:** Array of synthetic FHIR resources.

---

## 7. Processing Run History

Processing runs are stored when `MEDANON_SCORING_ENABLED=true`.

### `GET /v1/processing-runs`

**Role:** `analyst`

List processing runs with pagination.

**Query parameters:** `limit`, `offset`, `endpoint` (filter by endpoint path).

**Response 200:**
```json
{
  "runs": [
    {
      "id": "run-xyz",
      "endpoint": "/v1/process",
      "config_profile": "config_gdpr_eu.yaml",
      "resource_count": 1,
      "composite_score": 0.85,
      "created_at": "2026-04-21T15:00:00Z"
    }
  ],
  "total": 842
}
```

---

### `GET /v1/processing-runs/stats`

**Role:** `analyst`

Aggregate statistics across all processing runs.

**Response 200:**
```json
{
  "total_runs": 842,
  "avg_composite_score": 0.81,
  "by_endpoint": {
    "/v1/process": {"count": 620, "avg_score": 0.83},
    "/v1/jobs/bulk-export": {"count": 12, "avg_score": 0.78}
  },
  "by_profile": {
    "config_gdpr_eu.yaml": {"count": 400, "avg_score": 0.87}
  }
}
```

---

### `GET /v1/processing-runs/{id}`

**Role:** `analyst`

Get a single processing run by ID.

---

### `DELETE /v1/processing-runs`

**Role:** `analyst` *(known issue: should require `admin`, pending fix)*

Purge all processing run records.

**Response 204:** No content.

---

## 8. AI Agents

AI agent endpoints require `MEDANON_AI_ENABLED=true`. See [security.md § 10](../explanation/security-model.md#10-known-open-security-issues) for known PHI safety constraints.

### `GET /v1/ai/status`

**Role:** `viewer`

Check AI agent availability.

**Response 200:**
```json
{
  "enabled": true,
  "model": "ollama/llama3.2",
  "provider_status": "available"
}
```

---

### `POST /v1/ai/generate-config`

**Role:** `admin`

Generate a YAML config profile from a natural-language description. Bundled profiles are injected as few-shot prompt context (not vector retrieval), one by default (`MEDANON_AI_FEWSHOT_PROFILES`).

**Request:**
```json
{
  "description": "De-identify EHR data for IRB-approved cardiovascular research. Retain dates to year-month precision. Pseudonymize patient IDs reversibly."
}
```

**Response 200** (streaming SSE or JSON):
```json
{
  "config_yaml": "rules:\n  - name: ...",
  "rationale": "Generated based on the value-masking profile. Dates preserved to year-month for temporal analysis, IDs pseudonymized via gPAS.",
  "profile_basis": "config_value_masking.yaml"
}
```

---

### `POST /v1/ai/detect-pii`

**Role:** `analyst`

3-layer PII detection: regex → NER → LLM. The LLM layer **must** use a local model (`MEDANON_AI_PII_PROVIDER`).

**Request:**
```json
{
  "text": "Patient Hans Müller, DOB 1951-08-14, admitted to Charité Berlin.",
  "resource_type": "Patient"
}
```

**Response 200:**
```json
{
  "entities": [
    {"type": "PERSON", "text": "Hans Müller", "start": 8, "end": 20, "confidence": 0.97},
    {"type": "DATE", "text": "1951-08-14", "start": 26, "end": 36, "confidence": 0.99},
    {"type": "LOCATION", "text": "Charité Berlin", "start": 51, "end": 65, "confidence": 0.91}
  ],
  "layers_used": ["regex", "ner", "llm"]
}
```

---

### `POST /v1/ai/scan-fields`

**Role:** `analyst`

Classify FHIR field paths in a sample resource as likely PHI-bearing and suggest appropriate de-identification actions. Useful for bootstrapping a config profile for a new resource type.

**Request:**
```json
{
  "resource": {"resourceType": "Patient", "id": "p1", "name": [{"family": "Müller"}], "birthDate": "1951-08-14"},
  "resource_type": "Patient"
}
```

**Response 200:**
```json
{
  "fields": [
    {"path": "Patient.name", "phi_type": "PERSON", "confidence": 0.97, "suggested_action": "redact"},
    {"path": "Patient.birthDate", "phi_type": "DATE", "confidence": 0.99, "suggested_action": "generalize"}
  ]
}
```

---

### `POST /v1/ai/chat`

**Role:** `analyst`

Conversational interface for config-building questions. Ask natural-language questions about de-identification strategy; the agent has access to the active config profile and bundled examples.

**Request:**
```json
{
  "message": "Should I use cryptohash or gpas_pseudonymize for patient IDs?",
  "config_profile": "research",
  "history": []
}
```

**Response 200** (or SSE stream):
```json
{
  "reply": "Use gpas_pseudonymize when you need to reverse the pseudonym later..."
}
```

---

### `POST /v1/ai/explain`

**Role:** `analyst`

Generate a plain-language explanation of a config profile's rules. Supports SSE streaming.

**Request:**
```json
{
  "config_profile": "gdpr",
  "rule_name": "pseudonymize patient ID"
}
```

**Response 200** (JSON or SSE stream):
```json
{
  "explanation": "This rule replaces the Patient's ID with an HMAC-SHA3-256 hash keyed with MEDANON_HASH_KEY. The hash cannot be reversed without the key, satisfying GDPR Art. 4(5) pseudonymization. The same ID will always produce the same hash, which allows cross-resource linkage within the same dataset."
}
```

---

### `POST /v1/ai/compliance`

**Role:** `analyst`

Analyze a config profile against regulatory frameworks and return compliance gaps.

**Request:**
```json
{
  "config_profile": "gdpr",
  "frameworks": ["GDPR", "HIPAA"]
}
```

**Response 200:**
```json
{
  "compliance_score": 0.91,
  "gaps": [
    {
      "framework": "HIPAA",
      "article": "45 CFR §164.514(b)(2)(i)",
      "description": "Dates not generalized to year-only - GDPR profile retains month and day.",
      "severity": "medium"
    }
  ],
  "satisfied": ["GDPR Art. 4(5)", "GDPR Art. 89(1)"]
}
```

---

## 9. FHIR Operations

These endpoints interact with a remote FHIR server. All `server_url` values are SSRF-validated.

### `POST /fhir/$export`

**Role:** `admin`

Trigger a FHIR Bulk Data Access export on the source FHIR server and stream de-identified NDJSON.

### `POST /fhir/Patient/$export`

**Role:** `analyst`

Patient-level bulk export (all resources for all patients matching an optional `_type` filter).

### `POST /fhir/Group/{id}/$export`

**Role:** `admin`

Group-level bulk export.

### Subscription Management

| Endpoint | Role | Description |
|---|---|---|
| `GET /fhir/Subscription` | admin | List active subscriptions |
| `POST /fhir/Subscription` | admin | Create a subscription |
| `GET /fhir/Subscription/{id}` | analyst | Get subscription details |
| `PUT /fhir/Subscription/{id}` | analyst | Update subscription |
| `DELETE /fhir/Subscription/{id}` | analyst | Delete subscription |

---

## 10. Audit Log

### `GET /v1/audit`

**Role:** `admin`

Query recent audit events from the Redis Stream. Returns newest-first.

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `count` | int | Max events to return (default 100) |
| `event_type` | string | Filter by event type (e.g. `process`, `job.create`, `auth.deny`) |

**Response 200:**
```json
{
  "events": [
    {
      "ts": "2026-04-21T14:30:00Z",
      "event": "process",
      "actor": "api-key-user",
      "auth_method": "api-key",
      "outcome": "success",
      "_id": "1745245800000-0"
    }
  ]
}
```

Returns empty array when Redis is not configured.

---

## 11. Multi-Format De-identification

All of these accept `?config_profile=` to route through the [rule engine](./rules.md);
DICOM and HL7 v2 default to a built-in standards-based scrub when no profile is given.

| Endpoint | Role | Description |
|---|---|---|
| `POST /v1/process/dicom` | analyst | De-identify one DICOM object (PS 3.15 default) |
| `POST /v1/process/dicom/batch` | analyst | De-identify multiple DICOM objects |
| `POST /v1/process/hl7v2` | analyst | De-identify an HL7 v2 message (segment scrub default) |
| `POST /v1/process/hl7v2/batch` | analyst | De-identify an HL7 v2 batch |
| `POST /v1/process/cda` | analyst | De-identify a CDA/CCDA document |
| `POST /v1/process/tabular/inspect` | analyst | Discover columns + types in a CSV/XLSX/Parquet upload |
| `POST /v1/process/tabular` | analyst | De-identify a table using `column:` rules (profile or inline `rules`) |

See [Tabular & SQL](../how-to/deidentify-tabular.md), [DICOM](../how-to/deidentify-dicom.md),
[HL7 v2](../how-to/deidentify-hl7v2.md), and [CDA](../how-to/deidentify-cda.md).

## 12. SQL Source Connector

**Role:** `admin` (connection management). Hosts must be in `MEDANON_SQL_SOURCE_ALLOWED_HOSTS`.

| Endpoint | Description |
|---|---|
| `POST /v1/sql-connections` | Register a relational connection |
| `GET /v1/sql-connections` | List connections |
| `POST /v1/sql-connections/{id}/test` | Test connectivity |
| `DELETE /v1/sql-connections/{id}` | Remove a connection |
| `POST /v1/process/sql/inspect` | Reflect schema (tables/columns) |
| `POST /v1/jobs/sql-export` | Async de-identified export (poll like any job) |

## 13. DAG Workflows

See [Run workflows](../how-to/run-workflows.md). No additional profile required.

| Endpoint | Role | Description |
|---|---|---|
| `POST /v1/workflows` | analyst | Submit a workflow from a DAG definition |
| `POST /v1/workflows/template` | analyst | Submit from a built-in template |
| `GET /v1/workflows` | analyst | List workflows |
| `GET /v1/workflows/{id}` | analyst | Status + per-stage progress |
| `DELETE /v1/workflows/{id}` | analyst | Cancel a workflow |

## 14. Trust Profiles

Trust Profiles bundle Trust Gate audit configuration (which phases to run, sector targets, intended use) into reusable named profiles stored in the database. Requires the `trust` profile (`docker compose --profile trust up`).

| Endpoint | Role | Description |
|---|---|---|
| `GET /v1/trust-profiles/_phases` | viewer | List available audit phases and their descriptions |
| `GET /v1/trust-profiles` | viewer | List all trust profiles |
| `GET /v1/trust-profiles/{name}` | viewer | Get a specific profile |
| `POST /v1/trust-profiles` | admin | Create a trust profile |
| `PUT /v1/trust-profiles/{name}` | admin | Update a trust profile |
| `DELETE /v1/trust-profiles/{name}` | admin | Delete a trust profile |

**Example  create a trust profile:**
```bash
curl -X POST http://localhost:8000/v1/trust-profiles \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: $ADMIN_KEY' \
  -d '{
    "name": "research-icu",
    "phases": ["conformance", "completeness", "plausibility"],
    "sector": "hospital",
    "intended_use": "research"
  }'
```

## 15. Governance & EHDS (TEHDAS2 D7.2)

Governance endpoints for regulated secondary-use release. All are additive and inert unless used; the advisory ones evaluate-only (no transformation) and run locally regardless of any microservice split. See [Architecture § Governance & EHDS compliance](../explanation/architecture.md#governance--ehds-compliance).

| Endpoint | Role | Description |
|---|---|---|
| `POST /v1/minimise/assess` | analyst | Data-minimisation report (D7.2 §3). Body: a FHIR payload; optional `?declared_paths=` for purpose-limitation flagging. Returns direct/quasi identifier classification, granularity recommendations, and special-category (Art 9) flags. No transformation is performed. |
| `POST /v1/export/decision` | admin | Ad-hoc Five-Safes disclosure decision (D7.2 §5.4). Body: `resources`, optional `synthetic`, `declared_paths`, `thresholds`, `permit_id`, `recipient`. Returns `{ "decision": "release" \| "refer" \| "refuse", "reasons": [...] }` (most restrictive rule wins; REFER escalates to REFUSE in regulated mode). An inactive/revoked permit is reported as a documented REFUSE, not an HTTP error; an unknown `permit_id` returns 422. |
| `POST /v1/exposure/assess` | analyst | Cumulative-exposure / differencing risk (D7.2 §5.5.7). Compares a release's population against prior releases to the same permit/recipient. Input ids are pseudonymous; only keyed one-way fingerprints are stored (optional durable ledger). |
| `POST /v1/export/statistical` | analyst | Anonymised statistical-format release answering an EHDS data request (D7.2 §5.5.4). Returns group-by counts protected by small-cell suppression and/or differential privacy. Aggregate-only; no record-level data leaves the layer. |
| `POST /v1/catalog/descriptor` | analyst | Emits a HealthDCAT-AP JSON-LD `dcat:Dataset` descriptor (D7.2 §4.3, EHDS Art 55/78), optionally enriched inline or by `job_id` from the passport store. Discovery metadata only. |
| `POST /v1/synthetic/passport` | analyst | Synthetic Data Passport (fidelity + privacy verdict). Proxy to the analytics microservice when `ANALYTICS_SERVICE_URL` is set, else in-process. |
| `POST /v1/analyse/privacy-risk` | analyst | Privacy-risk assessment (re-id + DCR/NNDR + attribute inference). Same proxy behavior as above. |
| `GET /v1/permits`, `POST /v1/permits` | admin | Data-permit lifecycle (D7.2 §2 / EHDS Arts 45-49). Create always starts in `draft` (client-supplied status ignored). |
| `POST /v1/permits/{id}/{submit\|approve\|reject\|revoke}` | admin | Permit state-machine transitions. An illegal transition returns 409; an unknown id returns 404. |
| `GET /v1/permits/{id}` | admin | Read one permit. |
| `GET /v1/reports`, `GET /v1/reports/{job_id}` | analyst | Durable Transformation Passports from risk-driven exports (D7.2 §5.5.1 / Art 79). Anonymous by construction. Requires `MEDANON_APP_DB_URL`; without it the list is empty and per-job passports remain on `GET /v1/jobs/{id}`. |

**Connectors & settings:** `/v1/source-connections` and `/v1/output-destinations` (analyst: list/test; admin: create/delete) manage encrypted dataspace connectors, FHIR sources + S3 destinations; tokens never returned. `/v1/settings` (admin) reads/writes deployment-wide instance defaults. `GET /v1/runtime-config` is open and returns only the non-secret routing slice the SPA needs pre-login (active source/target ids, `builtin_fhir_enabled`). See [Integrate a connector](../how-to/integrate-connector.md#built-in-connectors-configurable-input-always-s3-output) for the request/response walkthrough.

## 16. API Keys

**Role:** `admin`. Per-client keys are hashed at rest (`PostgresApiKeyStore`). See
[Configure authentication](../how-to/configure-authentication.md).

| Endpoint | Description |
|---|---|
| `POST /v1/api-keys` | Issue a key (returned once) |
| `GET /v1/api-keys` | List keys (metadata only) |
| `GET /v1/api-keys/{id}` | Get key metadata |
| `POST /v1/api-keys/{id}/rotate` | Rotate a key |
| `DELETE /v1/api-keys/{id}` | Revoke a key |

### SMART on FHIR

| Endpoint | Description |
|---|---|
| `GET /.well-known/smart-configuration` | SMART discovery document |
| `POST /oauth2/introspect` | Token introspection |

## 17. Admin & Dashboard

| Endpoint | Role | Description |
|---|---|---|
| `POST /v1/admin/cache/flush` | admin | Flush the gPAS/result caches |
| `GET /v1/admin/cache/canary` | admin | gPAS cache-coherence probe |
| `GET /v1/dashboard/summary` | viewer | BFF aggregation for the UI Home dashboard |

---

## Quick-Start Examples

### De-identify a single Patient resource

```bash
curl -s http://localhost:8000/process?config_profile=gdpr \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{"resourceType":"Patient","id":"p1","name":[{"family":"Müller"}],"birthDate":"1951-08-14"}'
```

### Run a bulk export job and download the result

```bash
# 1. Start the job
JOB=$(curl -s -X POST http://localhost:8000/v1/jobs/bulk-export \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{"source_url":"http://fhir-source/fhir","resource_types":["Patient","Observation"],"config_profile":"gpas"}')

JOB_ID=$(echo $JOB | jq -r .job_id)

# 2. Poll until done
while true; do
  STATUS=$(curl -s http://localhost:8000/v1/jobs/$JOB_ID -H "X-API-Key: $MEDANON_API_KEY" | jq -r .status)
  echo "Status: $STATUS"
  [ "$STATUS" = "done" ] && break
  [ "$STATUS" = "failed" ] && exit 1
  sleep 5
done

# 3. Download
curl -s http://localhost:8000/v1/jobs/$JOB_ID/result \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -o deidentified.ndjson
```

### Score a de-identified resource

```bash
curl -s -X POST http://localhost:8000/v1/score?include_audit=true \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{
    "original": {"resourceType":"Patient","id":"p1","name":[{"family":"Müller"}]},
    "deidentified": {"resourceType":"Patient","id":"5e2f1a9c...","name":[]},
    "config_profile": "gdpr"
  }'
```

### Generate a config profile with AI

```bash
curl -s -X POST http://localhost:8000/v1/ai/generate-config \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $ADMIN_API_KEY" \
  -d '{"description":"GDPR-compliant profile for cardiovascular research. Keep diagnosis codes and measurement values. Generalize dates to year-month."}'
```

---

## OpenAPI / Swagger UI

The full OpenAPI 3.1 schema is available at:

- `/openapi.json`, machine-readable schema
- `/docs`, interactive Swagger UI
- `/redoc`, ReDoc documentation viewer

These endpoints do not require authentication.
