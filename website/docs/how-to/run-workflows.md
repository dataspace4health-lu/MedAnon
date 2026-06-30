---
title: "Run DAG Workflows"
sidebar_position: 7
description: "Compose multi-stage de-identification workflows with the workflow API."
---

# Run DAG Workflows

Workflows let you compose multi-stage pipelines (a DAG of stages) and run them
as a single logical operation — for example: fetch, de-identify, score, and upload.
The workflow API is available without any additional profile; the `/v1/workflows`
endpoints are part of the always-on `anonymizer` service.

## Submit a workflow

```bash
# From an explicit DAG definition
curl -X POST http://localhost:8000/v1/workflows \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: $MEDANON_API_KEY' \
  -d '{"name":"cohort-export","stages":[ ... ]}'

# From a built-in template
curl -X POST http://localhost:8000/v1/workflows/template \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: $MEDANON_API_KEY' \
  -d '{"template":"bulk-deidentify","params":{"config_profile":"my_profile"}}'
```

**Role:** `analyst` for both submission endpoints.

## Monitor and cancel

```bash
curl http://localhost:8000/v1/workflows \
  -H 'X-API-Key: $MEDANON_API_KEY'                       # list all workflows

curl http://localhost:8000/v1/workflows/{workflow_id} \
  -H 'X-API-Key: $MEDANON_API_KEY'                       # status + per-stage progress

curl -X DELETE http://localhost:8000/v1/workflows/{workflow_id} \
  -H 'X-API-Key: $MEDANON_API_KEY'                       # cancel
```

## Endpoints summary

| Endpoint | Role | Description |
|---|---|---|
| `POST /v1/workflows` | analyst | Submit a workflow from a DAG definition |
| `POST /v1/workflows/template` | analyst | Submit from a built-in template |
| `GET /v1/workflows` | analyst | List workflows |
| `GET /v1/workflows/{id}` | analyst | Status and per-stage progress |
| `DELETE /v1/workflows/{id}` | analyst | Cancel a workflow |
