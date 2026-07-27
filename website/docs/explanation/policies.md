---
title: "De-identification Policies"
sidebar_position: 5
description: "Profile selection and the compliance rationale behind each profile."
---

# MedAnon, De-identification Policy Guide

## Profile comparison

| Profile name | File | ID handling | Dates | Geographic | Text scrubbing | Requires gPAS | Legal basis |
|---|---|---|---|---|---|---|---|
| `minimal` / `auto` | `config.yaml` | SHA3-256 hash | Year only | Zip prefix (3-digit) | Regex + NLP | No | Testing only |
| `gdpr` | `config_gdpr_eu.yaml` | HMAC SHA3-256 | Redacted | Redacted | Regex + NLP | No | GDPR Art. 4(5), 25, 89 |
| `hipaa` | `config_hipaa_safe_harbor.yaml` | Redacted | Year only | State + 3-digit zip | Regex + NLP | No | 45 CFR § 164.514(b) |
| `value-masking` | `config_value_masking.yaml` | gPAS pseudonym (reversible) | Decade (birth), year (clinical) | Masked to `[REDACTED]` | `nlp_detect_act` (entity-specific) | Yes | Field-complete de-identification |

`auto` always resolves to `config.yaml`; `GPAS_URL` no longer changes profile
selection (`pipeline/config/service.py::_resolve_profile`). Select
`value-masking` explicitly, or author a custom profile with
`gpas_pseudonymize` rules, for gPAS pseudonymization.

---

## When to use each profile

### `config.yaml`, Default (Local dev / Testing)

Use when running local tests or CI pipelines without external dependencies. Uses plain SHA3-256 hash without an HMAC key, hashes are reversible via rainbow tables. **Not suitable for real patient data or any data sharing.**

### `config_gdpr_eu.yaml`, GDPR Art. 4(5)

Use for EU/EEA patient data. All direct identifiers are HMAC-SHA3-256 hashed, this satisfies GDPR Art. 4(5) pseudonymization (data cannot be attributed to a person without the key). Dates are fully redacted. Geographic data redacted.

**Key requirement:** Set `MEDANON_HASH_KEY` in the environment. Do not store the key in the config file. Losing this key makes re-linkage impossible. Per GDPR Art. 32, the key is itself a security-relevant asset, store it in a secrets manager and rotate according to your data retention policy.

### `config_hipaa_safe_harbor.yaml`, HIPAA Safe Harbor

Use for US patient data under HIPAA. Removes all 18 PHI identifier categories per 45 CFR § 164.514(b):
- Names, contact information, all geographic data below state level
- All dates except year (birth year retained)
- Phone, fax, email, SSN, MRN, account numbers, certificate numbers
- Device identifiers, URLs, IP addresses, biometrics, photos
- Any unique identifying number or code

**What is retained:** State, 3-digit zip prefix, birth year, gender, all clinical codes (Conditions, Observations, Medications, Procedures, diagnoses).

**Limitation:** HIPAA Safe Harbor additionally requires that ages ≥ 90 be further de-identified (the year alone is still identifying at extreme ages). This profile generalizes all birth dates to year. Organizations with patients ≥ 90 should additionally redact `Patient.birthDate` or use the `age_bracket` strategy in a custom profile.

### `config_value_masking.yaml`, Value Masking

Use when you need **fine-grained, entity-specific de-identification**, for example, generalizing dates rather than redacting them, and selectively encrypting certain field types while keeping clinical codes intact.

Key behaviour:
- Uses `nlp_detect_act`, NLP detects entity type first, then dispatches a per-entity action (e.g. `PERSON` → `redact`, `DATE` → `generalize`)
- IDs pseudonymized via gPAS with `rewrite_references: true`
- Birth dates generalized to decade; clinical dates to year
- Geographic data replaced with `[REDACTED]`
- `encrypt` + `generalize` combinations on specific field groups

**Gender fields:** `Patient.gender` and `Practitioner.gender` use `action: substitute` with `substitute_with: "unknown"`, NOT `[REDACTED]`. This is because FHIR R4 binds these fields to the `AdministrativeGender` value set (`male | female | other | unknown`). Any other value causes HAPI to reject the resource with HAPI-1821. `unknown` is the correct FHIR-compliant substitute.

**Requires gPAS.**

---

## Upload ordering and reference integrity

When de-identified resources are uploaded to a FHIR server, referenced resources must exist before the resources that reference them. The uploader computes a topological sort:

```
Organization  (tier 0 - no outbound references)
Practitioner  (tier 0 - no outbound references)
Patient       (tier 0 - no outbound references)
Encounter     (tier 1 - references Patient, Practitioner, Organization)
Observation   (tier 2 - references Encounter)
DiagnosticReport (tier 3 - references Observation)
```

Resources in the same tier upload together in a FHIR batch Bundle. This ordering is computed automatically from the `reference` fields in the actual data, it is not hardcoded.

Additionally, HAPI rejects purely numeric resource IDs (HAPI-0960). The uploader prefixes them with `p-` and rewrites all `reference` fields that pointed to those IDs.

---

## Creating a custom profile

```yaml
general:
  appname: MyOrg-Custom
  hash_type: sha3_256
  rewrite_references: true     # required if IDs change and resources reference each other
  rewrite_text_ids: true       # replace changed IDs that appear in text fields

rules:
  - name: "redact name"
    match: "Patient.name"
    action: redact

  - name: "pseudonymize id"
    match: "Patient.id"
    action: gpas_pseudonymize

  - name: "generalize birthdate"
    match: "Patient.birthDate"
    action: generalize
    params:
      strategy: date_year

  # Apply to all resource types
  - name: "remove narrative"
    match: "*.text.div"
    action: redact
```

**Important constraints when writing rules:**
- `Patient.gender` and `Practitioner.gender` must use `substitute` with a valid AdministrativeGender code (`male | female | other | unknown`), never `redact` or arbitrary text
- `rewrite_references: true` is required whenever IDs change (cryptohash or gPAS), otherwise cross-resource references will point to non-existent IDs on the target server
- Rules are applied in order, more specific rules (`Patient.name`) should appear before wildcards (`*.name`)

---

## Key management

For production use of `cryptohash`:

```bash
# Generate
openssl rand -hex 32

# Set in .env - never in the YAML config
MEDANON_HASH_KEY=<value>

# Reference in rule params
params:
  secret_key_env: MEDANON_HASH_KEY
```

Key rotation consequences:
- Old de-identified datasets: hashes will no longer match new outputs for the same patient
- Longitudinal studies using stable pseudonymous IDs: rotation breaks linkage
- Rotate intentionally and document the rotation date with the dataset

---

## Evidence and audit

Generate an evidence report after each de-identification run:

```bash
python3 services/anonymizer/tools/analyze_results.py \
  --input  original.ndjson \
  --output deid.ndjson \
  --operator "Alice Smith" \
  --config  services/anonymizer/config/config_hipaa_safe_harbor.yaml \
  --report-json evidence.json \
  --report-md   evidence.md
```

Report includes: operator name, ISO timestamp, config SHA-256, resource counts, ID change count, reference rewrite count, NLP token counts per entity type.

Enable `MEDANON_MANIFEST_ENABLED=true` to tag each de-identified resource with the rules that fired (required for the UI Resource Summary view and for GDPR Art. 30 record-keeping of processing activities).
