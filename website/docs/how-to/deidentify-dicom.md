---
title: "De-identify DICOM"
sidebar_position: 3
description: "Scrub DICOM objects via the PS 3.15 default profile or custom rules."
---

# De-identify DICOM

DICOM objects are de-identified against the **DICOM PS 3.15 Annex E** Basic
Application Level Confidentiality Profile by default, all Type 1 and Type 2
identifying attributes (Table E.1-1) are scrubbed.

```bash
# Default standards-based scrub (binary in, binary out)
curl -X POST http://localhost:8000/v1/process/dicom \
  --data-binary @study.dcm -o study.deid.dcm

# Batch
curl -X POST http://localhost:8000/v1/process/dicom/batch \
  -F 'files=@a.dcm' -F 'files=@b.dcm'
```

:::note Pixel data
Pixel data is **not** modified. Burned-in annotations require a separate OCR
pass that is outside this module's scope.
:::

## Custom rules

To go beyond the standard profile, pass `?config_profile=`, the object is then
routed through the full [rule engine](../reference/rules.md) using FHIRPath-style
selectors over the resource view:

```bash
curl -X POST 'http://localhost:8000/v1/process/dicom?config_profile=my_dicom_profile' \
  --data-binary @study.dcm -o study.deid.dcm
```
