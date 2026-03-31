# MedAnon De-identification Policy Guide

This document describes the five built-in configuration profiles bundled with
MedAnon, when to use each one, and how to create custom profiles.

---

## Profile Comparison

| Profile | File | ID handling | Date resolution | Geographic | Text scrubbing | Requires gPAS | Primary legal basis |
|---|---|---|---|---|---|---|---|
| **Default** | `config.yaml` | SHA3-256 hash | Year only | Zip prefix (3 digits) | Regex + NLP | No | Internal / testing |
| **gPAS Production** | `config_gpas.yaml` | gPAS pseudonym (reversible) | Year only | Zip prefix (3 digits) | Regex + NLP | Yes | Jurisdiction-specific |
| **GDPR** | `config_gdpr_eu.yaml` | SHA3-256 HMAC | Redacted | Redacted | Regex + NLP | No | GDPR Art. 4(5), 25, 89 |
| **HIPAA Safe Harbor** | `config_hipaa_safe_harbor.yaml` | Redacted | Year only | State + 3-digit zip | Regex + NLP | No | 45 CFR § 164.514(b) |
| **Research Pseudonymous** | `config_research_pseudonymous.yaml` | SHA3-256 hash | Year-month | 3-digit zip prefix | Regex + NLP | No | IRB / Art. 89 GDPR |
| **Structure Preserving** | `config_structure_preserving.yaml` | gPAS pseudonym (reversible) | Year only (birthDate) | Preserved | Regex + NLP | Yes | Structure-first / downstream consumers |

---

## When to Use Each Profile

### `config.yaml` — Default (Local Dev / Testing)

Use this profile when:
- Running local tests or CI pipelines without external dependencies
- Exploring the de-identification pipeline for the first time
- Processing synthetic or public datasets with no real patient data

**Not suitable for:** production clinical data, regulatory submissions, or
any data sharing with third parties.

---

### `config_gpas.yaml` — gPAS Production

Use this profile when:
- Operating in a production clinical environment with a gPAS server
- You need **reversible pseudonymization** (ability to re-link records under
  controlled conditions, e.g. follow-up studies, adverse event investigation)
- Your institution uses the MosaicGrieveswald TP/TT gPAS pseudonym service

Pair this profile with `config_gdpr_eu.yaml` or `config_hipaa_safe_harbor.yaml`
for the appropriate jurisdiction-level rule set.

**Required environment variables:**

| Variable | Description |
|---|---|
| `GPAS_URL` | gPAS server FHIR endpoint (e.g. `http://gpas:8080/ttp-fhir/fhir`) |
| `GPAS_DOMAIN` | Pseudonym domain name configured in gPAS |
| `GPAS_BASIC_USER` | HTTP Basic Auth username (default: `user`) |
| `GPAS_BASIC_PASS` | HTTP Basic Auth password |

---

### `config_gdpr_eu.yaml` — EU GDPR Compliance

Use this profile when:
- Processing personal data of EU/EEA residents
- You need to demonstrate compliance with GDPR data minimisation (Art. 5(1))
  and pseudonymisation requirements (Art. 4(5))
- The legal basis for processing is Art. 89 (scientific/statistical research)

**Key behaviour:** All direct identifiers are SHA3-256 hashed (not redacted),
which satisfies Art. 4(5) pseudonymisation. Dates are fully redacted.

**Key management (GDPR Art. 32):** Set `MEDANON_HASH_KEY` in your environment
for HMAC-keyed hashing. Do not store the key in the config file.

---

### `config_hipaa_safe_harbor.yaml` — HIPAA Safe Harbor

Use this profile when:
- Processing US patient data governed by HIPAA
- You require a documented Safe Harbor de-identification method under
  45 CFR § 164.514(b)
- Preparing datasets for research, publication, or third-party analysis

**What is removed:** All 18 PHI identifier categories listed in the regulation
(names, contact details, geographic data below state level, dates except year,
biometric data, photos, identifiers, and any unique codes).

**What is retained:** State, 3-digit zip prefix, birth year, gender, and all
clinical codes (Condition, Observation, Medication, Procedure).

**Limitation:** Ages ≥ 90 — HIPAA Safe Harbor requires additional handling
(full birth date removal). This profile generalizes to year for all ages;
organisations with patients ≥ 90 should additionally redact `Patient.birthDate`
or use the `age_bracket` strategy in a custom profile.

---

### `config_research_pseudonymous.yaml` — Research Pseudonymous

