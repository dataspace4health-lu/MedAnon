# De-identification Scoring System

## What it is

The scoring system evaluates the *output* of a de-identification run  not the input config, but the actual transformed FHIR resources. It answers three questions:

1. **Privacy**  Could a motivated attacker re-identify this patient? (hard gate: PASS/FAIL)
2. **Utility**  How much analytical value survived the de-identification?
3. **Quality**  Did the pipeline execute correctly and completely?

These three dimensions combine into a single composite score (0–100). The score is computed on demand via `POST /v1/jobs/{job_id}/score` after a bulk export job completes, or ad-hoc on a single resource via `POST /v1/score`.

The scoring engine (`engine`, `privacy`, `utility`, `quality`, `models`, `constants`) lives once in the shared inner package `packages/medanon-core/src/scoring/`, so the anonymizer and the standalone `scoring` microservice run identical code. The anonymizer-side glue - the score gate (`pipeline/scoring/gate.py`) and the Markdown audit report (`pipeline/scoring/audit.py`) - stays in the anonymizer.

---

## Architecture: Constraint-Based Multiplicative Model

The fundamental design choice is **privacy as a hard constraint, not a dimension to trade off against utility**.

```
Privacy gate (PASS/FAIL)
    │
    ├─ FAIL → composite = 0, utility and quality not computed
    │
    └─ PASS → composite = privacy_norm × utility × quality   (×100)
```

`privacy_norm = 1 − (risk_score / risk_threshold)`

The multiplicative formula means **no dimension can compensate for weakness in another**. A resource that processes without errors (quality = 1.0) but retains PHI-bearing narrative text (utility = 0.9) still scores zero if the privacy gate fails. Likewise, a resource that passes privacy but has half its rules skip due to mismatched FHIRPath expressions will show a low quality score even if the privacy risk appears acceptable.

### Why multiplicative and not additive?

The additive model (`0.4×privacy + 0.3×utility + 0.3×quality`) is the natural first choice because it produces smooth scores. It is also a mistake for compliance contexts. Domingo-Ferrer and Torra (2001) demonstrated that additive aggregation of information-loss metrics allows a high-utility dataset to compensate for unacceptably high disclosure risk  precisely what regulatory frameworks prohibit. El Emam et al.'s review of re-identification attacks (2011) showed that many published de-identified datasets that scored well on aggregate utility metrics still allowed full re-identification of specific individuals.

The multiplicative model with a hard privacy gate mirrors how real compliance decisions work: a dataset either meets the privacy threshold or it does not, and passing that threshold is a prerequisite for asking about utility at all.

---

## Module 1  Privacy Risk (Hard Gate)

**File:** `scoring/privacy.py` (in `packages/medanon-core`)  
**Threshold:** `MEDANON_SCORE_RISK_THRESHOLD` (default 0.3)  
**Formula:** `risk_score = max(attacker_risk, identifier_risk, text_risk)`

The risk score is the *maximum* of three orthogonal sub-evaluators  the worst dimension determines the outcome. This prevents a low text risk from hiding a critical unmasked identifier.

### 1a. Attacker Model

Per-resource path (without a full batch of Patient records):

Extracts the three HIPAA Safe Harbor quasi-identifiers (QIs)  gender, birth year, zip prefix  and counts how many are suppressed/redacted. Applies a calibrated risk table:

| QIs suppressed | Risk |
|---|---|
| 3 of 3 | 0.00 |
| 2 of 3 | 0.10 |
| 1 of 3 | 0.30 |
| 0 of 3 | 0.60 |

Batch path (when scoring a full job with multiple Patient resources):

Runs full **k-anonymity** over the entire Patient cohort using three attacker models derived from the literature:

- **Prosecutor model**  worst-case: attacker has a specific target and checks if they are in the dataset. Risk = 1/k where k is the minimum equivalence class size.
- **Journalist model**  random-target: attacker picks a random record and tries to link it. Risk ≈ proportion of records in small equivalence classes.
- **Marketer model**  population-level: attacker aims to correctly re-identify a fraction of the dataset. Risk ≈ average 1/k across all classes.

The batch attacker risk is `max(prosecutor, journalist, marketer)`.

**Why k-anonymity?** Samarati and Sweeney (1998, 2002) proved that a dataset satisfies k-anonymity if every released record is indistinguishable from at least k−1 others on the QI attributes. This is the foundational metric accepted by HIPAA Safe Harbor guidance and GDPR pseudonymization assessments. The three-model framework was recommended by the Article 29 Working Party (WP216, 2014) for risk-based assessments under GDPR.

