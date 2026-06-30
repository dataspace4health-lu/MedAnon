# Trust Gate: Research References

Publications, standards, and frameworks that ground the **Trust Gate** (the pre-privacy
intake data-quality barrier). Every entry below is drawn from the Trust Gate's own
documentation (`trust-gate.md` §19, `quality-evaluation-methodology.md` §14,
`trust-gate-architecture.md`) and confirmed against the source under
`services/trust-gate/src/`.

This list is the Trust Gate by itself. The de-identification **scoring system**
(`scoring-system.md`, the *output* privacy gate) is a separate subsystem with its own
lineage (k-anonymity, re-identification risk, HIPAA Safe Harbor, information-loss
control); those references live with that document and are deliberately not repeated
here.

---

## Data Quality Frameworks and Taxonomies

**[1]** Kahn MG, Callahan TJ, Barnard J, et al. *A Harmonized Data Quality Assessment
Terminology and Framework for the Secondary Use of Electronic Health Record Data.*
eGEMs 2016;4(1):18. doi:10.13063/2327-9214.1244.
The field-standard taxonomy used by OHDSI and PCORnet. Organizes every DQ check along
two axes: category (conformance / completeness / plausibility) and context (verification
/ validation). Every Trust Gate check is tagged to this taxonomy.

**[2]** OHDSI Data Quality Dashboard (DQD). Blacketer C, Garza M, Li J, et al.
*Increasing trust in real-world evidence through evaluation of observational data quality.*
J Am Med Inform Assoc 2021;28(10):2221-2228. doi:10.1093/jamia/ocab132.
Source of the violation-rate-vs-threshold check-scoring model and the
`measureValueCompleteness` analogue. The headline metric (percentage of checks
passing, not a weighted average of arbitrary domain weights) is adopted directly.
https://ohdsi.github.io/DataQualityDashboard/

**[3]** Wang RY, Strong DM. *Beyond Accuracy: What Data Quality Means to Data Consumers.*
Journal of Management Information Systems 1996;12(4):5-33. doi:10.1080/07421222.1996.11518099.
The foundational definition of data quality as fitness for the consumer's declared use,
which underlies the purpose-bound fitness verdict and the `approved_for` /
`not_approved_for` fields in the Quality Passport.

**[4]** DAMA International. *Data Management Body of Knowledge (DMBOK)*, 2nd ed. Technics
Publications, 2017.
Source of the eight data-quality dimensions used by the scorecard: completeness,
conformity, consistency, accuracy, uniqueness, integrity, currency, and provenance.

**[5]** ISO/IEC 25012. *Software engineering -- Software product Quality Requirements and
Evaluation (SQuaRE) -- Data quality model.* International Organization for
Standardization, 2008.
Used alongside DAMA DMBOK to define the dimension framing for the per-dimension
scorecard and letter grades.

**[6]** ISO/IEC 25024. *Measurement of data quality.* International Organization for
Standardization, 2015.
Companion measurement standard to ISO/IEC 25012; cited for the dimension definitions.

**[7]** Juran JM. *Juran on Quality by Design.* Free Press, 1992.
Classical quality-as-fitness-for-purpose definition, referenced in the purpose-bound
fitness section of the Trust Gate methodology.

**[8]** Houston SM, Winter VS, Borst C, et al. *The State of the Science in Health Care
Data Quality.* Perspectives in Health Information Management 2011.
The PDSA-based Data Quality Management Framework (DQMF) that the Trust Gate findings
and triage workflow implements: open findings, root-cause classification, and status
tracking across runs are the plan/do/study/act cycle of this framework.

**[9]** HL7 EHR Work Group. *Data Quality.* HL7 Confluence.
https://confluence.hl7.org/display/EHR/Data+Quality
The HL7 DQ dimensions (completeness, correctness, concordance, plausibility, currency)
that informed the check taxonomy and the concordance check family.

---

## Statistical Methods

**[10]** Iglewicz B, Hoaglin DC. *How to Detect and Handle Outliers.* ASQC Quality Press,
1993.
Source of the modified z-score `0.6745 * (x - median) / MAD` with cut-off 3.5, used
by the `plausibility.value_outlier` and `plausibility.distribution_drift` checks.
The modified z-score tolerates skew and heavy tails far better than the mean+/-sigma
estimator that clinical lab distributions exhibit.

**[11]** Tukey JW. *Exploratory Data Analysis.* Addison-Wesley, 1977.
Source of the Tukey `K x IQR` fence used as the fallback estimator when MAD is zero
(mostly-identical values), in both the outlier and drift checks.

