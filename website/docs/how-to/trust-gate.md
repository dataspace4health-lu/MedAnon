---
title: "Trust Gate"
sidebar_position: 13
description: "Pre-privacy data-quality barrier: Quality Passport, checks, and integration guide."
---

# Trust Gate

The Trust Gate assesses incoming FHIR data **before** it enters the privacy engine and emits a **Quality Passport** with a `PASS` / `CONDITIONAL_PASS` / `BLOCK` verdict.

Where the output gate (`pipeline/scoring/gate.py`) checks what *leaves* the pipeline, the Trust Gate checks what *enters* it. Running both gives end-to-end quality accountability: bad input is caught before de-identification; bad output is caught before release.

It runs as a standalone microservice (`services/trust-gate/`) behind the `trust` Docker profile.

## Enable it

```bash
docker compose --profile trust up -d trust-gate fhir-validator
```

Then set in `.env`:

```bash
TRUST_GATE_SERVICE_URL=http://trust-gate:8400
TRUST_GATE_MODE=warn        # advisory (default) | block | off
```

Mode behaviour:

| `TRUST_GATE_MODE` | Effect |
|---|---|
| `warn` (default) | Always assess and attach the passport; never blocks the request. |
| `block` | A `BLOCK` verdict returns HTTP 422 before any privacy transformation is applied. |
| `off` | Disable the gate entirely (equivalent to leaving `TRUST_GATE_SERVICE_URL` unset). |

Fail-soft: if the Trust Gate itself is unreachable the verdict degrades to advisory `CONDITIONAL_PASS`  the privacy pipeline is never blocked by a Trust Gate outage.

## What it checks

The checks follow the Kahn et al. (2016) taxonomy (Conformance / Completeness / Plausibility) used by OHDSI's Data Quality Dashboard and are tagged with PIQI HDQT v2.0 dimensions.

| Category | Check | Critical? |
|---|---|---|
| Conformance · value | `resourceType` present (SAM prerequisite) | **BLOCK** |
| Conformance · value | `id` present (SAM prerequisite) | **BLOCK** |
| Conformance · value | `status != entered-in-error` (FHIR Safety §4) | **BLOCK** |
| Conformance · value | Structural validity vs base FHIR (validator) | **BLOCK** |
| Conformance · value | Conformance to asserted `meta.profile` (validator) | **BLOCK** |
| Conformance · value | Coding carries system + code (not sentinel) | no |
| Conformance · value | Primitive format (date / dateTime / id regex) | no |
| Conformance · value | Code valid per code system (`$validate-code`) | no |
| Conformance · relational | References resolve within the batch | no |
| Conformance · relational | Provenance targets critical resources (ISO 21089) | configurable |
| Completeness | Required elements present per resource type | no |
| Completeness | `Observation` has `value[x]` or `dataAbsentReason` | no |
| Completeness | Recommended-element richness | no |
| Plausibility · uniqueness | No duplicate `ResourceType/id` | no |
| Plausibility · temporal | Date ordering (effective >= birthDate, period start <= end) | configurable |
| Plausibility · atemporal | Value/unit ranges (vital signs, SpO2, body weight) | configurable |
| Plausibility · atemporal | Data-driven value outliers (modified z-score / IQR) | no |
| Plausibility · uniqueness | Patient identity stable (HL7 EHR WG 2026 SAM) | no |

The three SAM prerequisite checks run before all others and BLOCK immediately on failure.

Slow checks (FHIR validator, terminology server) run in a persistent background pool with per-upstream timeouts. On timeout they are marked **SKIPPED** (NA  never FAIL), so intake latency is bounded by `TRUST_GATE_VALIDATOR_ASYNC_TIMEOUT_SEC` (default 20 s).

## Decision policy

| Condition | Verdict |
|---|---|
| Any critical check fails (SAM, structural, profile) | `BLOCK` |
| Overall pass rate < `TRUST_GATE_PASS_MIN_RATE` (default 90%) | `CONDITIONAL_PASS` |
| Any category pass rate < `TRUST_GATE_CATEGORY_MIN_RATE` (default 80%) | `CONDITIONAL_PASS` |
| Any resource type below its per-type threshold | `CONDITIONAL_PASS` |
| Provenance missing and `TRUST_GATE_PROVENANCE_SEVERITY=block` | `CONDITIONAL_PASS` |
| All gates pass | `PASS` |

## Quality Passport

The passport is PHI-free (counts, paths, check IDs  never values). It is stored in `medanon.processing_runs.trust_passport` and retrievable as Markdown:

```bash
GET /v1/processing-runs/{run_id}/passport
```

Key fields:

