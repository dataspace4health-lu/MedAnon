---
title: "Author & Modify Rules"
sidebar_position: 1
description: "Write, validate, test, and refine de-identification rules."
---

# Author & Modify Rules

This guide shows how to create and change rules and verify them before running
real data through them. For the full field reference see
[Rules](../reference/rules.md); to generate rules from a description see
[AI-assisted rules](./ai-assisted-rules.md).

## 1. Write a rule

A rule selects fields (`match`) and transforms them (`action`):

```yaml
rules:
  - name: "hash MRN"
    match: "Patient.identifier.where(system='urn:mrn').value"
    action: "cryptohash"
  - name: "generalize birth date to year"
    match: "Patient.birthDate"
    action: "generalize"
    params:
      strategy: "date_year"
```

Pick selectors with [FHIRPath](../reference/rules.md#match-dialects-per-data-type)
for FHIR/XML, or the `column:` dialect for tabular/SQL.

## 2. Save it as a config

Create or update a profile through the API (or the **Config Builder** in the
web UI, which writes the same YAML):

```bash
# Create
curl -X POST http://localhost:8000/v1/configs \
  -H 'Content-Type: application/json' \
  -d '{"name":"my_profile","yaml":"rules:\n  - match: Patient.name\n    action: redact\n"}'

# Update
curl -X PUT http://localhost:8000/v1/configs/my_profile \
  -H 'Content-Type: application/json' \
  -d '{"yaml":"rules:\n  - match: Patient.name\n    action: redact\n"}'
```

## 3. Check for problems before running data

Two read-only checks catch the most common authoring mistakes:

```bash
# Overlapping / shadowed rules (e.g. a broad rule hiding a narrow one)
curl http://localhost:8000/v1/configs/my_profile/conflicts

# HIPAA-identifier coverage gaps (sensitive fields no rule touches)
curl http://localhost:8000/v1/configs/my_profile/coverage
```

Enable strict schema validation to fail fast on a bad action name, parameter,
or condition operator:

```bash
MEDANON_RULE_SCHEMA_STRICT=true
```

## 4. Test on a sample resource

Run one resource through your profile and inspect the result:

```bash
curl -s -X POST 'http://localhost:8000/process?config_profile=my_profile' \
  -H 'Content-Type: application/json' \
  -d @sample-patient.json | python3 -m json.tool
```

Turn on the transformation manifest to see exactly which rule touched which
field (and why a conditional rule was skipped):

```bash
MEDANON_MANIFEST_ENABLED=true   # adds a meta.tag transformation summary
```

## 5. Order and conditionalize

- Use [`priority`](../reference/rules.md#priority) (lower runs first) to apply a
  narrow rule before a broad catch-all.
- Use [`condition` / `conditions`](../reference/rules.md#conditions) to fire a
  rule only for certain resources (e.g. only `final` observations).

## 6. Promote

Once the profile passes conflicts/coverage and your sample looks right, pass
`?config_profile=my_profile` on any processing endpoint, or set it as the
default for a deployment. The same profile drives batch, bulk-export, and job
processing.