**[12]** Vitter JS. *Random sampling with a reservoir.* ACM Transactions on Mathematical
Software 1985;11(1):37-57. doi:10.1145/3147.3165.
Vitter's Algorithm R: the seeded reservoir sampler used to maintain a fixed-size
unbiased sample of value distributions across batches (`TRUST_GATE_SAMPLER_SEED`,
default 500 values per `(code, unit)` group). Enables reproducible outlier and drift
detection regardless of total volume.

---

## Regulatory Standards and Governance Frameworks

**[13]** ICH E6(R3). *Integrated Addendum to ICH E6(R2): Guideline for Good Clinical
Practice.* International Council for Harmonisation, 2023.
Risk-based quality management (RBQM) and critical-to-quality (CtQ) elements. The
Trust Gate `critical-to-quality` elevation mechanism implements ICH E6(R3): for a
declared use case, CtQ checks are elevated so a failure forces BLOCK regardless of
overall score.

**[14]** FDA. *Submitting Documents Using Real-World Data and Real-World Evidence to FDA
for Drugs and Biologics.* Guidance for Industry, 2019.

**[15]** EMA. *Data quality framework for EU medicines regulation.* European Medicines
Agency, 2023.
Both [14] and [15] are referenced for the regulatory real-world evidence (RWE) use case
profile (`regulatory_rwe`) and its critical-to-quality check set.

**[16]** FDA. *21 CFR Part 11: Electronic Records; Electronic Signatures.* Code of Federal
Regulations.

**[17]** ALCOA+. *Attributable, Legible, Contemporaneous, Original, Accurate, plus
Complete, Consistent, Enduring, and Available.* FDA / MHRA data integrity guidance.
The append-only audit trail design (attributable, contemporaneous, traceable) is
grounded in ALCOA++ / 21 CFR Part 11.

**[18]** European Parliament and Council. *Regulation (EU) 2025/327 of 11 February 2025
on the European Health Data Space and amending Directive 2011/24/EU and Regulation
(EU) 2024/2847.* Official Journal of the European Union, 5 March 2025; in force 26 March
2025. Article 56: data quality and utility label.
http://data.europa.eu/eli/reg/2025/327/oj
Each Quality Passport carries an EHDS Art. 56 style label: a quality grade, a fitness
tier (high / moderate / low), a maturity level, and FAIR interoperability flags.

---

## FHIR Standard, Tooling, and Implementation Guides

**[19]** HL7 International. *Fast Healthcare Interoperability Resources (FHIR) R4.*
https://hl7.org/fhir/R4/
The base specification for the structural, format, and reference checks. FHIR R4
primitive format regexes are used directly in `conformance.value_format`. The
`$validate` and `CodeSystem/$validate-code` operations are the external calls for
`conformance.structural`, `conformance.profile`, and `conformance.terminology`.

**[20]** HL7 International. *FHIR Safety Checklist.*
https://www.hl7.org/fhir/safety.html
Source for the `conformance.status_not_entered_in_error` check: resources with
`status=entered-in-error` are retracted clinical records and must not be processed.

**[21]** Inferno Community. *fhir-validator-wrapper.*
https://github.com/inferno-community/fhir-validator-wrapper
The optional FHIR validator sidecar used for `conformance.structural` and
`conformance.profile`. The Trust Gate parses both the bare `OperationOutcome`
format and the Inferno wrapper envelope.

**[22]** Verily Life Sciences. *Elevating FHIR Data Quality: A Tiered Approach.*
https://verily.com/perspectives/elevating-fhir-data-quality
Source of the tiered binary / N-ary / population rule model, FHIRPath `resolve()` /
`memberOf()` validation, the concordance dimension (`plausibleGenderUseDescendants`
example), and the `withinVisitDates` N-ary check (on the roadmap).

**[23]** SNOMED International. *SNOMED CT Technical Implementation Guide.*
https://confluence.ihtsdotools.org/display/DOCTSG
Source of the Verhoeff check-digit algorithm used by `conformance.code_wellformed`
to validate SNOMED CT concept identifiers (SCTIDs) offline.

