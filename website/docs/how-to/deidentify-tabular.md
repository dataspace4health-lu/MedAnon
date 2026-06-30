---
title: "De-identify Tabular & SQL Data"
sidebar_position: 6
description: "Apply column-rules to CSV/XLSX/Parquet files and relational sources."
---

# De-identify Tabular & SQL Data

Tabular and relational data use the same rule engine and action set as FHIR, but
select fields with the **`column:`** matcher dialect instead of FHIRPath.

```yaml
rules:
  - match: "column:patient_name"
    action: "redact"
  - match: "column:DOB"
    action: "generalize"
    params: {strategy: "date_year"}
  - match: "table:patients/column:mrn"   # scope a rule to one table
    action: "cryptohash"
```

## Files (CSV / XLSX / Parquet)

Inspect first to discover columns, then process. Rules come from a saved
`?config_profile=` **or** inline `rules` (the UI column-mapper path).

```bash
# Discover columns + types
curl -X POST 'http://localhost:8000/v1/process/tabular/inspect' \
  -F 'file=@patients.csv'

# De-identify with a saved profile
curl -X POST 'http://localhost:8000/v1/process/tabular?config_profile=my_tabular_profile' \
  -F 'file=@patients.csv' -o deidentified.csv
```

Inline rules (JSON) are accepted via the `rules` query parameter; each
`{column, action, params?}` is converted to a `column:` rule automatically.

## Relational sources (SQL)

Register a connection (admin-only; host must be in
`MEDANON_SQL_SOURCE_ALLOWED_HOSTS`), inspect the schema, then export
de-identified rows as an async job.

```bash
# Register + test a connection
curl -X POST http://localhost:8000/v1/sql-connections \
  -H 'Content-Type: application/json' \
  -d '{"name":"clinic","dsn":"postgresql://...","sslmode":"require"}'
curl -X POST http://localhost:8000/v1/sql-connections/{conn_id}/test

# Inspect schema (tables/columns)
curl -X POST http://localhost:8000/v1/process/sql/inspect \
  -H 'Content-Type: application/json' -d '{"conn_id":"...","schema":"public"}'

# Export de-identified rows (async job → poll like any job)
curl -X POST http://localhost:8000/v1/jobs/sql-export \
  -H 'Content-Type: application/json' \
  -d '{"conn_id":"...","config_profile":"my_tabular_profile"}'
```

See [Jobs](../reference/api.md#3-async-job-queue) for polling and downloading results, and
[Configuration](../reference/configuration.md) for the SQL-source SSRF guard and
timeout settings.
