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
`?config_profile=<name>` to any processing endpoint to select one. When none is
specified the engine auto-selects `config.yaml` (no gPAS) or `config_gpas.yaml`
(when `GPAS_URL` is set).

## Bundled example configs

| Profile (`config/…`) | Purpose |
|---|---|
| `config.yaml` | Minimal: HMAC hash + regex scrubbing, no gPAS (default when `GPAS_URL` unset) |
| `config_gpas.yaml` | Production: gPAS pseudonymization + generalization + NLP scrubbing (default when `GPAS_URL` set) |
| `config_gdpr_eu.yaml` | GDPR Art. 4(5) HMAC pseudonymization |
| `config_hipaa_safe_harbor.yaml` | HIPAA Safe Harbor (45 CFR §164.514(b)): 18 PHI categories, dates→year, zip→3-digit |
| `config_research_pseudonymous.yaml` | IRB-grade: dates→year-month, IDs cryptohashed for longitudinal linkage |
| `config_structure_preserving.yaml` | Full FHIR structure retained; IDs via gPAS, PII→`[REDACTED]`, dates→year |
| `config_value_masking.yaml` | Fine-grained `nlp_detect_act` (entity-specific conditional NLP) + encrypt/generalize combos |
| `config_k_anonymity.yaml` | OLA-style k-anonymity lattice solver; requires the staging layer (`MEDANON_STAGING_DB_URL`) |

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
is warn-only. All eight bundled profiles pass strict validation.
