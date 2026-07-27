---
title: "Configuration"
sidebar_position: 3
description: "Environment variables that configure the anonymizer and its integrations."
---

# Configuration

All runtime configuration is via environment variables. Copy `.env.example` to
`.env`, it documents every variable with generation instructions. This page is
the factual catalog of the most commonly used variables, grouped by concern.

:::tip
For *which* de-identification profile to choose, see
[Config Profiles](./config-profiles.md) and the
[policies explanation](../explanation/policies.md). This page only documents the
knobs, not the rationale.
:::

## Core

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_CONFIG_DIR` | `/code/config` | YAML config directory |
| `MEDANON_API_KEY` | _(blank)_ | API key; blank = open mode |
| `MEDANON_MAX_BODY_BYTES` | `10 MB` | Max HTTP body size |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `MEDANON_RATE_LIMIT_ENABLED` | `true` | Enable slowapi rate limiting |
| `MEDANON_CORS_ORIGINS` | _(blank)_ | Comma-separated CORS origins |
| `MEDANON_MANIFEST_ENABLED` | `false` | Include transformation manifest in `meta.tag` |
| `MEDANON_RULE_SCHEMA_STRICT` | `false` | Fail config load on rule-schema violations |
| `MEDANON_OUTPUT_GATE_ENABLED` | `true` | Unified output validation barrier |
| `MEDANON_GATE_IDENTIFIER_MODE` | `block` | HIPAA-coverage-gap handling: `block` or `warn` |

## Transformation

| Variable | Purpose |
|---|---|
| `MEDANON_HASH_KEY` | HMAC-SHA3-256 key for `cryptohash` (required in production) |
| `MEDANON_HASH_ALLOW_PLAIN` | Allow plain SHA3-256 without HMAC (dev/tests only) |
| `MEDANON_RSA_PUBLIC_KEY` / `MEDANON_RSA_PRIVATE_KEY` | RSA key file paths (in container) |
| `MEDANON_KEY_ALLOWED_DIRS` | Colon-separated allowed key dirs (path-traversal guard) |

## gPAS pseudonymization

| Variable | Default | Purpose |
|---|---|---|
| `GPAS_URL` | _(unset)_ | gPAS server URL. No longer changes profile selection (`auto` always resolves to `config.yaml`); select `value-masking`, or a config with `gpas_pseudonymize` rules, to use it |
| `GPAS_DOMAIN` / `GPAS_OPERATION` |, | Pseudonymization domain and operation |
| `GPAS_TOKEN` / `GPAS_BASIC_USER` / `GPAS_BASIC_PASS` |, | Auth (token wins) |
| `GPAS_TIMEOUT_SEC` | `30` | Request timeout |
| `GPAS_RETRY_COUNT` / `GPAS_RETRY_BACKOFF_SEC` | `2` / `0.2` | Retry policy |
| `GPAS_CACHE_ENABLED` | `true` | In-process LRU cache for pseudonyms |
| `GPAS_CB_FAILURE_THRESHOLD` / `GPAS_CB_RECOVERY_TIMEOUT_SEC` / `GPAS_CB_WINDOW_SEC` | `5` / `30` / `60` | Circuit breaker |

## Job queue & storage

Backend is auto-selected (priority order): `MEDANON_REDIS_URL` →
`RedisJobStore`; else `MEDANON_APP_DB_URL` → `PostgresJobStore`; else SQLite at
`MEDANON_JOB_DB`.

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_REDIS_URL` | _(blank)_ | Redis URL; enables L2 gPAS cache + `RedisJobStore` |
| `MEDANON_APP_DB_URL` | _(blank)_ | PostgreSQL for app state; enables `PostgresJobStore` |
| `MEDANON_JOB_WORKERS` | `3` | Max concurrent job executions per instance |
| `MEDANON_OUTPUT_DIR` | `/output` | Completed job NDJSON results |
| `MEDANON_RESULT_STORAGE` | `filesystem` | `filesystem` or `s3` (MinIO) |
| `MEDANON_REQUIRE_DURABLE_STORE` | `false` | API refuses SQLite fallback when `true` (worker always refuses) |

## Microservice opt-outs

