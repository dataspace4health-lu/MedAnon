# Result Example — De-identified FHIR Output

This document shows side-by-side examples of identified input and de-identified output for selected config profiles. Each transformation is annotated to explain what rule applied and why.

For the full profile comparison see [policies.md](policies.md). For the scoring model see [scoring-system.md](scoring-system.md).

---

## Example Patient resource (identified input)

This is the raw FHIR R4 Patient resource as it exists on the source FHIR server. It contains a full set of direct and quasi-identifiers.

```json
{
  "resourceType": "Patient",
  "id": "patient-123",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2024-03-15T10:30:00Z"
  },
  "name": [
    {
      "use": "official",
      "family": "Müller",
      "given": ["Hans", "Georg"]
    }
  ],
  "birthDate": "1951-08-14",
  "gender": "male",
  "address": [
    {
      "use": "home",
      "line": ["Hauptstraße 42"],
      "city": "Berlin",
      "postalCode": "10115",
      "country": "DE"
    }
  ],
  "telecom": [
    {"system": "phone", "value": "+49 30 12345678", "use": "home"},
    {"system": "email", "value": "hans.mueller@example.de"}
  ],
  "identifier": [
    {"system": "http://hospital.de/mrn", "value": "MRN-789456"},
    {"system": "http://kvnummer.de", "value": "A123456789"}
  ],
  "text": {
    "status": "generated",
    "div": "<div>Patient Hans Müller, born 14.08.1951, MRN-789456. Contact: +49 30 12345678</div>"
  }
}
```

---

## Profile 1: GDPR (`config_gdpr_eu.yaml`)

**Use case:** EU/EEA data sharing. Satisfies GDPR Art. 4(5) pseudonymization.

```json
{
  "resourceType": "Patient",
  "id": "a7f3c91b2d8e4f6a0b5c3d2e1f9a8b7c",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2024-03-15T10:30:00Z"
  },
  "name": [],
  "birthDate": null,
  "gender": "male",
  "address": [],
  "telecom": [],
  "identifier": [
    {"system": "http://hospital.de/mrn", "value": "5e2f1a9c3b7d6e4f8a2c1b9d5e3f7a6b"},
    {"system": "http://kvnummer.de", "value": "9c3d2e1f8b7a6c5d4e3f2a1b9c8d7e6f"}
  ],
  "text": {
    "status": "generated",
    "div": "<div>[REDACTED]</div>"
  }
}
```

**Transformations applied:**

| Field | Original | Result | Rule applied | Why |
|---|---|---|---|---|
| `id` | `patient-123` | `a7f3c91b...` | `cryptohash` (HMAC-SHA3-256) | GDPR Art. 4(5): pseudonymize all identifiers. Keyed with `MEDANON_HASH_KEY` — cannot be reversed without the key. |
| `name` | `[{family: "Müller", given: ["Hans", "Georg"]}]` | `[]` | `redact` | Direct identifier. Fully removed. |
| `birthDate` | `1951-08-14` | `null` | `redact` | GDPR requires dates to be removed or heavily generalized. This profile removes them entirely. |
| `address` | `[{line: "Hauptstraße 42", city: "Berlin", postalCode: "10115"}]` | `[]` | `redact` | Geographic identifiers. Fully removed. |
| `telecom` | `[{phone: "+49 30 12345678"}, ...]` | `[]` | `redact` | Contact identifiers. Fully removed. |
| `identifier[*].value` | `MRN-789456`, `A123456789` | hashes | `cryptohash` | Identifiers pseudonymized but retained (allows longitudinal linkage within the same dataset). |
| `text.div` | XHTML narrative with name + phone | `[REDACTED]` | `nlp_scrub` | Free-text PHI removed by NLP entity detection. |
| `gender` | `male` | `male` | (retained) | Gender is a quasi-identifier but retained in GDPR profile — it is a standard clinical field needed for analysis. |

---

## Profile 2: HIPAA Safe Harbor (`config_hipaa_safe_harbor.yaml`)

**Use case:** US HIPAA compliance. Removes all 18 PHI categories per 45 CFR § 164.514(b).

