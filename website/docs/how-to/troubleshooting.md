---
title: "Troubleshooting & FAQ"
sidebar_position: 20
description: "Common problems and how to resolve them."
---

# Troubleshooting & FAQ

## Startup

**gPAS healthcheck never goes green.** gPAS (WildFly) takes ~90s on first boot
and must reach its PostgreSQL backend. If `/metadata` works but pseudonymize
hangs, the WildFly datasource is likely defaulting to the wrong DBMS, set the
`TTP_*_DB_DBMS`/`HOST` variables. See [Deploy](./deploy-docker.md).

**API exits immediately with `SystemExit(2)`.** The durable-store guard refused
the SQLite fallback. The dedicated `worker` always refuses SQLite (it shares
`/output` across containers). Set `MEDANON_REDIS_URL` or `MEDANON_APP_DB_URL`,
or for single-container dev set `MEDANON_ALLOW_SQLITE_FALLBACK=true`.

## Rules & config

**A rule isn't firing.** Check, in order: the [match dialect](../reference/rules.md#match-dialects-per-data-type)
matches the data type; a higher-priority rule isn't shadowing it
(`GET /v1/configs/{name}/conflicts`); a `condition` isn't excluding it (enable
`MEDANON_MANIFEST_ENABLED=true` to see `skipped_reason: condition_not_met`).

**Config won't load.** Run with `MEDANON_RULE_SCHEMA_STRICT=true` to surface the
exact violation (unknown action, bad param, invalid condition operator, unknown
top-level key). See [Author rules](./author-rules.md).

**Output is blocked.** The [output gate](../explanation/scoring-system.md)
detected residual PII or a score-gate violation. Inspect the block reason; tune
`MEDANON_GATE_IDENTIFIER_MODE` (`block` vs `warn`) only when you understand the
gap, or add a rule to cover the leaking field.

## Free-text / NLP

**Text fields show `[NLP_UNAVAILABLE]`.** NLP failed closed, the NLP
microservice was unreachable. This is by design (no unscrubbed text leaks).
Check the `nlp` service is up; outside Docker set
`NLP_SERVICE_URL=http://localhost:8200`.

## AI agents

**`/v1/ai/*` returns "AI disabled".** Set `MEDANON_AI_ENABLED=true` and start the
`ai` profile. `detect-pii` additionally requires a **local** provider
(`MEDANON_AI_PII_PROVIDER`). See [AI-assisted rules](./ai-assisted-rules.md).

## Jobs

**A job is stuck or lost.** With Redis, the index orphan sweep
(`MEDANON_REDIS_INDEX_SWEEP_SEC`) and `XAUTOCLAIM` recover stale jobs. Check
`GET /v1/jobs/dead` for dead-lettered jobs and requeue with
`POST /v1/jobs/{id}/requeue`. See the [runbook](./operations-runbook.md).

## FAQ

**Can I reverse a pseudonym?** Only gPAS-issued pseudonyms, under controlled
access via `gpas_depseudonymize`. `cryptohash` is one-way by design.

**Do I have to use the bundled configs?** No, they are starting examples. Author
your own with [Author rules](./author-rules.md) or generate one with
[AI](./ai-assisted-rules.md).

**Which data types are supported?** FHIR R4, CDA/CCDA, DICOM, HL7 v2, and
tabular/SQL, see the per-format how-to guides.
