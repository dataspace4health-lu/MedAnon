---
title: "AI-Assisted Rule Creation"
sidebar_position: 2
description: "Generate, explain, and validate rules with the built-in AI agents."
---

# AI-Assisted Rule Creation

The toolkit ships AI agents that help you author and review de-identification
rules. They turn natural-language intent into a validated config, answer
questions about a config, scan a sample for PII, explain rules in plain
language, and analyse regulatory gaps.

:::warning PHI safety
The **config agents** (`generate-config`, `chat`, `explain`, `compliance`,
`scan-fields`) receive **no patient data**, only rule text, field *paths*, and
field *types*. The **PII leak detector** (`detect-pii`) does see field values and
therefore **must** run on a local/self-hosted model
(`MEDANON_AI_PII_PROVIDER`). See the [security model](../explanation/security-model.md).
:::

## Enable AI

```bash
MEDANON_AI_ENABLED=true
MEDANON_AI_MODEL=ollama/llama3.2          # or an external model for non-PHI agents
MEDANON_AI_API_BASE=http://ollama:11434
```

Start the local LLM with the `ai` profile:

```bash
docker compose --profile ai up -d
curl http://localhost:8000/v1/ai/status     # capability + provider check
```

## Generate a config from a description

```bash
curl -X POST http://localhost:8000/v1/ai/generate-config \
  -H 'Content-Type: application/json' \
  -d '{"description":"HIPAA Safe Harbor for Patient and Encounter; keep year of birth; pseudonymize MRNs"}'
```

The agent uses existing configs as few-shot examples, returns **validated YAML**,
and falls back to a keyword-based generator if the LLM is unavailable. Save the
result with `POST /v1/configs` (see [Author rules](./author-rules.md)).

## Scan a sample and get per-field suggestions

Send a PHI-free **field-path tree** (`path : <type>` lines), the agent
classifies each path as PII/not-PII and suggests an action:

```bash
curl -X POST http://localhost:8000/v1/ai/scan-fields \
  -H 'Content-Type: application/json' \
  -d '{"field_tree":"Patient.name.family : string\nPatient.birthDate : date\nPatient.gender : code"}'
```

Returns structured JSON the **Config Builder** overlays on its field tree, so you
accept suggestions per field rather than editing YAML by hand.

## Ask questions about a config

```bash
curl -X POST http://localhost:8000/v1/ai/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"Why redact birthDate instead of generalizing it?","config_yaml":"<your rules>"}'
```

The chat and field-scanner agents share one **action-selection policy**, so they
recommend consistent, utility-preserving actions instead of redacting everything.

## Explain rules and check compliance

```bash
# Plain-language explanation of a config (optionally streamed)
curl -X POST http://localhost:8000/v1/ai/explain \
  -H 'Content-Type: application/json' -d '{"yaml":"<your rules>"}'

# Gap analysis against a framework
curl -X POST http://localhost:8000/v1/ai/compliance \
  -H 'Content-Type: application/json' \
  -d '{"yaml":"<your rules>","regulation":"HIPAA"}'
```

Both degrade gracefully: when AI is unavailable, `explain` returns a static
rule description and `compliance` returns a static HIPAA checklist.

## Detect residual PII in output (local model only)

```bash
curl -X POST http://localhost:8000/v1/ai/detect-pii \
  -H 'Content-Type: application/json' \
  -d '{"resources":[ ... de-identified resources ... ]}'
```

This augments the [scoring](../explanation/scoring-system.md) `text_risk` check
with contextual LLM analysis and is enforced to a local provider.