| Field | Meaning |
|---|---|
| `decision` | `PASS` / `CONDITIONAL_PASS` / `BLOCK` |
| `overall_score` + `overall_grade` | % of applicable checks passing; DAMA/ISO letter grade |
| `category_scores` | Per-category (conformance / completeness / plausibility) pass rates |
| `scorecard` | Per-dimension `{score, grade, checks_passed, checks_assessed}` |
| `fitness` / `approved_for` / `not_approved_for` | Purpose-bound fitness verdict |
| `checks` (FAIL results) | Failed checks with `violations`, `recommendation`, `violation_details` |
| `skipped_checks` | Checks that could not run, with the reason (honesty mechanism) |
| `report` | Markdown summary (rendered in the UI via `MarkdownReport`) |

`violation_details` rows carry `{resource_type, resource_id, path, detail}`  never values. Capped at 50 per check.

## FHIR validator

Two options:

**Bundled (default, via `--profile trust`):** HAPI FHIR sidecar's `{base}/{type}/$validate` endpoint. Starts automatically with the trust profile.

**Production (recommended):** the HL7/Inferno FHIR validator-wrapper. Set:
```bash
TRUST_GATE_VALIDATOR_URL=http://fhir-validator:4567
TRUST_GATE_VALIDATOR_ENDPOINT=/validate
```

If the validator is unreachable, conformance checks degrade to **NA**  never a false PASS, never a hard block on the validator's own outage.

## Terminology server (optional)

Code validation (`$validate-code`) requires a terminology server such as Snowstorm. Without it the terminology check is NA. Set `TRUST_GATE_TERMINOLOGY_URL` to enable.

## Trust Profiles

Reusable audit configurations  named bundles of phases, sector, and intended use  stored in the database and forwarded per request.

```bash
# Create a profile
curl -X POST http://localhost:8000/v1/trust-profiles \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: $ADMIN_KEY' \
  -d '{"name":"research-icu","phases":["conformance","completeness","plausibility"],"sector":"hospital","intended_use":"research"}'

# List phases
curl http://localhost:8000/v1/trust-profiles/_phases -H 'X-API-Key: $KEY'
```

## Key environment variables

| Variable | Default | Purpose |
|---|---|---|
| `TRUST_GATE_SERVICE_URL` | _(unset)_ | Trust Gate URL. When unset, intake barrier is a no-op. |
| `TRUST_GATE_MODE` | `warn` | `warn` / `block` / `off` |
| `TRUST_GATE_VALIDATOR_URL` | _(unset)_ | FHIR validator URL |
| `TRUST_GATE_VALIDATOR_ENDPOINT` | `/validate` | Validator API path |
| `TRUST_GATE_TERMINOLOGY_URL` | _(unset)_ | Terminology server (e.g. Snowstorm) |
| `TRUST_GATE_PASS_MIN_RATE` | `0.90` | Overall pass-rate floor for PASS verdict |
| `TRUST_GATE_CATEGORY_MIN_RATE` | `0.80` | Per-category pass-rate floor |
| `TRUST_GATE_PROVENANCE_SEVERITY` | `warn` | `warn` = advisory; `block` = scored check |
| `TRUST_GATE_VALIDATOR_ASYNC_TIMEOUT_SEC` | `20` | Validator timeout; on expiry → SKIPPED |
| `TRUST_GATE_TERMINOLOGY_ASYNC_TIMEOUT_SEC` | `5` | Terminology timeout; on expiry → SKIPPED |
| `TRUST_GATE_OFFPATH_POOL_SIZE` | `4` | Worker-pool size for off-critical-path checks |
| `TRUST_GATE_RESOURCE_THRESHOLDS_JSON` | `""` | JSON map of per-resource-type pass-rate thresholds |

## Trust Gate API

The Trust Gate microservice exposes its own endpoints directly (not proxied through the anonymizer):

```
POST http://trust-gate:8400/v1/trust/assess          # single resource / Bundle / list → passport
POST http://trust-gate:8400/v1/trust/assess/batch    # {resources:[...]} → one passport for the batch
GET  http://trust-gate:8400/health
GET  http://trust-gate:8400/ready
GET  http://trust-gate:8400/metrics
```

The anonymizer integrates automatically at `POST /v1/process` and `POST /v1/process/batch` when `TRUST_GATE_SERVICE_URL` is set.

## Testing

```bash
# Contract test (always runs, no live validator needed)
cd services/anonymizer && python3 -m pytest tests/test_validator_client.py -q

# Integration test (requires a running validator)
docker run -p 4567:4567 hl7_validator
TRUST_GATE_VALIDATOR_URL=http://localhost:4567 TRUST_GATE_VALIDATOR_ENDPOINT=/validate \
  python3 -m pytest tests/test_validator_integration.py -q
```

## References

- Kahn MG et al. *A Harmonized Data Quality Assessment Terminology and Framework for the Secondary Use of EHR Data.* eGEMs 2016;4(1):18.
- OHDSI Data Quality Dashboard  github.com/OHDSI/DataQualityDashboard
- ASTP/ONC *PIQI Healthcare Data Quality Taxonomy v2.0* (December 2024)
- ISO HL7 21089 *Trusted End-to-End Information Flows* (2026)
- HL7 FHIR Safety Checklist  hl7.org/fhir/safety.html