| Variable | Effect when set |
|---|---|
| `ANALYTICS_SERVICE_URL` | `/analyse/risk` & `/generate/synthetic` proxy to the analytics service |
| `SCORING_SERVICE_URL` | Scoring proxies to the `scoring` microservice |
| `NLP_SERVICE_URL` | NLP delegates to the NLP microservice (default `http://nlp-lb:8200` in Docker) |
| `AI_SERVICE_URL` | AI agent endpoints proxy to a remote AI service |

## Authentication (pluggable)

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_AUTH_PROVIDER` | `auto` | `auto` / `apikey` / `oidc` / `none` |
| `MEDANON_AUTH_ALLOW_API_KEY` | `true` | Accept `X-API-Key` alongside OIDC |
| `OIDC_ISSUER` / `OIDC_JWKS_URL` / `OIDC_AUDIENCE` |, | OIDC validation |
| `OIDC_ROLE_CLAIM_PATH` / `OIDC_ROLE_MAP` |, | Role mapping (e.g. `realm_access.roles` for Keycloak) |

See the [security model](../explanation/security-model.md) and
[configure authentication](../how-to/configure-authentication.md) for end-to-end setup.

## AI integration

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_AI_ENABLED` | `false` | Enable AI agent features |
| `MEDANON_AI_MODEL` | | LLM model id (e.g. `ollama/llama3.2`) |
| `MEDANON_AI_API_BASE` | `http://ollama:11434` | LLM API base URL |
| `MEDANON_AI_PII_PROVIDER` | | Model override for PII detection (**must be local/self-hosted**) |

## Scoring & score gate

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_SCORING_ENABLED` | `false` | Enable universal scoring and processing-run persistence. When `true`, the score gate is also active by default. |
| `MEDANON_SCORE_GATE_ENABLED` | _(follows `MEDANON_SCORING_ENABLED`)_ | Explicitly enable/disable the output gate independent of scoring persistence. |
| `MEDANON_SCORE_GATE_MIN_COMPOSITE` | `80` | Minimum composite score (0-100) to pass the gate. Grade B = 80, Grade C = 65, Grade D = 50. |
| `MEDANON_SCORE_GATE_REQUIRE_PRIVACY` | `true` | Also block on privacy gate failures (fail_count > 0). |
| `MEDANON_GATE_IDENTIFIER_MODE` | `block` | How to handle a HIPAA coverage gap: `block` = hard block; `warn` = release with warning when no PII was actually detected. Detected PII (`text_risk > 0`) always blocks regardless. |
| `MEDANON_SCORE_RISK_THRESHOLD` | `0.3` | Privacy gate threshold: risk score above this → FAIL. |
| `MEDANON_SCORE_NER_ENABLED` | `true` | Enable Presidio NER in the text risk scan (delegates to NLP microservice). |
| `MEDANON_SCORE_NER_THRESHOLD` | `0.5` | Minimum Presidio confidence to count as a detection. |

## PHI safety (structural pass)

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_STRUCTURAL_PHI_ENABLED` | `false` | Enable a structural geo-redaction pass: strips extension text, reduces over-precise postal codes, and scrubs free-text fields not matched by any rule. |
| `MEDANON_ATTACHMENT_SCAN` | `false` | Enable heuristic scanning of `Attachment.data` / base64 fields for embedded PHI patterns before emitting output. |
| `MEDANON_PII_GATE` | `false` | Block any resource with residual PII detected in output text (independent of the composite score gate). |

## Trust Gate

