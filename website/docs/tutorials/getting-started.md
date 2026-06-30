---
title: "Getting Started"
sidebar_position: 1
description: "Bring the stack up and de-identify your first FHIR resource."
---

# Getting Started

This tutorial takes you from a clean checkout to your first de-identified FHIR
resource in about ten minutes. You will start the full stack, send one
`Patient` resource through the engine, and read the transformed output.

:::info Prerequisites
- Docker and Docker Compose
- `make`, `curl`, and a terminal
- ~4 GB free RAM (gPAS + HAPI FHIR + NLP are memory-hungry)
:::

## 1. Configure

```bash
git clone <repo>
cd privacy-toolkit
cp .env.example .env      # the defaults work for local dev
```

The default `.env` runs in **open mode** (no API key) and selects the minimal
`config.yaml` profile because `GPAS_URL` is unset. That is exactly what we want
for a first run.

## 2. Start the stack

```bash
make up                   # preflight checks + docker compose + verify
docker compose ps         # wait until services report "healthy" (~90s)
```

Then confirm the API is alive:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Open the web UI at **http://localhost:8501**.

## 3. De-identify one resource

Send a single `Patient` through `POST /process`:

```bash
curl -s -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{
    "resourceType": "Patient",
    "id": "example",
    "name": [{"family": "Mustermann", "given": ["Erika"]}],
    "birthDate": "1974-12-25",
    "telecom": [{"system": "phone", "value": "+49 30 123456"}]
  }' | python3 -m json.tool
```

You will see the name redacted, the phone scrubbed, and the birth date
generalized, the exact transformation depends on the active profile.

## 4. Make it your own

The real power is authoring your **own** rules. Override the active config per
request with `?config_profile=`, but more importantly you can write rules that
do exactly what your policy requires:

```yaml
rules:
  - match: "Patient.name"
    action: "redact"
  - match: "Patient.birthDate"
    action: "generalize"
    params: {strategy: "date_year"}
```

Continue with [Author Your First Rule](./author-your-first-rule.md) to save and
test a config, or [Generate a Config with AI](./generate-config-with-ai.md) to
draft one from a description. The bundled configs are just
[starting examples](../reference/config-profiles.md).

## 5. Tear down

```bash
make down
```

## Where to next

- **Operate it for real** → [Deploy with Docker & Kubernetes](../how-to/deploy-docker.md)
- **Drive it from code** → [REST API Reference](../reference/api.md)
- **Understand the engine** → [Architecture](../explanation/architecture.md)
- **Pick the right profile** → [De-identification Policies](../explanation/policies.md)