Use this profile when:
- Preparing a dataset for internal clinical research under IRB approval
- Temporal analysis requires month-level date precision
- Longitudinal studies need stable pseudonymised IDs across multiple exports

**Key differences from HIPAA Safe Harbor:**
- Dates generalized to **year-month** (not year-only)
- Patient IDs are **cryptohashed** (not redacted), preserving referential integrity
- City/district redacted, but state and 3-digit zip prefix retained

**Not suitable for:** external data sharing, regulatory submissions, or any
context where HIPAA Safe Harbor compliance is required.

**Governance:** Document operator name and config hash in every evidence report:
```bash
python3 tools/analyze_results.py \
  --input input.ndjson --output output.ndjson \
  --operator "Your Name" \
  --config config/config_research_pseudonymous.yaml \
  --report-md evidence_report.md
```

---

### `config_structure_preserving.yaml` — Structure Preserving

Use this profile when:
- Downstream consumers require a complete, valid FHIR structure (no missing fields)
- IDs must be pseudonymized for linkage but clinical data must remain intact
- Feeding de-identified FHIR resources into systems that validate structure (e.g. validators, FHIR servers)

**Key behaviour:** Fields are **never removed** — names, addresses, and telecom values are replaced with `[REDACTED]` via `substitute` (field stays present). IDs are pseudonymized via gPAS with `rewrite_references: true` to maintain referential integrity across bundles. Only `birthDate` is generalized (year-only). All clinical data (codes, values, observations, conditions) is untouched.

**Requires gPAS.** Not suitable when gPAS is unavailable — use `config_research_pseudonymous.yaml` instead.

---

## Creating a Custom Profile

All config files follow the same YAML structure:

```yaml
general:
  appname: MyOrg-Custom
  hash_type: sha3_256          # sha3_256 | sha256 | sha512
  rewrite_references: true     # rewrite FHIR references after ID changes
  rewrite_text_ids: true       # replace pseudonymised IDs in text fields

rules:
  - match: Patient.name        # FHIRPath expression
    action: redact             # action name

  - match: Patient.birthDate
    action: generalize
    params:
      strategy: date_year      # date_year | date_year_month | zip_prefix | age_bracket
```

### Available Actions

| Action | Description | Key params |
|---|---|---|
| `redact` | Replace field value with `""` / `null` | `replacement` |
| `cryptohash` | SHA3-256 (or HMAC) hash of the value | `hash_type`, `secret_key_env` |
| `generalize` | Coarsen the value | `strategy`: `date_year`, `date_year_month`, `zip_prefix`, `age_bracket`, `number_round`, `category` |
| `substitute` | Replace with a fixed value | `value` |
| `perturb` | Add random noise to numeric values | `range`, `distribution` |
| `scrub_text` | Regex-based PHI removal in free text | `mode`, `patterns` |
| `nlp_detect` | NLP-based entity detection (Presidio) | `mode`, `threshold` |
| `encrypt` | RSA encryption | `public_key_path` |
| `gpas_pseudonymize` | gPAS server pseudonymization | `gpas_url`, `domain`, `operation` |

### FHIRPath Wildcards

Use `"*.fieldName"` to apply a rule to that field across all resource types.
Use `Patient.fieldName` to target only Patient resources. Rules are evaluated
in order — place more specific rules before wildcards.

---

## Key Management

For all profiles using `cryptohash` in production:

1. Set `MEDANON_HASH_KEY` environment variable (do not store in config):
   ```bash
   export MEDANON_HASH_KEY="$(openssl rand -hex 32)"
   ```

2. Reference in the config rule:
   ```yaml
   - match: Patient.id
     action: cryptohash
     params:
       secret_key_env: MEDANON_HASH_KEY
   ```

3. Rotate keys according to your data retention policy. After rotation, old
   pseudonymised outputs **cannot** be re-linked to new outputs without the
   original key.

---

## Evidence and Audit

After each de-identification run, generate an evidence report:

```bash
python3 services/anonymizer/tools/analyze_results.py \
  --input  original_patients.ndjson \
  --output deid_patients.ndjson \
  --operator "Alice Smith" \
  --config  services/anonymizer/config/config_hipaa_safe_harbor.yaml \
  --report-json evidence.json \
  --report-md   evidence.md
```

The report includes:
- Operator name and ISO timestamp
- Config file name and SHA-256 hash
- Resource counts before/after, ID change count, reference rewrites
- NLP token counts (confirms text scrubbing ran)

Use `docs/EVIDENCE_REPORT_TEMPLATE.md` as the basis for formal audit submissions.
