# Connector Integration Guide

This document describes how to connect SPE FHIR BlackBox to a **dataspace connector**
(Eclipse Dataspace Components / EDC, FIWARE/NGSI-LD, or any HTTP-capable connector) for
privacy-preserving FHIR data exchange.

---

## Overview

Two integration patterns are supported:

| Pattern | When to use |
|---|---|
| **A — File export** | Connector pulls a pre-built NDJSON asset from a shared location (object store, mounted volume, or HTTP endpoint). Zero coupling between MedAnon and the connector at runtime. |
| **B — Server-to-server round-trip** | MedAnon fetches from a source FHIR server, de-identifies in-flight, and pushes de-identified resources to a target FHIR server. The connector negotiates access on either side. |

---

## Pattern A — File Export → Connector Asset

```
FHIR Server  ──▶  MedAnon /process/from-server  ──▶  NDJSON file
                                                         │
                                                   Connector asset
                                                         │
                                               Consumer pulls via contract
```

### Step 1 — Generate the de-identified NDJSON

```bash
curl -s -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{
    "server_url": "http://hapi-fhir:8080/fhir",
    "resource_types": ["Patient", "Observation"],
    "params": {"_count": 500}
  }' \
  > /output/deidentified.ndjson
```

The response is streaming NDJSON — one de-identified resource per line.

### Step 2 — Expose as a Connector Asset

Register `/output/deidentified.ndjson` as an asset in your connector. Example for EDC:

```json
{
  "asset": {
    "id": "fhir-deidentified-patients-v1",
    "properties": {
      "name": "De-identified FHIR Patient Cohort",
      "contenttype": "application/x-ndjson",
      "dataformat": "FHIR R4 NDJSON",
      "policy": "HIPAA Safe Harbor"
    }
  },
  "dataAddress": {
    "type": "HttpData",
    "baseUrl": "http://medanon:8000/process/from-server",
    "method": "POST",
    "body": "{\"server_url\":\"http://hapi-fhir:8080/fhir\",\"resource_types\":[\"Patient\"]}"
  }
}
```

Alternatively, point `baseUrl` to the pre-built NDJSON file if using a file-based data address.

### Step 3 — Risk Verification (optional)

Before publishing, verify the k-anonymity level meets your policy threshold:

```bash
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @/output/deidentified.ndjson \
  | python3 -c "import json,sys; r=json.load(sys.stdin); print('min_k:', r['summary']['min_k'])"
```

Reject the asset if `min_k < 5` (HIPAA) or `min_k < 3` (research).

---

## Pattern B — Server-to-Server Round-Trip

```
Source FHIR  ──▶  MedAnon /process/round-trip  ──▶  Target FHIR
     │                         │                          │
  Connector                de-identify                Connector
  contract A               in-flight                  contract B
```

### Single API call

```bash
curl -s -X POST http://localhost:8000/process/round-trip \
  -H "Content-Type: application/json" \
  -d '{
    "source_server_url": "http://source-fhir:8080/fhir",
    "target_server_url": "http://target-fhir:8080/fhir",
    "resource_types": ["Patient", "Observation", "Condition"],
    "params": {"_count": 200},
    "source_token": "Bearer <source-token>",
    "target_token": "Bearer <target-token>",
    "timeout": 60
  }'
```

Response is streaming NDJSON — one status line per resource:

```json
{"resourceType": "Patient", "target_id": "abc123", "status": "ok"}
{"resourceType": "Observation", "target_id": "def456", "status": "ok"}
{"resourceType": "Patient", "status": "error", "error": "processing error"}
```

### Environment variable configuration

For production deployments where tokens must not appear in request bodies, configure
via environment variables in `docker-compose.yml` or Helm values:

```bash
FHIR_SOURCE_URL=http://source-fhir:8080/fhir
FHIR_TARGET_URL=http://target-fhir:8080/fhir
FHIR_SOURCE_TOKEN=Bearer <token>
FHIR_TARGET_TOKEN=Bearer <token>
```

When these are set, `source_server_url` / `target_server_url` can be omitted from
the request body.

---

## EDC Integration Notes

### Provider side

1. Register a `HttpData` data address pointing to `POST /process/round-trip` or
   `POST /process/from-server`.
2. Attach a usage policy (e.g. `idsc:USE` restricted to approved connectors).
3. The EDC data plane POSTs the request body as-is to MedAnon; the streaming NDJSON
   response flows back to the consumer.

### Consumer side

1. Negotiate the contract through the EDC control plane.
2. Initiate the transfer; the EDC data plane delivers the NDJSON to the consumer's
   chosen data address (S3, Azure Blob, local HTTP sink).

### EDC data plane proxy limitation

EDC's `HttpProxy` transfer type does not support chunked streaming by default.
Use `HttpData` with a pull-based pattern (consumer pulls the completed file) rather
than trying to stream the NDJSON response through the EDC data plane in real time.

---

## FIWARE / NGSI-LD Integration Notes

MedAnon can serve as a **context source** for an NGSI-LD broker when de-identified
resources are mapped to NGSI-LD entities.

### Recommended flow

1. Call `POST /process/from-server` to get de-identified NDJSON.
2. Convert each FHIR resource to an NGSI-LD entity using your FHIR→NGSI-LD mapping.
3. `POST /ngsi-ld/v1/entities` to the Orion-LD or Scorpio broker.

FHIR Patient → NGSI-LD entity type `urn:ngsi-ld:Patient` with attributes mapped
from de-identified FHIR fields (no PHI present after step 1).

### Orion-LD example

```bash
# Step 1: de-identify
curl -s -X POST http://localhost:8000/process/from-server \
  -H "Content-Type: application/json" \
  -d '{"server_url":"http://hapi-fhir:8080/fhir","resource_types":["Patient"]}' \
  | python3 scripts/fhir_to_ngsild.py \
  | curl -s -X POST http://orion-ld:1026/ngsi-ld/v1/entities \
      -H "Content-Type: application/ld+json" \
      --data-binary @-
```

---

## Security Checklist

Before exposing de-identified data through a connector:

- [ ] `min_k ≥ 5` confirmed via `/analyse/risk` (HIPAA Safe Harbor requirement)
- [ ] Config profile matches the data-sharing agreement (HIPAA / GDPR / research)
- [ ] `MEDANON_HASH_KEY` is set to a strong secret (no plain SHA3 in production)
- [ ] `MEDANON_MANIFEST_ENABLED=true` for audit trail in `meta.tag`
- [ ] Source and target FHIR tokens are stored as secrets, not in request bodies
- [ ] SSRF: `server_url` must be a DNS name (not a raw private IP); enforced by MedAnon
- [ ] Network policy: MedAnon should only be reachable from the connector data plane,
  not the public internet
- [ ] Audit log (`/output/audit.log`) retained per your compliance retention policy

---

## Endpoint Quick Reference

| Endpoint | Use case |
|---|---|
| `POST /process` | De-identify a single resource or Bundle (request body) |
| `POST /process/from-server` | Fetch + de-identify from a FHIR server → streaming NDJSON |
| `POST /process/everything` | Fetch `$everything` for a patient + de-identify |
| `POST /process/and-upload` | De-identify a resource and push to a target FHIR server |
| `POST /process/round-trip` | Fetch source → de-identify → push target (full pipeline) |
| `POST /analyse/risk` | k-anonymity / l-diversity risk report on de-identified NDJSON |
| `POST /generate/synthetic` | Generate synthetic FHIR Patient cohort from real distributions |
