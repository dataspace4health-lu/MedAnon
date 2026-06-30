---
title: "Author Your First Rule"
sidebar_position: 2
description: "Write, save, and test a custom de-identification rule end to end."
---

# Author Your First Rule

In this lesson you will write a small config from scratch, save it, check it for
problems, and run a resource through it. It assumes the stack is up (see
[Getting Started](./getting-started.md)).

## 1. Write two rules

Create `my_profile.yaml`:

```yaml
rules:
  - name: "redact patient name"
    match: "Patient.name"
    action: "redact"
  - name: "keep only birth year"
    match: "Patient.birthDate"
    action: "generalize"
    params:
      strategy: "date_year"
```

## 2. Save it as a profile

```bash
curl -X POST http://localhost:8000/v1/configs \
  -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"name":"my_profile","yaml":open("my_profile.yaml").read()}))')"
```

## 3. Check it before running data

```bash
curl http://localhost:8000/v1/configs/my_profile/conflicts   # shadowed rules?
curl http://localhost:8000/v1/configs/my_profile/coverage    # uncovered identifiers?
```

## 4. Run a resource through it

```bash
curl -s -X POST 'http://localhost:8000/process?config_profile=my_profile' \
  -H 'Content-Type: application/json' \
  -d '{"resourceType":"Patient","id":"x","name":[{"family":"Mustermann"}],"birthDate":"1974-12-25"}' \
  | python3 -m json.tool
```

The name is redacted and the birth date becomes `1974`.

## 5. Add a condition

Make the birth-date rule fire only for active patients:

```yaml
  - name: "keep only birth year (active only)"
    match: "Patient.birthDate"
    action: "generalize"
    params: {strategy: "date_year"}
    condition:
      field: "Patient.active"
      operator: "equals"
      value: true
```

Re-save with `PUT /v1/configs/my_profile`, enable
`MEDANON_MANIFEST_ENABLED=true`, and re-run, the manifest now shows whether the
rule fired or was skipped (`condition_not_met`).

## Next

- Add more field types and actions → [Rules reference](../reference/rules.md)
- Let AI draft a config for you → [Generate a Config with AI](./generate-config-with-ai.md)
- Apply rules to non-FHIR data → [Tabular & SQL](../how-to/deidentify-tabular.md)
