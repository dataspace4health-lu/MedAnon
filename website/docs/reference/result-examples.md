---
title: "Result Examples"
sidebar_position: 9
description: "Input/output walkthrough for a realistic mixed clinical batch, PII, free text, and a Base64 attachment."
---

# Result Examples

A single batch of three resources, Patient, Observation, and DocumentReference, processed with one rule profile. The example deliberately mixes structured PII, clinical codes, a free-text narrative, and a Base64-encoded binary attachment to show every significant action in one pass.

For the scoring model see [Scoring System](../explanation/scoring-system.md). For how to write your own rules see [Author Rules](../how-to/author-rules.md).

---

## Rule Profile Used in This Example

```yaml
rules:
  # ── Patient identity ────────────────────────────────────────────────────────
  - name: pseudonymize patient id
    match: Patient.id
    action: cryptohash

  - name: pseudonymize identifiers
    match: "Patient.identifier.value"
    action: cryptohash

  - name: redact full name
    match: Patient.name
    action: redact

  - name: generalize birth date to year
    match: Patient.birthDate
    action: generalize
    params:
      strategy: date_year

  - name: redact street address
    match: "Patient.address.line"
    action: redact

  - name: generalize postal code to 3-digit prefix
    match: "Patient.address.postalCode"
    action: generalize
    params:
      strategy: zip_prefix
      length: 3

  - name: redact phone and email
    match: Patient.telecom
    action: redact

  - name: scrub HTML narrative
    match: "Patient.text.div"
    action: nlp_scrub

  # ── Observation ─────────────────────────────────────────────────────────────
  - name: pseudonymize observation id
    match: Observation.id
    action: cryptohash

  - name: generalize observation date to year-month
    match: Observation.effectiveDateTime
    action: generalize
    params:
      strategy: date_year_month

  - name: scrub clinical notes
    match: "Observation.note.text"
    action: nlp_scrub

  # ── DocumentReference ───────────────────────────────────────────────────────
  - name: pseudonymize document id
    match: DocumentReference.id
    action: cryptohash

  - name: redact binary attachment
    match: "DocumentReference.content.attachment.data"
    action: redact

  - name: generalize document date to year-month
    match: DocumentReference.date
    action: generalize
    params:
      strategy: date_year_month
```

Cross-resource reference rewriting is handled automatically by the finalize stage, no extra rules needed.

---

## Resource 1: Patient

**Input**

```json
{
  "resourceType": "Patient",
  "id": "pat-00842",
  "meta": { "versionId": "3", "lastUpdated": "2024-11-12T08:20:00Z" },
  "identifier": [
    { "system": "http://hospital.example/mrn", "value": "MRN-40821" },
    { "system": "http://fhir.de/sid/gkv/kvid-10", "value": "A123456789" }
  ],
  "name": [
    { "use": "official", "family": "Schneider", "given": ["Lena", "Maria"] }
  ],
  "birthDate": "1978-04-23",
  "gender": "female",
  "address": [
    {
      "use": "home",
      "line": ["Friedrichstraße 88"],
      "city": "Hamburg",
      "postalCode": "20099",
      "country": "DE"
    }
  ],
  "telecom": [
    { "system": "phone", "value": "+49 40 55512345", "use": "mobile" },
    { "system": "email", "value": "lena.schneider@example.de" }
  ],
  "text": {
    "status": "generated",
    "div": "<div>Patient Lena Schneider, DOB 23.04.1978, MRN-40821. Resides at Friedrichstraße 88, Hamburg. Mobile: +49 40 55512345.</div>"
  }
}
```

**Output**

