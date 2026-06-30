---
title: "Rules"
sidebar_position: 1
description: "The rule model: match selectors, actions and their parameters, conditions, priority, and validation."
---

# Rules

A **rule** is the unit of de-identification. It pairs a **match** selector (which
fields) with an **action** (how to transform them). A config is an ordered list
of rules. Rules are plain YAML (readable, diffable, and auditable), and the
same model is reused across every supported data type.

```yaml
rewrite_references: true     # optional: rewrite FHIR references after IDs change
rules:
  - name: "redact patient name"   # optional label (manifests, logs)
    match: "Patient.name"         # selector - dialect depends on data type
    action: "redact"              # one of the actions below
    priority: 50                  # optional; lower = applied first (default 100)
    params:                       # action-specific; optional
      replacement: "[REDACTED]"
    condition:                    # optional; rule only fires when it holds
      field: "Patient.active"
      operator: "equals"
      value: true
```

## Rule keys

| Key | Type | Notes |
|---|---|---|
| `match` | string (**required**) | Field selector; dialect depends on data type (below) |
| `action` | string (**required**) | Must be a registered action (below) |
| `name` | string | Optional human label; appears in the manifest and logs |
| `params` | map | Action-specific parameters |
| `condition` | map | A single condition node; the rule fires only if it holds |
| `conditions` | list | Multiple condition nodes, **all** must hold (logical AND) |
| `priority` | int | Lower runs first; default `100`; ties keep YAML order (stable) |

Unknown top-level keys (e.g. a typo'd `patams`) are **rejected** by the schema.

## Match dialects (per data type)

The action set is shared across data types; only the **match selector** dialect
differs.

| Data type | Match dialect | Example |
|---|---|---|
| FHIR R4 (JSON/NDJSON/XML) | **FHIRPath** | `Patient.telecom.where(system='phone')` |
| CDA / CCDA | FHIRPath over the parsed document | `Patient.name` |
| DICOM | FHIRPath over the resource view (opt-in via `?config_profile=`) | `Patient.birthDate` |
| Tabular (CSV/XLSX/Parquet) & SQL | **`column:`** dialect | `column:patient_name`, `table:patients/column:dob` |

:::note
**HL7 v2** and **DICOM** have a built-in standards-based default scrub (HL7
segment scrubbing; DICOM PS 3.15 Annex E). Supplying `?config_profile=` routes
them through the full rule engine instead. See the per-format how-to guides.
:::

## Actions and parameters

Validated at load time against a typed schema (`pipeline/config/rule_schema.py`).
Strategy enums, numeric bounds, and required keys are enforced; actions tolerate
extra keys.

| Action | Key params (defaults) |
|---|---|
| `redact` | `replacement` |
| `mask` | `strategy` = `keep_prefix`/`keep_suffix`(default)/`keep_domain`/`keep_country_code`/`full`; `mask_char` (`*`); `keep_chars` (4); `preserve_length` (true) |
| `generalize` | `strategy` = `date_year`(default)/`date_year_month`/`date_year_instant`/`date_decade`/`age_bracket`/`number_round`/`zip_prefix`/`category`; `bracket_size` (10); `precision` (10); `prefix_length` (3); `mapping`; `unmapped` |
| `perturb` | `min`, `max` (must satisfy `max >= min`) |
| `date_shift` | `max_days` (>0); `direction` = `past`(default)/`future`/`both`; `preserve_age_bracket` (false); `anchor_path` |
| `tokenize` | `format`, `namespace`, `preserve_length` (false), format-preserving tokens |
| `substitute` | `substitute_with` (**required**) |
| `cryptohash` | `hash_type`, `secret_key`, `secret_key_env`, HMAC-SHA3-256 |
| `encrypt` / `decrypt` | `algorithm`, RSA |
| `scrub_text` | regex-based free-text scrub (no required params) |
| `nlp_scrub` / `nlp_detect` / `nlp_detect_act` | `entities`, `threshold` (0.5), `language`, `entity_actions`, `default_action`, `mode`, `html`, `base64_encoded`, `entity_priorities`, `fail_mode`, `mapping_scope` |
| `gpas_pseudonymize` / `gpas_depseudonymize` | `gpas_domain`, `gpas_operation` |

See [Actions](./actions.md) for the behaviour of each action, and
[Configuration](./configuration.md) for the keys (`MEDANON_HASH_KEY`,
`GPAS_*`, …) the crypto/gPAS actions depend on.

## Conditions

A rule with a `condition`/`conditions` block only fires when the condition
holds; otherwise the element is left untouched and a `skipped_reason:
condition_not_met` entry is added to the manifest.

```yaml
- match: "Observation.value"
  action: "redact"
  conditions:               # all must hold (AND)
    - field: "Observation.status"
      operator: "in"
      value: ["final", "amended"]
    - field: "Observation.category.coding.code"
      operator: "not_equals"
      value: "vital-signs"
```

**Operators:** `equals`, `not_equals`, `contains`, `not_contains`, `exists`,
`not_exists`, `in`, `not_in`, `matches`, `not_matches`. Nodes may nest with
`any_of` (OR) and `all_of` (AND). A malformed condition never crashes the run.
It evaluates to "does not match".

## Priority

Rules are stable-sorted by `priority` ascending (lower = applied first; default
`100`); within the same priority the YAML order is preserved. Use it to run a
specific narrow rule before a broad catch-all.

## Validation

Profiles are validated when loaded. Severity is controlled by
`MEDANON_RULE_SCHEMA_STRICT`:

- **`false` (default)**, violations are logged as warnings; the profile still
  loads.
- **`true`**, any violation (unknown action, bad param, invalid condition
  operator, unknown top-level key) raises `ValueError` at load.

You can check a saved profile over the API without running data through it:

- `GET /v1/configs/{name}/conflicts`, overlapping/shadowed rules
- `GET /v1/configs/{name}/coverage`, HIPAA-identifier coverage gaps

See [Author rules](../how-to/author-rules.md) to create and test rules, and
[AI-assisted rules](../how-to/ai-assisted-rules.md) to generate them from a
natural-language description.