```json
{
  "resourceType": "Patient",
  "id": "",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2024-03-15T10:30:00Z"
  },
  "name": [],
  "birthDate": "1951",
  "gender": "male",
  "address": [
    {
      "use": "home",
      "city": null,
      "postalCode": "101",
      "country": "DE"
    }
  ],
  "telecom": [],
  "identifier": [],
  "text": {
    "status": "generated",
    "div": "<div>Patient [[PERSON_1]], born 1951, [[LOCATION_1]]. Contact: [[PHONE_NUMBER_1]]</div>"
  }
}
```

**Transformations applied:**

| Field | Original | Result | Rule applied | Why |
|---|---|---|---|---|
| `id` | `patient-123` | `""` | `redact` | HIPAA: all patient identifiers removed. |
| `name` | full name | `[]` | `redact` | HIPAA category 1: names. |
| `birthDate` | `1951-08-14` | `1951` | `generalize` (date_year) | HIPAA category 3: dates except year. Year retained for age-based analysis. |
| `address.line` | `Hauptstraße 42` | removed | `redact` | HIPAA category 4: street address. |
| `address.city` | `Berlin` | removed | `redact` | HIPAA category 5: geographic data below state level. |
| `address.postalCode` | `10115` | `101` | `generalize` (zip_prefix 3) | HIPAA: zip codes generalized to 3-digit prefix. |
| `telecom` | phone + email | `[]` | `redact` | HIPAA categories 6 (phone) + 10 (email). |
| `identifier` | MRN, KV-Nummer | `[]` | `redact` | HIPAA categories 12–17: account numbers, certificate numbers. |
| `text.div` | narrative with name + phone | `[[PERSON_1]] ... [[PHONE_NUMBER_1]]` | `nlp_scrub` | Residual PHI in free text removed by NLP and replaced with typed tokens. |
| `gender` | `male` | `male` | (retained) | Not a HIPAA PHI category — retained. |

---

## Profile 3: gPAS Production (`config_gpas.yaml`)

**Use case:** Reversible pseudonymization for research with potential follow-up linkage.

```json
{
  "resourceType": "Patient",
  "id": "psn-R7K2M9P4",
  "meta": {
    "versionId": "1",
    "lastUpdated": "2024-03-15T10:30:00Z"
  },
  "name": [],
  "birthDate": "1951",
  "gender": "male",
  "address": [
    {
      "postalCode": "101"
    }
  ],
  "telecom": [],
  "identifier": [
    {"system": "http://hospital.de/mrn", "value": "psn-A3T8N2L5"},
    {"system": "http://kvnummer.de", "value": "psn-B6X1Q7R4"}
  ],
  "text": {
    "status": "generated",
    "div": "<div>Patient [[PERSON_1]], born 1951, [[LOCATION_1]].</div>"
  }
}
```

**Key difference from GDPR/HIPAA:** The `id` and `identifier` values are gPAS **pseudonyms** (e.g. `psn-R7K2M9P4`) rather than irreversible hashes. A TTP admin can call `$depseudonymize` on gPAS to recover the original `patient-123` value for authorized re-linkage.

**Transformations applied:**

| Field | Original | Result | Rule | Note |
|---|---|---|---|---|
| `id` | `patient-123` | `psn-R7K2M9P4` | `gpas_pseudonymize` | TTP-reversible pseudonym stored in gPAS |
| `name` | full name | `[]` | `redact` | Direct identifier removed |
| `birthDate` | `1951-08-14` | `1951` | `generalize` (date_year) | Year retained |
| `address.postalCode` | `10115` | `101` | `generalize` (zip_prefix 3) | 3-digit prefix retained |
| `identifier[*].value` | MRN, KV-Nummer | gPAS pseudonyms | `gpas_pseudonymize` | Each identifier gets its own pseudonym in the same domain |
| `text.div` | narrative | NLP-scrubbed | `nlp_scrub` | Names replaced with `[[PERSON_N]]` tokens |

---

## Example Observation resource (identified input)

```json
{
  "resourceType": "Observation",
  "id": "obs-456",
  "status": "final",
  "subject": {"reference": "Patient/patient-123"},
  "effectiveDateTime": "2023-09-22T14:15:00Z",
  "code": {
    "coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]
  },
  "valueQuantity": {"value": 72, "unit": "beats/min"},
  "note": [
    {"text": "Patient Hans Müller complained of palpitations. Seen by Dr. Schmidt."}
  ]
}
```

### After de-identification (gPAS profile)

