# Connector Integration

How to connect SPE FHIR BlackBox to a dataspace connector  Eclipse Dataspace Components (EDC), FIWARE/NGSI-LD, or any HTTP-based connector  for privacy-preserving FHIR data exchange.

---

## Integration Patterns

| Pattern | When to use |
|---|---|
| **A  Pre-built asset** | Connector pulls a pre-built NDJSON file. Zero runtime coupling between MedAnon and the connector. Best for scheduled batch transfers. |
| **B  Round-trip** | MedAnon fetches from source FHIR, de-identifies in-flight, pushes to target FHIR. Connector negotiates access on either end. |
| **C  EDC HTTP Data Plane** | EDC's HTTP Data Plane calls MedAnon as a transformation proxy. De-identification happens transparently inside the EDC transfer. |
| **D  FIWARE/NGSI-LD** | MedAnon acts as a NGSI-LD Context Broker middleware that de-identifies FHIR-to-NGSI-LD entity payloads before forwarding. |

---

## Built-in connectors: configurable input, always-S3 output

The lowest-friction way to wire MedAnon into a dataspace is the built-in connector
configuration. You save two things once, then reference them by id on any export
job:

- **Input source**: where data comes from (a FHIR server URL plus an optional
  bearer token). The token is encrypted at rest and resolved inside the worker at
  fetch time; it is never stored in the job record.
- **S3 output destination**: the bucket the de-identified file is delivered to
  (endpoint, region, bucket, key-prefix template, access key, and an encrypted
  secret key). The de-identified file is always delivered to S3.

Manage them under Configure -> Dataspace Connectors (admin) in the UI, or via the
API. Both stores require `MEDANON_APP_DB_URL` (PostgreSQL) and encrypt secrets
with `MEDANON_SQL_CRED_KEY`.

### 1. Save an input source and an S3 destination

```bash
# Input source (FHIR server). Returns {"id": "<source_id>", ...}
curl -s -X POST http://localhost:8000/v1/source-connections \
  -H 'Content-Type: application/json' \
  -d '{"name":"Provider HAPI","server_url":"https://fhir.example.org/fhir","token":"eyJ..."}'

# S3 output destination. Returns {"id": "<destination_id>", ...}
curl -s -X POST http://localhost:8000/v1/output-destinations \
  -H 'Content-Type: application/json' \
  -d '{
        "name":"Dataspace bucket",
        "endpoint":"s3.eu-central-1.amazonaws.com",
        "region":"eu-central-1",
        "bucket":"dataspace-deidentified",
        "access_key":"AKIA...",
        "secret_key":"...",
        "key_prefix":"{permit_id}/{ts}/"
      }'

# Optional pre-flight checks (probe the FHIR server / ensure the bucket exists):
curl -s -X POST http://localhost:8000/v1/source-connections/<source_id>/test
curl -s -X POST http://localhost:8000/v1/output-destinations/<destination_id>/test
```

The `key_prefix` is an object-key template. Supported tokens: `{job_id}`,
`{permit_id}`, `{ts}`, `{resource_type}`. A trailing `/` (or an empty prefix)
appends `{job_id}.ndjson`.

### 2. Reference them on an export job

```bash
curl -s -X POST http://localhost:8000/v1/jobs/bulk-export \
  -H 'Content-Type: application/json' \
  -d '{"source_id":"<source_id>","destination_id":"<destination_id>"}'
```

When the job finishes and passes the score/PII gate, the NDJSON is delivered to
`s3://dataspace-deidentified/<rendered-key>`. `source_id` and `destination_id` are
supported on `bulk-export`, `cohort`, `patient-export`, `batch-patient-export`,
and `risk-driven-export`.

### 3. Guarantee the file always lands in S3

Set `MEDANON_REQUIRE_S3_DELIVERY=true` in the dataspace deployment. A job with no
resolvable destination then fails closed instead of leaving the output only in the
internal result store. To pin every job to one destination without passing
`destination_id` each time, set `MEDANON_DEFAULT_DESTINATION_ID=<destination_id>`.