```json
{
  "resourceType": "Patient",
  "id": "9f3a2b1c8d7e4f5a0b6c3d2e1f8a9b7c",
  "meta": { "versionId": "3", "lastUpdated": "2024-11-12T08:20:00Z" },
  "identifier": [
    { "system": "http://hospital.example/mrn", "value": "3c7d2e1f9a8b5c4d2e1f3a7c9b8d5e4f" },
    { "system": "http://fhir.de/sid/gkv/kvid-10", "value": "7b2a1c9e3d8f5a4b2c1e9d8a7f3b5c2d" }
  ],
  "name": [],
  "birthDate": "1978",
  "gender": "female",
  "address": [
    {
      "use": "home",
      "line": [],
      "city": "Hamburg",
      "postalCode": "200",
      "country": "DE"
    }
  ],
  "telecom": [],
  "text": {
    "status": "generated",
    "div": "<div>Patient [[PERSON_1]], DOB [[DATE_TIME_1]], [[MEDICAL_LICENSE_1]]. Resides at [[LOCATION_1]]. Mobile: [[PHONE_NUMBER_1]].</div>"
  }
}
```

**Transformations**

| Field | Input | Output | Action | Stage |
|---|---|---|---|---|
| `id` | `pat-00842` | `9f3a2b1c…` | `cryptohash` (HMAC-SHA3-256) | match |
| `identifier[0].value` | `MRN-40821` | `3c7d2e1f…` | `cryptohash` | match |
| `identifier[1].value` | `A123456789` | `7b2a1c9e…` | `cryptohash` | match |
| `name` | `[{family: "Schneider", given: ["Lena","Maria"]}]` | `[]` | `redact` | match |
| `birthDate` | `1978-04-23` | `1978` | `generalize(date_year)` | match |
| `address.line` | `["Friedrichstraße 88"]` | `[]` | `redact` | match |
| `address.postalCode` | `20099` | `200` | `generalize(zip_prefix, 3)` | match |
| `address.city` | `Hamburg` | `Hamburg` | no rule, retained | n/a |
| `telecom` | phone + email | `[]` | `redact` | match |
| `text.div` | narrative with name, DOB, MRN, phone | `[[PERSON_1]]…[[PHONE_NUMBER_1]]` | `nlp_scrub` | phi_detection |
| `gender` | `female` | `female` | no rule, retained | n/a |

`address.city` and `gender` are retained because no rule covers them. This is intentional, you decide exactly what gets transformed. If city is sensitive for your use case, add a rule for `Patient.address.city`.

---

## Resource 2: Observation

**Input**

```json
{
  "resourceType": "Observation",
  "id": "obs-bp-20241110",
  "status": "final",
  "subject": { "reference": "Patient/pat-00842" },
  "effectiveDateTime": "2024-11-10T14:30:00Z",
  "code": {
    "coding": [
      { "system": "http://loinc.org", "code": "55284-4",
        "display": "Blood pressure systolic and diastolic" }
    ]
  },
  "component": [
    {
      "code": { "coding": [{ "system": "http://loinc.org", "code": "8480-6", "display": "Systolic blood pressure" }]},
      "valueQuantity": { "value": 148, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]" }
    },
    {
      "code": { "coding": [{ "system": "http://loinc.org", "code": "8462-4", "display": "Diastolic blood pressure" }]},
      "valueQuantity": { "value": 92, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]" }
    }
  ],
  "note": [
    {
      "text": "Patient Lena Schneider presented with elevated BP (148/92 mmHg). Discussed by Dr. A. Hoffmann; refer to nephrology. Insurance ID: A123456789."
    }
  ]
}
```

**Output**

```json
{
  "resourceType": "Observation",
  "id": "8a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d",
  "status": "final",
  "subject": { "reference": "Patient/9f3a2b1c8d7e4f5a0b6c3d2e1f8a9b7c" },
  "effectiveDateTime": "2024-11",
  "code": {
    "coding": [
      { "system": "http://loinc.org", "code": "55284-4",
        "display": "Blood pressure systolic and diastolic" }
    ]
  },
  "component": [
    {
      "code": { "coding": [{ "system": "http://loinc.org", "code": "8480-6", "display": "Systolic blood pressure" }]},
      "valueQuantity": { "value": 148, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]" }
    },
    {
      "code": { "coding": [{ "system": "http://loinc.org", "code": "8462-4", "display": "Diastolic blood pressure" }]},
      "valueQuantity": { "value": 92, "unit": "mmHg", "system": "http://unitsofmeasure.org", "code": "mm[Hg]" }
    }
  ],
  "note": [
    {
      "text": "Patient [[PERSON_1]] presented with elevated BP (148/92 mmHg). Discussed by [[PERSON_2]]; refer to nephrology. Insurance ID: [[MEDICAL_LICENSE_1]]."
    }
  ]
}
```

