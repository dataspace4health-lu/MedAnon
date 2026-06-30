---
title: "De-identify HL7 v2"
sidebar_position: 4
description: "Scrub HL7 v2 messages via the default segment profile or custom rules."
---

# De-identify HL7 v2

HL7 v2 messages are scrubbed segment-by-segment by default, PHI is removed from
`PID` (patient identification: name, DOB, address, phone, SSN, MRN), `NK1`
(next of kin), and the other identifying segments.

```bash
# Default segment scrub (text in, text out)
curl -X POST http://localhost:8000/v1/process/hl7v2 \
  -H 'Content-Type: text/plain' \
  --data-binary @message.hl7

# Batch (multiple messages in one payload)
curl -X POST http://localhost:8000/v1/process/hl7v2/batch \
  -H 'Content-Type: text/plain' \
  --data-binary @batch.hl7
```

## Custom rules

Supply `?config_profile=` to route the message through the full
[rule engine](../reference/rules.md) instead of the built-in segment scrub.
This is useful when you need a specific action, for example pseudonymizing the
MRN rather than blanking it:

```bash
curl -X POST 'http://localhost:8000/v1/process/hl7v2?config_profile=my_hl7_profile' \
  -H 'Content-Type: text/plain' --data-binary @message.hl7
```