**Why the per-resource table fallback?** Most API calls score a single resource without a full patient cohort. Full k-anonymity requires at minimum tens of records to be meaningful. The QI suppression table provides a defensible approximation: a Patient with all three QIs suppressed has no population-linkage vector and scores 0.0 regardless of k.

### 1b. Identifier Coverage

Maps HIPAA Safe Harbor's 18 identifier categories to FHIR resource paths (`constants.py:HIPAA_SENSITIVE_PATHS`) and checks whether the transformation manifest covers each sensitive field that is *present* in the resource.

- Reads `manifest_entries` from the resource's `meta.tag` (written by the pipeline during de-identification)
- For each HIPAA-sensitive path present in the resource, checks whether a manifest entry covers it (exact match or prefix match)
- `risk = uncovered_sensitive_paths / total_sensitive_paths`

A PHI-bearing resource type (Patient, Practitioner, etc.) with **zero manifest entries** scores `identifier_risk = 1.0` immediately  this catches resources that passed through the pipeline without any transformations firing (missing rules, wrong FHIRPath, misconfigured profile).

**Why HIPAA Safe Harbor as the identifier baseline?** 45 CFR §164.514(b) provides an explicit enumeration of 18 identifier categories that, when removed or transformed, are deemed sufficient to remove PHI under the Expert Determination and Safe Harbor methods. This is the only regulatory framework that gives a complete, auditable checklist rather than a principles-based risk assessment. Using it as the identifier coverage baseline means our scoring output can be cited directly in compliance documentation.

### 1c. Text Risk

Scans all string fields longer than 20 characters for residual PII patterns:

**Regex patterns:** SSN (`\d{3}-\d{2}-\d{4}`), phone numbers, email addresses, ISO dates, IP addresses, MRN patterns (`MRN:\d{4+}`).

**NER scan (optional, `MEDANON_SCORE_NER_ENABLED`):** Delegates to the NLP microservice (`nlp-lb:8200`) via `RemoteNlpAdapter.detect()`. Only runs if the NLP adapter is already initialized and the microservice is reachable  scoring never blocks on a cold NLP start. If NLP is unavailable, only the regex scan is performed.

`text_risk = min(1.0, entity_count × 0.15)`

**Why the 0.15-per-entity calibration?** A single date in a Condition narrative is not a re-identification risk on its own (it is a clinical date, not a birth date). A resource with 7+ entities  a name, a date, an MRN, and a phone number appearing together in a clinical note  almost certainly contains residual PHI. The 0.15 slope places the risk threshold crossing at ~2 entities (0.30 = threshold), which is conservative enough to flag clusters of co-occurring PII while not penalising resources with a single non-sensitive date string.

---

## Module 2  Utility (Continuous 0–1)

**File:** `scoring/utility.py` (in `packages/medanon-core`)  
**Formula:** `0.25×retention + 0.30×semantic + 0.15×temporal + 0.30×info_loss`

### 2a. Field Retention (weight 0.25)

When the original resource is available: `retained_top_level_keys / original_top_level_keys` (excluding `meta` and `resourceType`).

When only the de-identified resource is available (on-demand job scoring, no originals stored): estimated from the manifest  fraction of actions that are *not* `redact`. This is a pessimistic estimate.

### 2b. Semantic Preservation (weight 0.30)

Checks that the de-identified resource still makes sense as a FHIR resource:

- Every `coding` element still has a `system` and `code` value
- Bonus credit for known clinical code systems (LOINC, SNOMED CT, ICD-10, RxNorm, UCUM  `constants.py:CLINICAL_CODE_SYSTEMS`)
- Every `reference` string is well-formed (`ResourceType/id` format)
- `resourceType` is present

**Why semantic preservation carries the highest weight?** Field retention only measures whether a top-level key was removed. A Patient record with `name` redacted to `[REDACTED]` still has the `name` field  retention is 1.0. But the resource is useless for any join, cohort selection, or clinical summary. Semantic preservation checks whether the retained fields are still *interpretable* by a downstream FHIR consumer. A Condition with its SNOMED code replaced by a blank string fails semantic preservation even if every field exists.

### 2c. Temporal Consistency (weight 0.15)

Checks that date ordering within a resource makes sense after de-identification:

- `period.start ≤ period.end` within the de-identified resource
- When originals are available, checks that relative ordering between event dates is preserved (e.g., if `onsetDateTime` was before `abatementDateTime` in the original, it should be in the de-identified output)