```json
{
  "resourceType": "Observation",
  "id": "psn-W5J2N8K3",
  "status": "final",
  "subject": {"reference": "Patient/psn-R7K2M9P4"},
  "effectiveDateTime": "2023",
  "code": {
    "coding": [{"system": "http://loinc.org", "code": "8867-4", "display": "Heart rate"}]
  },
  "valueQuantity": {"value": 72, "unit": "beats/min"},
  "note": [
    {"text": "Patient [[PERSON_1]] complained of palpitations. Seen by [[PERSON_2]]."}
  ]
}
```

**Key transformations:**

| Field | Transformation | Why |
|---|---|---|
| `id` | `obs-456` → `psn-W5J2N8K3` | gPAS pseudonym, reversible |
| `subject.reference` | `Patient/patient-123` → `Patient/psn-R7K2M9P4` | Reference rewriting (Pass 3+4) — the patient pseudonym was computed in Pass 2; all cross-resource references are updated to match in post-processing. |
| `effectiveDateTime` | `2023-09-22T14:15:00Z` → `2023` | `generalize` (date_year) — clinical dates generalized to year only |
| `valueQuantity.value` | `72` | retained — clinical measurements are not direct identifiers |
| `note[0].text` | names present | NLP `[[PERSON_1]]`, `[[PERSON_2]]` — Pass 1.5 NLP batch detection identified two PERSON entities and replaced them with typed tokens |

**Reference rewriting:** The `subject.reference` field originally pointed to `Patient/patient-123`. After Pass 2, the patient ID is now `psn-R7K2M9P4`. Pass 3+4 (`post_processor.py`) walks every resource and rewrites all matching references to keep the FHIR graph consistent.

---

## Example with NLP scrubbing — detailed

The following shows how the NLP microservice processes a clinical narrative:

**Input:**
```
Patient Hans Müller complained of palpitations. Seen by Dr. Anna Schmidt at 
Charité Berlin. Phone follow-up scheduled: +49 30 98765432. 
Previous MRN at Vivantes: MRN-112233.
```

**Pass 1.5 — NLP batch detection result:**

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

Token naming (`[[TYPE_N]]`) is deterministic within a resource: the same entity text always maps to the same token. Across resources, token numbering resets, so `[[PERSON_1]]` in Observation-A is not the same person as `[[PERSON_1]]` in Observation-B.

---

## Composite score — example output

After de-identification, a job can be scored via `POST /v1/jobs/{id}/score`:

```json
{
  "job_id": "abc123",
  "composite_score": 0.78,
  "privacy": {
    "score": 0.92,
    "gate": "PASS",
    "risk_level": "low",
    "k_anonymity": 8,
    "hipaa_identifiers_remaining": 0
  },
  "utility": {
    "score": 0.81,
    "field_retention_rate": 0.73,
    "code_retention_rate": 1.0,
    "date_precision": "year"
  },
  "quality": {
    "score": 0.96,
    "structural_validity": 0.98,
    "reference_integrity": 1.0,
    "required_fields_present": 0.94
  },
  "resource_count": 4821,
  "config_profile": "config_gpas.yaml"
}
```

**Interpreting the score:**
- `composite = 0.78` — good overall. Privacy gate passed, utility moderate (dates generalized reduces precision), quality high.
- `utility.field_retention_rate = 0.73` — 27% of fields were redacted. Expected for a production profile.
- `utility.date_precision = "year"` — all clinical dates generalized to year only. Expected for `config_gpas.yaml`.
- `quality.structural_validity = 0.98` — 2% of resources had minor FHIR structural issues (common with source EHR data).

Download a Markdown audit report with `GET /v1/jobs/{id}/score/report`.

---

## Manifest tag (MEDANON_MANIFEST_ENABLED=true)

When `MEDANON_MANIFEST_ENABLED=true`, each output resource gets a `meta.tag` entry recording which rules were applied:

```json
{
  "meta": {
    "tag": [
      {
        "system": "https://medanon.io/manifest",
        "code": "de-identified",
        "display": "Patient.id:gpas_pseudonymize | Patient.name:redact | Patient.birthDate:generalize(date_year) | Patient.address.postalCode:generalize(zip_prefix) | Patient.text.div:nlp_scrub"
      }
    ]
  }
}
```

This satisfies GDPR Art. 30 accountability requirements: every output resource carries an auditable record of the transformations that were applied to it. The manifest tag is stripped before uploading to the target FHIR server (it would exceed HAPI's `tag_display varchar(200)` column).