| Variable | Default | Purpose |
|---|---|---|
| `TRUST_GATE_SERVICE_URL` | _(unset)_ | URL of the Trust Gate microservice. When unset the intake barrier is a complete no-op. |
| `TRUST_GATE_MODE` | `warn` | `warn` = advisory passport (never blocks); `block` = BLOCK verdict returns HTTP 422 before any transformation; `off` = disabled. |
| `TRUST_GATE_VALIDATOR_URL` | _(unset)_ | FHIR validator URL (e.g. `http://fhir-validator:4567`). |
| `TRUST_GATE_VALIDATOR_ENDPOINT` | `/validate` | Validator API path. |
| `TRUST_GATE_TERMINOLOGY_URL` | _(unset)_ | Terminology server for `$validate-code` (e.g. Snowstorm). Optional; absent → terminology checks are NA. |
| `TRUST_GATE_PASS_MIN_RATE` | `0.90` | Fraction of applicable checks that must pass to reach PASS verdict (vs CONDITIONAL_PASS). |
| `TRUST_GATE_CATEGORY_MIN_RATE` | `0.80` | Per-category minimum pass rate. |
| `TRUST_GATE_PROVENANCE_SEVERITY` | `warn` | `warn` = provenance advisory; `block` = adds a scored `conformance.provenance_present` check. |
| `TRUST_GATE_VALIDATOR_ASYNC_TIMEOUT_SEC` | `20` | Structural/profile validator timeout; on expiry checks are SKIPPED (NA). |
| `TRUST_GATE_TERMINOLOGY_ASYNC_TIMEOUT_SEC` | `5` | Terminology validation timeout; on expiry check is SKIPPED (NA). |
| `TRUST_GATE_OFFPATH_POOL_SIZE` | `4` | Worker-pool size for off-critical-path validator and terminology evaluation. |
| `TRUST_GATE_RESOURCE_THRESHOLDS_JSON` | `""` | JSON map overriding per-resource-type pass-rate thresholds (fractions or percentages). |

## Two-phase staging and encrypted bodies

| Variable | Default | Purpose |
|---|---|---|
| `MEDANON_STAGING_DB_URL` | _(unset)_ | PostgreSQL URL for the two-phase staging layer. When set, Phase 1 fetches resources into `medanon.staged_resources`; Phase 2 claims partitions, de-identifies, and writes NDJSON. Optimal for bulk exports above `MEDANON_STAGED_THRESHOLD_ROWS`. |
| `MEDANON_STAGED_THRESHOLD_ROWS` | `500000` | Resource count above which the export engine switches to the staging path. |
| `MEDANON_EXPORT_MODE` | `auto` | `auto` (threshold-based), `staged` (always stage), or `stream` (always stream). |
| `MEDANON_STAGE_BODIES` | `none` | `encrypted` = store Fernet-encrypted resource bodies in PostgreSQL to skip the FHIR re-fetch in Phase 2 (requires `MEDANON_STAGE_BLOB_KEY`). |
| `MEDANON_STAGE_BLOB_KEY` | _(unset)_ | Fernet key for encrypted staging bodies. Generate with `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `MEDANON_FHIR_REFETCH_CHUNK` | `200` | Phase 2 re-fetch batch size when `MEDANON_STAGE_BODIES` is not `encrypted`. |
| `MEDANON_STAGING_EXECUTOR` | `thread` | Parallelism model for Phase 2: `thread` (default) or `process`. |

## Bulkheads (concurrency caps per upstream)

| Variable | Default | Purpose |
|---|---|---|
| `BULKHEAD_GPAS_MAX_CONCURRENT` | `10` | Maximum concurrent gPAS requests. A single gPAS replica wedges if over-driven. |
| `BULKHEAD_GPAS_WAIT_SEC` | `2` | Seconds to wait for a gPAS slot before fast-failing. |
| `BULKHEAD_NLP_MAX_CONCURRENT` | `8` | Maximum concurrent NLP microservice requests. |
| `BULKHEAD_NLP_WAIT_SEC` | `2` | Seconds to wait for an NLP slot before fast-failing. |
| `BULKHEAD_FHIR_MAX_CONCURRENT` | `16` | Maximum concurrent FHIR server requests. |

## FHIRPath caches

| Variable | Default | Purpose |
|---|---|---|
| `FHIRPATH_CACHE_SIZE` | `512` | LRU cache size for compiled FHIRPath expressions. |
| `FHIRPATH_CLASSIFY_CACHE_SIZE` | `256` | Cache size for resource-type classification results. |
| `FHIRPATH_WHERE_CACHE_SIZE` | `256` | Cache size for `where()` filter results. |
| `FHIRPATH_CANDIDATES_CACHE_SIZE` | `256` | Cache size for candidate-path sets. |

:::note
The authoritative, fully-commented list is `.env.example` in the repository root.
:::