This matters for research datasets. Generalizing `2019-03-15` to `2019-03` is safe. But if a date perturbation pushes `abatementDateTime` before `onsetDateTime`, the resource encodes a logical impossibility that will break clinical data quality checks.

### 2d. Information Loss (weight 0.30)

Aggregates information loss across all fired actions using calibrated weights from `constants.py:INFO_LOSS_WEIGHTS`:

| Action | Loss weight | Rationale |
|---|---|---|
| `redact` | 1.0 | Complete removal  maximum loss |
| `scrub_text` / `nlp_detect` | 0.6 | Tokenization replaces actual content |
| `generalize` | 0.5 | Reduces precision (dates → year) |
| `substitute` | 0.4 | Value replaced with a non-reversible surrogate |
| `perturb` | 0.3 | Value shifted within a range  partially recoverable at population level |
| `cryptohash` | 0.1 | Deterministic transform  same input always produces same output, enabling longitudinal linkage |
| `gpas_pseudonymize` | 0.1 | Reversible pseudonymization  operator can recover original with gPAS |
| `encrypt` | 0.0 | Fully reversible by authorized parties |

`score = 1 − avg_loss_across_all_manifest_entries`

**Why these specific weights?** They reflect the Domingo-Ferrer and Torra (2001) framework for quantifying information loss in statistical disclosure control, adapted to the FHIR action semantics. The key insight is that reversibility determines analytical utility: a cryptohash preserves longitudinal linkage (same patient → same hash across datasets) with near-zero information loss, whereas a redacted field cannot be used for any linkage or filtering.

---

## Module 3  Quality (Continuous 0–1)

**File:** `scoring/quality.py` (in `packages/medanon-core`)  
**Formula:** `0.40×success_rate + 0.30×rule_coverage + 0.15×schema_validation + 0.15×reference_integrity`  
**Non-compensatory gates:** error rate > 5% caps at 0.60; error rate > 20% caps at 0.20

### 3a. Success Rate (weight 0.40)

`1 − (error_count / total_count)` where errors include resources that produced processing errors or `{"error": ...}` output entries.

The gates are applied *after* the weighted sum: they prevent a pipeline that processes 25% of resources incorrectly from achieving a quality score above 0.20, regardless of how well the remaining 75% scored on coverage and schema checks.

### 3b. Rule Coverage (weight 0.30)

For each rule in the active config profile, checks whether it fired on this resource type:
- Counts rules applicable to this resource type (wildcard `*.` rules and `ResourceType.` rules)
- Checks which applicable rules appear in the manifest's `rule` field
- `coverage = fired_applicable / total_applicable`

If no rules are applicable to the resource type, coverage is 1.0 (correct  a Medication resource does not need Patient-specific rules to fire). If settings are unavailable, coverage is 0.5 (indeterminate  cannot penalise without knowing what rules exist, but cannot reward either).

This check catches silent rule misses: a rule targeting `Patient.birthDate` with a misspelled FHIRPath expression will never fire, but the Patient will still process without error. Without rule coverage, the quality score would be 1.0 on a Patient with an untouched birthDate.

### 3c. Schema Validation (weight 0.15)

Lightweight structural checks without a full FHIR validator:
- `resourceType` is present
- `id` is present (even if pseudonymized)
- No empty arrays for fields FHIR requires to be non-empty (`name`, `identifier`, `telecom`, `address`)  empty arrays are a common artefact of a `redact` action that removes all elements but leaves the container
- `meta.tag` is well-formed (array of objects with string keys)

### 3d. Reference Integrity (weight 0.15)

Checks that all `reference` strings in the resource are valid FHIR references: `ResourceType/id`, `#local`, `urn:uuid:...`, or absolute URLs. Dangling or malformed references break FHIR server uploads and Bundle processing.

---

## Composite Score

```
composite (0–100) = privacy_norm × utility.score × quality.score × 100
```

Where `privacy_norm = 1 − (risk_score / risk_threshold)`. If `privacy.passed = False`, `composite = 0`.

### Example interpretations

| Composite | What it means |
|---|---|
| 90–100 | Strong de-identification, high utility, clean pipeline execution |
| 70–89 | Acceptable; check which utility sub-dimension is pulling the score down |
| 50–69 | Moderate issues  likely rule coverage gaps or significant information loss |
| < 50 | Significant problems  review the audit report for actionable recommendations |
| 0 | Privacy gate failed  PHI likely present; do not release |

