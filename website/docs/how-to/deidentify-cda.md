---
title: "De-identify CDA / CCDA"
sidebar_position: 5
description: "Scrub PHI from CDA R2 / CCDA clinical documents."
---

# De-identify CDA / CCDA

CDA R2 / CCDA documents are parsed with `defusedxml` and routed through the
[rule engine](../reference/rules.md). The built-in handling scrubs PHI from the
standard sections, `recordTarget` (patient demographics: name, DOB, address,
telecom, IDs), `author`, `legalAuthenticator`, `dataEnterer`, participants,
informants, and the encompassing encounter.

```bash
curl -X POST http://localhost:8000/v1/process/cda \
  -H 'Content-Type: application/xml' \
  --data-binary @document.xml -o document.deid.xml
```

## Custom rules

Pass `?config_profile=` to apply your own rules over the parsed document
(FHIRPath-style selectors):

```bash
curl -X POST 'http://localhost:8000/v1/process/cda?config_profile=my_cda_profile' \
  -H 'Content-Type: application/xml' \
  --data-binary @document.xml -o document.deid.xml
```

XML parsing uses `defusedxml`, so entity-expansion and external-entity attacks
are mitigated by default.