Delivery runs only after the output barrier passes, so a blocked or leaky job is
never delivered to the dataspace bucket.

---

## Pattern A  Pre-built NDJSON Asset

```
FHIR Server → MedAnon bulk export → NDJSON file → Connector asset → Consumer
```

This pattern separates de-identification from data access. MedAnon runs a bulk export job, saves the de-identified NDJSON, and the connector serves it as a static asset. The consumer downloads the file at negotiation time.

### 1. Run a bulk export job

```bash
# Submit the job
JOB=$(curl -s -X POST http://localhost:8000/v1/jobs/bulk-export \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{
    "source_url": "http://hapi-fhir:8080/fhir",
    "resource_types": ["Patient", "Observation", "Condition"],
    "config_profile": "gdpr"
  }')

JOB_ID=$(echo $JOB | jq -r .job_id)
echo "Job: $JOB_ID"

# Poll until done
until [ "$(curl -s http://localhost:8000/v1/jobs/$JOB_ID -H "X-API-Key: $MEDANON_API_KEY" | jq -r .status)" = "done" ]; do
  sleep 5
done

# Download the result
curl -s http://localhost:8000/v1/jobs/$JOB_ID/result \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -o /output/deidentified.ndjson

echo "De-identified NDJSON written to /output/deidentified.ndjson"
```

### 2. Expose the asset via HTTP (MinIO / nginx)

With the `--profile s3` Docker Compose option, MinIO is available at `http://minio:9000`. Upload the asset:

```bash
mc alias set medanon http://localhost:9000 $MINIO_ROOT_USER $MINIO_ROOT_PASSWORD
mc cp /output/deidentified.ndjson medanon/exports/deidentified.ndjson
```

Or serve via nginx (or any static file server) and register the URL as a connector asset.

### 3. Register as an EDC asset (example)

```bash
curl -s -X POST http://edc-control-plane:8181/management/v2/assets \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $EDC_API_KEY" \
  -d '{
    "@context": {"@vocab": "https://w3id.org/edc/v0.0.1/ns/"},
    "@id": "urn:asset:fhir-cardiovascular-cohort-gdpr",
    "properties": {
      "name": "Cardiovascular Cohort  GDPR de-identified",
      "description": "FHIR R4 Patient, Observation, Condition resources. De-identified per GDPR Art. 4(5).",
      "contenttype": "application/x-ndjson",
      "version": "2026-04-21"
    },
    "dataAddress": {
      "@type": "HttpData",
      "baseUrl": "http://minio:9000/exports/deidentified.ndjson"
    }
  }'
```

### 4. Attach a usage policy and contract definition

```bash
# Usage policy  allow only research purposes
curl -s -X POST http://edc-control-plane:8181/management/v2/policydefinitions \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $EDC_API_KEY" \
  -d '{
    "@id": "research-only-policy",
    "policy": {
      "@type": "odrl:Set",
      "odrl:permission": [{
        "odrl:action": "USE",
        "odrl:constraint": {
          "odrl:leftOperand": "purpose",
          "odrl:operator": {"@id": "odrl:eq"},
          "odrl:rightOperand": "research"
        }
      }]
    }
  }'

# Contract definition linking asset to policy
curl -s -X POST http://edc-control-plane:8181/management/v2/contractdefinitions \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: $EDC_API_KEY" \
  -d '{
    "@id": "fhir-cohort-contract",
    "accessPolicyId": "research-only-policy",
    "contractPolicyId": "research-only-policy",
    "assetsSelector": [{
      "@type": "CriterionDto",
      "operandLeft": "@id",
      "operator": "=",
      "operandRight": "urn:asset:fhir-cardiovascular-cohort-gdpr"
    }]
  }'
```

---

## Pattern B  Server-to-Server Round-Trip