---

## Audit Report

After scoring a job, `POST /v1/jobs/{job_id}/score` writes a Markdown audit report to `/output/{job_id}_score_audit.md` (retrievable via `GET /v1/jobs/{job_id}/score/report`).

The report (`pipeline/scoring/audit.py`) contains:

- **Executive summary**  composite, pass/fail breakdown, config profile
- **Privacy findings**  worst attacker risk, k-anonymity stats (if batch), uncovered HIPAA paths, text risk patterns with value previews
- **Utility breakdown**  per sub-dimension averages, action distribution table, information loss analysis
- **Quality breakdown**  rule coverage gap list, schema failures, reference integrity issues
- **Per-resource-type table**  pass/fail rate and average composite by resource type
- **Recommendations**  priority-ordered list with YAML config snippets that can be pasted directly into a config profile to address the top issues

---

## Scoring Without Original Resources

The job scoring path (`score_job`) operates on the NDJSON result file only  original resources are not available. The scoring engine handles this gracefully:

- **Privacy:** attacker model and text risk do not need originals. Identifier coverage uses only the manifest.
- **Utility field retention:** estimated from manifest (fraction of non-redact actions) instead of exact key comparison.
- **Utility temporal consistency:** period-internal ordering still checked; cross-field relative ordering skipped.
- **Information loss:** entirely manifest-based.

This design was a deliberate constraint. Storing both original and de-identified resources doubles the storage footprint of every job and introduces a PHI storage risk (the originals). The manifest attached by the pipeline during processing carries enough metadata to produce a meaningful score without replaying the original data.

---

## Persistence

When `MEDANON_SCORING_ENABLED=true`, every de-identification call automatically scores its output and writes the result to the `medanon.processing_runs` PostgreSQL table (`app-db`). This enables:

- Processing history via `GET /v1/processing-runs`  paginated log of all runs with scores
- Aggregate statistics via `GET /v1/processing-runs/stats`  averages by endpoint and profile
- Trend analysis: composite score drift over time as config profiles or source data evolve

The `processing_runs` table stores: endpoint path, config profile name, resource count, composite score, and per-dimension scores. PHI is never stored  only aggregate statistics.

If `MEDANON_SCORING_ENABLED=false` (default), the scoring engine is still available via the explicit endpoints (`POST /v1/score`, `POST /v1/jobs/{id}/score`) but results are not automatically persisted.

---

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `MEDANON_SCORING_ENABLED` | `false` | Auto-score and persist every de-identification run to `medanon.processing_runs`. Set `true` in production to enable processing history. |
| `MEDANON_SCORE_RISK_THRESHOLD` | `0.3` | Privacy gate threshold. Above this risk score → FAIL. |
| `MEDANON_SCORE_NER_ENABLED` | `true` | Enable Presidio NER in text risk scan (delegates to NLP microservice) |
| `MEDANON_SCORE_NER_THRESHOLD` | `0.5` | Minimum Presidio confidence to count as a detection |

---

## API Reference

| Endpoint | Description |
|---|---|
| `POST /v1/score` | Score a single resource ad-hoc (original optional) |
| `POST /v1/jobs/{id}/score` | Trigger scoring for a completed job, generate audit report |
| `GET /v1/jobs/{id}/score` | Retrieve cached score result |
| `GET /v1/jobs/{id}/score/report` | Retrieve the Markdown audit report |

---

## Research References

- Samarati, P. & Sweeney, L. (1998). *Protecting privacy when disclosing information: k-anonymity and its enforcement through generalization and suppression.*
- Sweeney, L. (2002). *k-anonymity: A model for protecting privacy.* IJUFKS 10(5).
- El Emam, K. et al. (2011). *A systematic review of re-identification attacks on health data.* PLOS ONE 6(12).
- Domingo-Ferrer, J. & Torra, V. (2001). *A quantitative comparison of disclosure control methods for microdata.* Confidentiality, Disclosure, and Data Access.
- Article 29 Working Party (2014). *Opinion 05/2014 on Anonymisation Techniques (WP216).* European Commission.
- HIPAA Safe Harbor: 45 CFR §164.514(b), US Department of Health and Human Services.
- Dankar, F.K. & El Emam, K. (2012). *Practicing differential privacy in health care: A review.* Transactions on Data Privacy 6(1).
- HL7 FHIR R4 Security Considerations: https://hl7.org/fhir/security.html