**[24]** PIQI / ASTP-ONC. *Patient Information Quality Improvement (PIQI) Healthcare Data
Quality Taxonomy (HDQT) v2.0.* ASTP/ONC, 2024. https://piqiframework.org/
**Confirmed in code, not inferred.** `passport.py` pins `FRAMEWORK_VERSIONS["hdqt"] =
"2.0"` and tags every check with an HDQT category (availability / accuracy / conformity
/ plausibility) and dimension; the PIQI Simple Assessment Module (SAM) pattern -- a
composable check returning pass / fail / "could not assess" -- is the direct analogue
of the Trust Gate's PASS / FAIL / NA result (NA = Kahn's "could-not-assess"), and is
realized by `checks/governance.evaluate_provenance_sam` and the SAM provenance phase
(`trust-gate-architecture.md` Phase G). The related HL7 PIQI Implementation Guide
(SAM Guide v1.1, 2025; balloted Sep 2025, https://build.fhir.org/ig/HL7/piqi/) is the
emerging standardization of the same framework.

---

## Clinical Terminologies and Code Systems

**[25]** Regenstrief Institute. *Logical Observation Identifiers Names and Codes (LOINC).*
https://loinc.org
Used in code-system validation and for keying per-`(code, unit)` value distributions.

**[26]** National Library of Medicine. *RxNorm.*
https://www.nlm.nih.gov/research/umls/rxnorm/
Recognized clinical code system in `conformance.terminology` and `conformance.code_wellformed`.

**[27]** UCUM. *Unified Code for Units of Measure.* Regenstrief Institute.
https://ucum.org
Used in unit validation and the `plausibility.definitional_bounds` check (e.g. a
percentage value must be in [0, 100] by the mathematical definition of the unit).

---

## OMOP CDM and Observational Research

**[28]** OHDSI. *OMOP Common Data Model.*
https://ohdsi.github.io/CommonDataModel/
The CDM table structure used by the OMOP assessment path (`assess_omop`). The DQD
check set in `config/dqd_checks.yaml` follows the OHDSI DQD pattern over OMOP tables
(`measureValueCompleteness`, FK integrity, date format, value range, table presence).

**[29]** OHDSI. *The Book of OHDSI.*
https://ohdsi.github.io/TheBookOfOHDSI/
Referenced for the patient-as-unit-of-analysis design (`Patient/$everything` per
compartment) and the population-level evidence generation principles that inform
the Trust Gate's dataset-level assessment model.

**[30]** OHDSI. *Data Quality Dashboard changelog.*
https://ohdsi.github.io/DataQualityDashboard/news/index.html
Documents the reversal of universal `plausibleValueLow`/`plausibleValueHigh`
measurement ranges because "many of these ranges included plausible values and as
such were causing unexpected check failures." The Trust Gate adopted the same
position: no bundled universal clinical ranges, only data-driven estimators and
operator-supplied authoritative ranges.

---

## Clinical Data Quality Literature

**[31]** Bussy S, Guilloux A, Gaiffas S, Bacry E. *lab2clean: An automated algorithm to
clean retrospective clinical laboratory results.* BMC Medical Informatics and Decision
Making 2024;24:172. doi:10.1186/s12911-024-02652-7.
The data-driven, distribution-relative approach to laboratory value plausibility,
concluding that usable bounds require "clinical and terminologist expertise and a large
dataset to prepare laboratory value distributions." The Trust Gate's
`plausibility.value_outlier` implements this approach (modified z-score / Tukey IQR
over the per-(code, unit) distribution) rather than a bundled range table.

**[32]** HL7 FHIR R4 Security Considerations.
https://hl7.org/fhir/security.html
Referenced for PHI-safe passport design: violation details record record IDs, field
paths, and example values of evaluable defects only; never free text, names, or
narrative content.

---

## Recent and Emerging Work (2024-2025)

These postdate most of the foundational citations above and corroborate the Trust
Gate's conformance / completeness / plausibility design from independent groups. They
are tracked as confirmation and as candidate sources for future check families, not as
load-bearing citations for the current implementation.

**[33]** Hond AAH de, et al. *Development and initial validation of a data quality
evaluation tool in obstetrics real-world data through HL7-FHIR interoperable Bayesian
networks and expert rules.* JAMIA Open 2024;7(3):ooae062. doi:10.1093/jamiaopen/ooae062.
An independent FHIR-native data-quality tool organized on the same conformance /
completeness / plausibility triad (Kahn), exposed as a real-time FHIR API. Validates
the architectural choice of a FHIR-API quality service; its Bayesian-network and
expert-rule scoring is a candidate direction beyond the current deterministic checks.

**[34]** OHDSI. *Data Quality Dashboard (DQD)* -- ongoing releases.
https://github.com/OHDSI/DataQualityDashboard/releases
The DQD check catalogue and thresholds continue to evolve; the OMOP/DQD check set
(`config/dqd_checks.yaml`, ref [2]) should be reconciled against current DQD releases
periodically rather than pinned to a single snapshot.
