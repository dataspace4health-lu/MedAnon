# MedAnon — Connector Integration

How to connect MedAnon to a dataspace connector (EDC, FIWARE/NGSI-LD, or any HTTP connector) for privacy-preserving FHIR data exchange.

---

## Integration patterns

| Pattern | When to use |
|---|---|
| **A — File export** | Connector pulls a pre-built NDJSON asset. Zero runtime coupling between MedAnon and the connector. |
| **B — Round-trip** | MedAnon fetches from source FHIR, de-identifies in-flight, pushes to target FHIR. The connector negotiates access on either side. |

---

## Pattern A — File export

```
FHIR Server → MedAnon /process/from-server → NDJSON file → Connector asset → Consumer
```

### 1. Generate de-identified NDJSON

```bash
curl -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{
    "server_url": "http://hapi-fhir:8080/fhir",
    "resource_types": ["Patient", "Observation"]
  }' > /output/deidentified.ndjson
```

### 2. Register as connector asset

Register `/output/deidentified.ndjson` (or its HTTP URL) as an asset in your connector with a data usage policy. The file can be served via any HTTP endpoint or object store.

### 3. Consumer access

The consumer connector negotiates a contract, downloads the NDJSON, and processes it. MedAnon is not involved at access time.

---

## Pattern B — Server-to-server round-trip

```
Source FHIR (authenticated) → MedAnon → Target FHIR (de-identified)
```

```bash
curl -X POST http://localhost:8000/process/round-trip \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://source-fhir:8080/fhir",
    "target_url": "http://target-fhir:8080/fhir",
    "resource_types": ["Patient", "Condition", "Observation"],
    "config_profile": "structural"
  }'
```

The target FHIR server holds de-identified data. The consumer connector points to the target server.

---

## Async bulk export (large datasets)

For large cohorts, use the async job queue:

```bash
# Submit
curl -X POST http://localhost:8000/v1/jobs/bulk-export \
  -H "Content-Type: application/json" \
  -d '{"source_url":"http://hapi-fhir:8080/fhir","config_profile":"gdpr"}'
# → {"job_id":"abc123"}

# Poll
curl http://localhost:8000/v1/jobs/abc123

# Download when status=done
curl http://localhost:8000/v1/jobs/abc123/result -o deidentified.ndjson
```

---

## Authentication

When `MEDANON_API_KEY` is set, include it in connector-to-MedAnon requests:

```
X-API-Key: <your-key>
```

For FHIR servers requiring auth, set `FHIR_SOURCE_TOKEN` / `FHIR_TARGET_TOKEN` in `.env`.

---

## Notes

- All responses are streaming NDJSON — one de-identified resource per line.
- Use `?config_profile=<name>` to select the de-identification profile per request.
- The target FHIR server (`hapi-fhir-target:8082`) is physically separate from the source — identified and de-identified data never share a database.
