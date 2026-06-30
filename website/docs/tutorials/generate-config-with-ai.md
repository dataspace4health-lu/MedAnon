---
title: "Generate a Config with AI"
sidebar_position: 3
description: "Describe your intent in plain language and let the AI agents draft a validated config."
---

# Generate a Config with AI

This lesson uses the AI agents to draft a config from a description, review it,
and save it. The config agents see **no patient data**, only rule text and
field paths/types.

## 1. Enable AI

```bash
MEDANON_AI_ENABLED=true
MEDANON_AI_MODEL=ollama/llama3.2
MEDANON_AI_API_BASE=http://ollama:11434
```

```bash
docker compose --profile ai up -d
curl http://localhost:8000/v1/ai/status      # confirm AI is available
```

## 2. Describe what you want

```bash
curl -s -X POST http://localhost:8000/v1/ai/generate-config \
  -H 'Content-Type: application/json' \
  -d '{"description":"Redact patient and practitioner names, keep year of birth, pseudonymize MRNs, scrub free-text notes"}' \
  | python3 -m json.tool
```

The agent returns **validated YAML**. If the LLM is unavailable it falls back to
a keyword-based generator so you still get a usable starting point.

## 3. Ask why, and check compliance

```bash
# Save the YAML to gen.yaml, then:
curl -X POST http://localhost:8000/v1/ai/chat \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"message":"Is this HIPAA Safe Harbor compliant?","config_yaml":open("gen.yaml").read()}))')"

curl -X POST http://localhost:8000/v1/ai/compliance \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"yaml":open("gen.yaml").read(),"regulation":"HIPAA"}))')"
```

## 4. Save and use it

```bash
curl -X POST http://localhost:8000/v1/configs \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json;print(json.dumps({"name":"ai_generated","yaml":open("gen.yaml").read()}))')"
```

Now run data through it with `?config_profile=ai_generated`, exactly like a
hand-written profile.

## Next

- Refine the generated rules by hand → [Author rules](../how-to/author-rules.md)
- Understand the safety boundary → [Security model](../explanation/security-model.md)