**Transformations**

| Field | Input | Output | Action | Stage |
|---|---|---|---|---|
| `id` | `obs-bp-20241110` | `8a1b2c3d…` | `cryptohash` | match |
| `subject.reference` | `Patient/pat-00842` | `Patient/9f3a2b1c…` | reference rewriting | finalize |
| `effectiveDateTime` | `2024-11-10T14:30:00Z` | `2024-11` | `generalize(date_year_month)` | match |
| `code` (LOINC `55284-4`) | retained | retained | no rule, clinical code, not PII | n/a |
| `component[*].valueQuantity` | `148`, `92` | `148`, `92` | no rule, clinical measurement | n/a |
| `note[0].text` | narrative with name, doctor, insurance ID | `[[PERSON_1]]…[[MEDICAL_LICENSE_1]]` | `nlp_scrub` | phi_detection |

The `subject.reference` pointed to `Patient/pat-00842`. After the pseudonymize stage computed the patient's new ID, the finalize stage (`post_processor.py`) rewrote every matching reference across all resources automatically.

**NLP entity detection for the note:**

| Entity type | Detected text | Token |
|---|---|---|
| `PERSON` | `Lena Schneider` | `[[PERSON_1]]` |
| `PERSON` | `Dr. A. Hoffmann` | `[[PERSON_2]]` |
| `MEDICAL_LICENSE` | `A123456789` | `[[MEDICAL_LICENSE_1]]` |

Token naming is deterministic within a resource, the same text always maps to the same token. Numbering resets per resource, so `[[PERSON_1]]` here is unrelated to `[[PERSON_1]]` in any other resource.

---

## Resource 3: DocumentReference (Base64 attachment)

**Input**

```json
{
  "resourceType": "DocumentReference",
  "id": "doc-discharge-20241112",
  "status": "current",
  "subject": { "reference": "Patient/pat-00842" },
  "date": "2024-11-12T10:00:00Z",
  "type": {
    "coding": [{ "system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary" }]
  },
  "content": [
    {
      "attachment": {
        "contentType": "application/pdf",
        "title": "Discharge Summary - Lena Schneider",
        "data": "JVBERi0xLjQKJcOkw7zDtsOfCjIgMCBvYmoKPDwgL0xlbmd0aCAzIDAgUiAvRmlsdGVyIC9GbGF0ZURlY29kZSA+PgpzdHJlYW0KeJxLSkr..."
      }
    }
  ]
}
```

**Output**

```json
{
  "resourceType": "DocumentReference",
  "id": "f2e1d3c4b5a6978f3e2d1c4b5a697834",
  "status": "current",
  "subject": { "reference": "Patient/9f3a2b1c8d7e4f5a0b6c3d2e1f8a9b7c" },
  "date": "2024-11",
  "type": {
    "coding": [{ "system": "http://loinc.org", "code": "18842-5", "display": "Discharge summary" }]
  },
  "content": [
    {
      "attachment": {
        "contentType": "application/pdf",
        "title": "Discharge Summary - Lena Schneider",
        "data": null
      }
    }
  ]
}
```

**Transformations**

| Field | Input | Output | Action | Stage |
|---|---|---|---|---|
| `id` | `doc-discharge-20241112` | `f2e1d3c4…` | `cryptohash` | match |
| `subject.reference` | `Patient/pat-00842` | `Patient/9f3a2b1c…` | reference rewriting | finalize |
| `date` | `2024-11-12T10:00:00Z` | `2024-11` | `generalize(date_year_month)` | match |
| `content[0].attachment.data` | Base64 PDF (full discharge note) | `null` | `redact` | match |
| `content[0].attachment.title` | `"Discharge Summary - Lena Schneider"` | `"Discharge Summary - Lena Schneider"` | no rule, retained | n/a |

The attachment title still contains the patient name because no rule covers `DocumentReference.content.attachment.title`. To scrub it, add:

