# De-identification Evidence Report

> **Instructions:** Complete every section marked `[FILL IN]` before archiving this report.
> Generate the technical metrics for sections 3-4 using `analyze_results.py` (see Appendix).
> Archive this report alongside the de-identified output dataset.

---

## 1. Administrative Information

| Field | Value |
|---|---|
| **Report date** | [FILL IN -- ISO 8601: YYYY-MM-DD] |
| **Organisation** | [FILL IN] |
| **Department / project** | [FILL IN] |
| **Operator name** | [FILL IN -- person who ran the de-identification] |
| **Operator role** | [FILL IN -- e.g. Data Protection Officer, Research Data Manager] |
| **Purpose of processing** | [FILL IN -- e.g. secondary research, quality audit, external data sharing] |
| **Legal basis** | [FILL IN -- e.g. GDPR Art. 89 / HIPAA 45 CFR SS 164.514(b) / IRB approval #XXXX] |
| **Dataset description** | [FILL IN -- cohort name, date range, source system] |
| **Approval reference** | [FILL IN -- ethics committee / IRB approval number, or N/A] |

---

## 2. Configuration

| Field | Value |
|---|---|
| **Config profile used** | [FILL IN -- e.g. `config_hipaa_safe_harbor.yaml`] |
| **Config SHA-256 hash** | [FILL IN -- from `analyze_results.py --config`] |
| **MedAnon version / image tag** | [FILL IN -- e.g. `medanon:1.2.3` or git commit hash] |
| **gPAS domain** (if applicable) | [FILL IN or N/A] |
| **HMAC key rotation date** (if applicable) | [FILL IN or N/A] |
| **Keycloak auth enabled** | [Yes / No] |
| **Transformation manifest enabled** | [Yes / No -- MEDANON_MANIFEST_ENABLED] |

> The SHA-256 hash of the config file proves the exact rule set used. Verify:
> `sha256sum config/<profile>.yaml`

### Config Profile Summary

| Setting | Value |
|---|---|
| ID strategy | [cryptohash / gpas_pseudonymize / HMAC] |
| Date strategy | [date_year / date_year_month / perturb] |
| Text scrubbing | [regex only / regex + NLP / disabled] |
| Reference rewriting | [true / false] |
| Text ID rewriting | [true / false] |

---

## 3. Input Dataset

| Field | Value |
|---|---|
| **Input file(s)** | [FILL IN -- file name(s), format (JSON/NDJSON/XML), storage location] |
| **Total resources** | [FILL IN -- from `analyze_results.py`: `comparable_resource_ids`] |
| **Resource types** | [FILL IN -- e.g. Patient: 500, Observation: 2000, Condition: 800] |
| **Data source system** | [FILL IN -- e.g. HAPI FHIR R4, Epic EHR export, Synthea synthetic] |
| **Date range of records** | [FILL IN -- earliest to latest encounter/birth date] |
| **Patient count** | [FILL IN] |
| **File checksum (SHA-256)** | [FILL IN -- `sha256sum input_file.ndjson`] |

---

## 4. Output Dataset

| Field | Value |
|---|---|
| **Output file(s)** | [FILL IN -- file name(s), format, storage location] |
| **Total resources** | [FILL IN] |
| **Resource types** | [FILL IN -- from `analyze_results.py`: `resource_types_after`] |
| **IDs transformed** | [FILL IN -- `ids_changed` / `comparable_resource_ids`] |
| **References rewritten** | [FILL IN -- `reference_changes`] |
| **NLP tokens replaced** | [FILL IN -- from `analyze_results.py`: `token_counts`] |
| **File checksum (SHA-256)** | [FILL IN -- `sha256sum output_file.ndjson`] |

### Transformation Verification

- [ ] All resource IDs were transformed (`ids_changed` == `comparable_resource_ids`)
- [ ] All FHIR References were rewritten (if `rewrite_references: true`)
- [ ] NLP entity tokens present in output (if NLP rules active)
- [ ] No original patient names remain in output (spot check)
- [ ] No original dates remain with full precision (spot check)

---

## 5. Re-identification Risk Assessment

> Run `POST /analyse/risk` on the de-identified output and record results below.

| Metric | Value | Threshold | Status |
|---|---|---|---|
| **Min k (k-anonymity)** | [FILL IN] | >= 5 | [PASS / FAIL] |
| **Risk level** | [FILL IN: low/medium/high/critical] | low | [PASS / FAIL] |
| **Prosecutor risk (1/k)** | [FILL IN] | <= 0.200 | [PASS / FAIL] |
| **Journalist risk** | [FILL IN] | <= 0.200 | [PASS / FAIL] |
| **Marketer risk** | [FILL IN] | <= 0.100 | [PASS / FAIL] |
| **Singleton groups (k=1)** | [FILL IN] | 0 | [PASS / FAIL] |
| **Records with missing QI** | [FILL IN] | 0 | [PASS / FAIL] |
| **l-diversity (min l)** | [FILL IN or N/A] | >= 2 | [PASS / FAIL] |
| **l-diversity violations** | [FILL IN or N/A] | 0 | [PASS / FAIL] |

**Quasi-identifiers used:** [FILL IN -- e.g. gender, birth_year, zip_prefix_3]

**Command used:**

```bash
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @<OUTPUT_FILE> | python3 -m json.tool
```

### Risk Interpretation

| Level | Condition | Required Action |
|---|---|---|
| Low | k >= 5 | None. Meets basic k-anonymity. |
| Medium | k = 3 or 4 | Document justification or apply broader generalization. |
| High | k = 2 | Suppress records in small groups or increase generalization. |
| Critical | k = 1 | Do not release. Unique records must be suppressed. |

---

## 6. Residual Risks and Mitigations

> Document any known residual risks and the mitigations applied or planned.

| # | Risk Description | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| 1 | [FILL IN -- e.g. small cohort for rare disease] | [Low/Medium/High] | [Low/Medium/High] | [FILL IN -- e.g. additional record suppression] |
| 2 | [FILL IN -- e.g. temporal re-identification via unique admission dates] | | | |
| 3 | [FILL IN -- e.g. external dataset linkage via diagnosis codes] | | | |

---

## 7. Data Handling and Governance

| Field | Value |
|---|---|
| **Output access control** | [FILL IN -- who has access, access control mechanism] |
| **Storage location** | [FILL IN -- server, path, encryption at rest] |
| **Retention period** | [FILL IN -- how long the de-identified data will be kept] |
| **Destruction method** | [FILL IN -- secure deletion, shredding, etc.] |
| **Was original data destroyed?** | [Yes / No -- if no, where is it stored and who controls access?] |
| **Data sharing agreement** | [FILL IN -- DUA reference number, or N/A] |
| **Recipient(s)** | [FILL IN -- who receives the de-identified data, or "internal only"] |

---

## 8. Audit Trail

| Event | Timestamp | Operator | Details |
|---|---|---|---|
| Config selected | [FILL IN] | [FILL IN] | Profile: [FILL IN], SHA-256: [FILL IN] |
| De-identification executed | [FILL IN] | [FILL IN] | Input: [N] resources, Output: [N] resources |
| Risk assessment completed | [FILL IN] | [FILL IN] | min_k: [FILL IN], risk_level: [FILL IN] |
| Output reviewed | [FILL IN] | [FILL IN] | Spot-check: [PASS/FAIL] |
| Report signed | [FILL IN] | [FILL IN] | |

---

## 9. Attestation

By signing below, the signatories attest that:

1. The de-identification was performed using the config profile documented in Section 2.
2. The risk assessment results in Section 5 were reviewed and meet the thresholds for the intended use.
3. All residual risks in Section 6 have been documented with appropriate mitigations.
4. The output dataset and this evidence report will be archived according to organisational data governance policy.
5. The output dataset will not be used for any purpose beyond what is stated in Section 1.

| Role | Name | Signature | Date |
|---|---|---|---|
| **Operator** | [FILL IN] | | |
| **Data Protection Officer** | [FILL IN] | | |
| **Principal Investigator** (if research) | [FILL IN] | | |
| **Reviewer** (optional) | [FILL IN] | | |

---

## Appendix A: Generating Technical Metrics

Run the following to produce machine-readable metrics for Sections 3-4:

```bash
cd services/anonymizer

python3 tools/analyze_results.py \
  --input  <original_input.ndjson> \
  --output <deid_output.ndjson> \
  --operator "<Your Name>" \
  --config config/<profile>.yaml \
  --report-json evidence_metrics.json \
  --report-md   evidence_metrics.md

cat evidence_metrics.md
```

Paste the relevant fields from `evidence_metrics.json` into Sections 3 and 4.

The `config_sha256` field in the JSON output is the authoritative hash for Section 2.

## Appendix B: Risk Assessment Command

```bash
# Patient resources only (k-anonymity)
curl -s -X POST http://localhost:8000/analyse/risk \
  -H "Content-Type: application/x-ndjson" \
  --data-binary @<OUTPUT_FILE> | python3 -m json.tool > risk_report.json

# Patient + Condition resources (k-anonymity + l-diversity)
cat Patient.deid.ndjson Condition.deid.ndjson | \
  curl -s -X POST http://localhost:8000/analyse/risk \
    -H "Content-Type: application/x-ndjson" \
    --data-binary @- | python3 -m json.tool > risk_report.json
```

## Appendix C: File Integrity Verification

Record checksums for all input and output files:

```bash
sha256sum input_file.ndjson output_file.ndjson config/<profile>.yaml > checksums.sha256

# Verify later:
sha256sum -c checksums.sha256
```
