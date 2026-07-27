---
title: "Starter Configs"
sidebar_position: 5
description: "Optional example configs you can copy and adapt, not a fixed product surface."
---

# Starter Configs

:::tip Author your own
These bundled configs are **starting examples**, not the main feature. The point
of the toolkit is that you [author and modify your own rules](../how-to/author-rules.md)
- or [generate them with AI](../how-to/ai-assisted-rules.md), for whatever data
and policy you have. Treat the list below as templates to copy and adapt.
:::

A config is a YAML list of `match → action` rules (see [Rules](./rules.md)). Pass
`?config_profile=<alias>` to any processing endpoint to select one. When none is
specified the engine uses `auto`, which always resolves to `config.yaml`
(`pipeline/config/service.py::_resolve_profile`). `GPAS_URL` being set no longer
changes which profile `auto` picks - select `value-masking`, or author your own
config with `gpas_pseudonymize` rules, to get gPAS pseudonymization.

## Bundled example configs

| Alias | Profile (`config/…`) | Purpose |
|---|---|---|
| `auto` / `minimal` | `config.yaml` | Minimal: HMAC-SHA3-256 hash + regex scrubbing, no gPAS |
| `gdpr` | `config_gdpr_eu.yaml` | GDPR Art. 4(5) HMAC pseudonymization |
| `hipaa` | `config_hipaa_safe_harbor.yaml` | HIPAA Safe Harbor (45 CFR §164.514(b)): 18 PHI categories, dates→year, zip→3-digit |
| `value-masking` | `config_value_masking.yaml` | Field-complete masking via gPAS pseudonymization + fine-grained `nlp_detect_act` (entity-specific conditional NLP) |

For *which* profile to choose for a given compliance scenario, see
[De-identification Policies](../explanation/policies.md).

## Rule shape

```yaml
rules:
  - name: "redact patient name"
    match: "Patient.name"        # FHIRPath expression
    action: "redact"
    priority: 100                # optional; lower runs first (default 100)
    params:                      # action-specific; optional
      replacement: "[REDACTED]"
```

`rewrite_references: true` at the top level rewrites FHIR bundle references
after IDs change.

## Available actions

`redact`, `cryptohash`, `encrypt`, `decrypt`, `perturb`, `substitute`,
`generalize`, `scrub_text`, `mask` (5 strategies), `date_shift` (deterministic
per-subject offset), `tokenize` (format-preserving), and the NLP actions
`nlp_detect_by_path` / `nlp_detect_act`.

Rules are validated at load time against a Pydantic schema (per-action param
models + action-name + condition-operator checks). Set
`MEDANON_RULE_SCHEMA_STRICT=true` to fail loading on any violation; the default
is warn-only. All four bundled profiles pass strict validation.