```
Source FHIR (authenticated) → MedAnon → Target FHIR (de-identified) ← Consumer Connector
```

MedAnon reads from the source FHIR server, de-identifies in memory, and writes to the target FHIR server. The consumer connector points to the target server to negotiate access.

```bash
curl -X POST http://localhost:8000/process/round-trip \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d '{
    "source_url": "http://source-fhir:8080/fhir",
    "target_url": "http://target-fhir:8080/fhir",
    "resource_types": ["Patient", "Condition", "Observation"],
    "config_profile": "structural"
  }'
```

The source FHIR server (`fhir-server`) has no host port  it is only reachable from within the Docker `source-net`. The target FHIR server (`fhir-target`, port 8082) is the publicly accessible de-identified store.

**Set FHIR server credentials in `.env`:**
```bash
FHIR_SOURCE_TOKEN=Bearer <token>    # Bearer token for source FHIR
FHIR_TARGET_TOKEN=Bearer <token>    # Bearer token for target FHIR
```

---

## Pattern C  EDC HTTP Data Plane Proxy

In this pattern, the EDC HTTP Data Plane calls MedAnon as a **transformation proxy** during data transfer. EDC fetches raw FHIR from the provider, forwards it to MedAnon's `/process` endpoint, and delivers the de-identified response to the consumer.

### Architecture

```
Provider EDC ──(fetch raw FHIR)──▶ Provider FHIR Server
     │
     │ (raw FHIR payload)
     ▼
MedAnon /process                   ← EDC Data Plane calls this
     │
     │ (de-identified FHIR)
     ▼
Consumer EDC ──▶ Consumer Data Plane ──▶ Consumer Storage
```

### EDC Data Plane configuration

Register MedAnon as a transformation step in the EDC `HttpDataPlane`:

```json
{
  "sourceDataAddress": {
    "@type": "HttpData",
    "baseUrl": "http://provider-fhir:8080/fhir/Patient/patient-123"
  },
  "destinationDataAddress": {
    "@type": "HttpProxy"
  },
  "transformationAddress": {
    "@type": "HttpData",
    "baseUrl": "http://medanon:8000/process",
    "headers": {
      "X-Api-Key": "<medanon-api-key>",
      "Content-Type": "application/json"
    },
    "queryParams": "config_profile=gdpr"
  }
}
```

**Note:** Not all EDC distributions support the `transformationAddress` extension. Check your EDC version's data plane documentation. The alternative is to use a custom `DataFlowController` that chains the FHIR fetch → MedAnon call → delivery.

### Inline transformation via EDC custom extension

```java
// Custom EDC DataFlowController (Java/Kotlin)
public class MedanonTransformController implements DataFlowController {
    private final String medanonUrl;
    private final String apiKey;

    @Override
    public TransferInitiateResponse initiateFlow(DataFlowRequest request) {
        // 1. Fetch raw FHIR from source
        String rawFhir = fetchFhir(request.getSourceDataAddress());

        // 2. Call MedAnon /process
        String deidentified = callMedanon(medanonUrl + "/process", rawFhir, apiKey);

        // 3. Deliver to consumer
        deliverToConsumer(request.getDestinationDataAddress(), deidentified);

        return TransferInitiateResponse.success();
    }
}
```

---

## Pattern D  FIWARE/NGSI-LD Middleware

In FIWARE dataspaces, MedAnon acts as a middleware between a FHIR source and an NGSI-LD Context Broker. FHIR resources are de-identified by MedAnon before being converted to NGSI-LD entity format and published to the broker.

### Architecture

```
FHIR Source Server
      │
      ▼
MedAnon /process                   ← de-identification
      │
      ▼
FHIR→NGSI-LD converter             ← FHIR R4 → NGSI-LD entity mapping
      │
      ▼
FIWARE Context Broker (Orion-LD)
      │
      ▼
Consumer (via NGSI-LD subscription or query)
```