```yaml
- name: scrub attachment title
  match: "DocumentReference.content.attachment.title"
  action: nlp_scrub
```

MedAnon never silently transforms a field. Every transformation requires an explicit rule.

---

## NLP Scrubbing Example

The following shows how the NLP microservice processes a clinical narrative:

**Input:**
```
Patient Hans Müller complained of palpitations. Seen by Dr. Anna Schmidt at
Charité Berlin. Phone follow-up scheduled: +49 30 98765432.
Previous MRN at Vivantes: MRN-112233.
```

**phi_detection stage, NLP batch detection result:**

| Entity type | Detected text | Start | End | Replacement |
|---|---|---|---|---|
| `PERSON` | `Hans Müller` | 8 | 20 | `[[PERSON_1]]` |
| `PERSON` | `Dr. Anna Schmidt` | 42 | 59 | `[[PERSON_2]]` |
| `LOCATION` | `Charité Berlin` | 63 | 78 | `[[LOCATION_1]]` |
| `PHONE_NUMBER` | `+49 30 98765432` | 118 | 134 | `[[PHONE_NUMBER_1]]` |
| `MEDICAL_LICENSE` | `MRN-112233` | 161 | 172 | `[[MEDICAL_LICENSE_1]]` |

**Output:**
```
Patient [[PERSON_1]] complained of palpitations. Seen by [[PERSON_2]] at
[[LOCATION_1]]. Phone follow-up scheduled: [[PHONE_NUMBER_1]].
Previous MRN at Vivantes: [[MEDICAL_LICENSE_1]].
```

Token naming (`[[TYPE_N]]`) is deterministic within a resource: the same entity text always maps to the same token. Across resources, token numbering resets, so `[[PERSON_1]]` in one resource is not the same person as `[[PERSON_1]]` in another.

---

## Composite Score

After the batch completes, request a score via `POST /v1/jobs/{id}/score`:

```json
{
  "job_id": "7c4e2a1b",
  "composite_score": 0.81,
  "privacy": {
    "score": 0.94,
    "gate": "PASS",
    "risk_level": "low",
    "k_anonymity": 12,
    "hipaa_identifiers_remaining": 0,
    "text_risk": false
  },
  "utility": {
    "score": 0.79,
    "field_retention_rate": 0.71,
    "code_retention_rate": 1.0,
    "date_precision": "year_month"
  },
  "quality": {
    "score": 0.97,
    "structural_validity": 0.99,
    "reference_integrity": 1.0,
    "required_fields_present": 0.96
  },
  "resource_count": 3,
  "config_profile": "clinical-research"
}
```

| Metric | Value | Interpretation |
|---|---|---|
| `composite_score` | 0.81 | Good overall balance of privacy, utility, and quality |
| `privacy.gate` | PASS | No PII detected in output; k-anonymity = 12 |
| `utility.field_retention_rate` | 0.71 | 29% of fields were redacted or generalized |
| `utility.code_retention_rate` | 1.0 | All LOINC codes retained, clinical usability preserved |
| `utility.date_precision` | `year_month` | Dates generalized to year-month, not full precision |
| `quality.reference_integrity` | 1.0 | All cross-resource references consistently rewritten |

Download a Markdown audit report with `GET /v1/jobs/{id}/score/report`.

---

## Manifest Tag

When `MEDANON_MANIFEST_ENABLED=true`, each output resource carries an auditable record of the transformations applied to it in `meta.tag`:

```json
{
  "meta": {
    "tag": [
      {
        "system": "https://medanon.io/manifest",
        "code": "de-identified",
        "display": "Patient.id:cryptohash | Patient.identifier.value:cryptohash | Patient.name:redact | Patient.birthDate:generalize(date_year) | Patient.address.line:redact | Patient.address.postalCode:generalize(zip_prefix) | Patient.telecom:redact | Patient.text.div:nlp_scrub"
      }
    ]
  }
}
```

This satisfies GDPR Art. 30 accountability requirements. The manifest tag is stripped before uploading to the target FHIR server, it would exceed HAPI's `tag_display varchar(200)` column.