### Step 1: De-identify the FHIR resource

```bash
DEIDENTIFIED=$(curl -s -X POST http://localhost:8000/process?config_profile=gdpr \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $MEDANON_API_KEY" \
  -d @patient.fhir.json)

echo $DEIDENTIFIED | jq .
```

### Step 2: Convert to NGSI-LD entity

Map FHIR fields to NGSI-LD properties. Example mapping for a de-identified Patient:

```python
import json

deidentified = json.loads(deidentified_fhir)

ngsi_entity = {
    "id": f"urn:ngsi-ld:Patient:{deidentified['id']}",
    "type": "Patient",
    "@context": [
        "https://uri.etsi.org/ngsi-ld/v1/ngsi-ld-core-context.jsonld",
        "https://healthdataspace.example.eu/fhir-ngsi-ld-context.jsonld"
    ],
    "gender": {"type": "Property", "value": deidentified.get("gender")},
    "birthYear": {"type": "Property", "value": deidentified.get("birthDate")},
    # name, address, telecom are redacted in GDPR profile  not mapped
}
```

### Step 3: Publish to Orion-LD Context Broker

```bash
curl -s -X POST http://orion-ld:1026/ngsi-ld/v1/entities \
  -H "Content-Type: application/ld+json" \
  -H "NGSILD-Tenant: health-research" \
  -d "$NGSI_ENTITY"
```

### FIWARE Dataspace connector (TrueConnector)

If using FIWARE's TrueConnector (IDS-compatible), register the Orion-LD entity as a resource:

```json
{
  "@type": "ids:Resource",
  "@id": "urn:resource:fhir-cohort-ngsi-ld",
  "ids:title": [{"@value": "FHIR Cardiovascular Cohort  GDPR de-identified (NGSI-LD)", "@language": "en"}],
  "ids:description": [{"@value": "NGSI-LD entities derived from FHIR R4 Patient, Observation, Condition. De-identified per GDPR Art. 4(5).", "@language": "en"}],
  "ids:resourceEndpoint": [{
    "@type": "ids:ConnectorEndpoint",
    "ids:accessURL": {"@id": "http://orion-ld:1026/ngsi-ld/v1/entities?type=Patient"}
  }]
}
```

---

## Authentication Summary

| Interface | Auth mechanism | Configuration |
|---|---|---|
| MedAnon API | `X-API-Key: <key>` header | `MEDANON_API_KEY` in `.env` |
| Source FHIR server | Bearer token | `FHIR_SOURCE_TOKEN=Bearer <token>` |
| Target FHIR server | Bearer token | `FHIR_TARGET_TOKEN=Bearer <token>` |
| EDC control plane | API key | Per-EDC distribution config |
| Orion-LD Context Broker | Per-tenant header | `NGSILD-Tenant` header + optional OAuth2 |
| MinIO (S3) | Access key / secret | `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` |

---

## Operational Notes

- **Streaming NDJSON:** All NDJSON responses are streamed one resource per line. Consumers can begin processing before the transfer completes.
- **Config profile selection:** Use `?config_profile=<name>` to select the de-identification profile per request. The profile determines which fields are pseudonymized, generalized, or redacted.
- **Network isolation:** The source FHIR server has no host-accessible port. Only MedAnon and the worker container bridge the `source-net`. External systems never reach identified data directly.
- **Reference integrity:** When processing bundles via `/process/ndjson` or bulk export, MedAnon rewrites all cross-resource references (e.g. `Patient/patient-123` → `Patient/5e2f1a9c...`) after all IDs are transformed.
- **Large datasets:** Use the async job queue (`/v1/jobs/bulk-export`) for cohorts larger than a few thousand resources. The synchronous `/process/round-trip` is best for ad-hoc or small transfers.
- **SSRF protection:** All user-supplied `server_url` values are SSRF-validated before the connection is attempted. See [security.md § 4.3](security.md#43-ssrf-protection).
