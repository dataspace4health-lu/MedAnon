# MedAnon: A Flexible, Rule-Driven Solution for De-identification and Reversible Pseudonymization of FHIR Health Data with AI Assistance

*A technical overview of the Health Data Preparation & Privacy Toolkit (MedAnon).*

---

## Abstract

Electronic Health Records (EHRs) are created primarily to support patient care, but they carry enormous secondary value for health economics and for clinical, epidemiological, and AI-driven research. The growing adoption of the Fast Healthcare Interoperability Resources (FHIR) standard has made exchanging medical data between parties far easier. Yet that exchange is blocked by data-protection regulations, because the data contains information that can identify patients directly or indirectly. De-identification techniques, when correctly applied, are the key to unlocking this value safely. We present **MedAnon**, a flexible and concrete solution for de-identifying health data. It supports a wide range of customizable data-processing behaviors that can be configured and tailored to specific use-case requirements through human-readable rules, enabling faster cooperation between legal and IT professionals. Beyond rule-based transformation, MedAnon adds clinical-narrative scrubbing through Natural Language Processing, reversible pseudonymization via a Trusted Third Party, a self-scoring engine that *measures* re-identification risk against named regulatory frameworks, and AI assistance that runs locally so that protected data never leaves the environment. We believe MedAnon is a valuable tool for meeting current regulatory requirements while creating added value from the secondary use of healthcare data.

**Keywords:** de-identification, pseudonymization, anonymization, FHIR, FHIRPath, k-anonymity, HIPAA, GDPR, clinical NLP, AI-assisted compliance.

---

## I. Introduction

EHRs exist primarily to store patients' medical data and build their medical history so that clinicians can provide appropriate care. But this large volume of medical data has several important secondary missions — health economics, health research, and clinical or epidemiological research on specific diseases. Such research typically needs a large and diverse population sample drawn from heterogeneous sources, often with different (or unstructured) data structures.

The HL7 FHIR standard mitigates the problem of differing data structures: it defines data formats, elements called *resources*, and an API for exchanging them, easing interoperability between parties. However, data-protection regulations block this exchange because the data contains patient identifiers — Personally Identifiable Information (PII). In most cases these identifiers are not useful for the intended secondary purpose; their presence merely hinders use. Simply removing them is not enough: alongside direct identifiers there are **quasi-identifiers** — attributes that are not unique on their own (such as birth date, postal code, or gender) but, when correlated with external data sources, can re-link medical data to a patient even after the direct identifiers are gone.

Re-establishing the link between identity and data exposes patients to serious harm — blackmail, discrimination in employment or insurance, and stigmatization. It is therefore essential to assess the risk of re-identification carefully, an operation that must usually be done case by case.

### A. Regulatory Framework

Regulatory frameworks such as the US HIPAA Privacy Rule and the EU General Data Protection Regulation (GDPR) govern how identifiers must be handled. The common approach is **de-identification**: a set of techniques that removes or transforms identifying information so individual data can no longer be linked to a specific person. It has three sub-categories:

1. **Anonymization** — irreversibly removes the link between the individual and the data.
2. **Pseudonymization** — reversibly replaces identifiers with artificial ones (pseudonyms).
3. **Aggregation** — replaces information with a summary.

HIPAA defines two methods: *Expert Determination* (an expert assesses the residual re-identification risk) and *Safe Harbor* (removal of 18 specific identifiers). GDPR is less concrete: it recognizes pseudonymization as a risk-reducing measure (Art. 4, 25, 32) and, in Recital 26, exempts truly anonymous data from the regulation, but it gives only abstract guidance rather than operative procedures.

Because the frameworks — especially in Europe — give abstract instructions without concrete operative steps, the problem of deciding *what* to de-identify and *how* is left to the solution provider. Several challenges follow:

- **Risk of re-identification attacks** — performing proper anonymization is complex, and re-identification is a well-studied threat.
- **Extracting identifiers from FHIR and the privacy-utility trade-off** — FHIR resources contain many kinds of IDs and identifiers serving different purposes (internal links vs. external correlation); a de-identification strategy must modify internal IDs consistently while redacting external ones according to their meaning and the needs of the downstream use.
- **Keeping pace with FHIR evolution** — the standard changes across releases; a long-term tool must be upgradeable.
- **Subtle pitfalls of pseudonymization** — a naïve implementation, even with a strong hash function, can be disastrous: a field drawn from a small set (e.g., a date) can be brute-forced by hashing every possibility. Inappropriate or misused encryption (e.g., a constant initialization vector) can leak keys. The "perfectly hiding" property and the features recommended by international guidelines must be guaranteed.

### B. Technical Challenges

Applying de-identification to healthcare data also complicates statistical analysis: removing or altering sensitive information can lose crucial indicators or introduce bias. Removing demographic or geographic detail, for instance, may hide health disparities in specific populations. A careful balance between protecting privacy and preserving data integrity is therefore required so that analyses remain valid and informative.

There are also cases where privacy must be protected *and* the data must be shown to come from real, not artificial, sources to validate results — for example, regulators who require a trackback from a clinical trial to a real dataset, or public health registries that publish data rapidly during a pandemic using quantitative anonymization. Because the process usually relies on manual intervention, fully automated de-identification without safeguards is itself a risk to patients' privacy.

### C. Our Contribution

De-identification is a multifaceted challenge demanding a flexible, interdisciplinary approach: it requires experts in data science, security, legal compliance, and healthcare to collaborate while preserving the data's utility. Flexibility is paramount, because threats and regulations evolve.

De-identifying medical data is a team effort between privacy experts (e.g., Data Protection Officers) and data engineers. Rather than inventing new de-identification primitives, **MedAnon provides a tool that facilitates communication between these two worlds while implementing the most widely used techniques** — and extends them where modern clinical data demands it. Our aims are to give legal experts a range of technical options and to give engineers a system that implements those rules easily, operating locally on the data controller's/processor's own premises.

The main contributions are:

- a flexible, open de-identification engine for FHIR resources, driven by human-readable **selection-action rules** that legal and IT professionals can author together;
- a **layered set of de-identification techniques** — suppression, generalization, reversible/irreversible pseudonymization (including an external Trusted Third Party), and **NLP-based scrubbing of free-text clinical narrative** — addressing direct identifiers, quasi-identifiers, and unstructured PHI together;
- a built-in **self-scoring** capability that treats privacy as a hard constraint and *measures* the output against HIPAA/GDPR-aligned criteria rather than merely asserting de-identification;
- **AI assistance** for authoring, discovery, and verification that runs against a local model, so protected data never leaves the environment.

The remainder of this article is organized as follows: Section II describes the solution and its architecture; Section III details the de-identification techniques; Section IV covers the workflow and life cycle; Section V discusses the use of AI; Section VI presents benefits and use cases; Section VII concludes.

---

## II. Solution Design and Architecture

MedAnon de-identifies FHIR data through **anonymization** and **pseudonymization**. It does not perform aggregation, because the goal is to process data as individual records, not as aggregated summaries.

### A. The De-identification Process

The envisaged process begins with close collaboration between legal and technical experts, who together identify which information in the FHIR data must be de-identified to satisfy legal privacy requirements. This includes selecting the technique to apply to each field and mapping those decisions into a **configuration file** of selection-action rules. After this preparation phase, data is processed by the engine, which outputs a de-identified version suitable for statistical analysis, sharing, or combination into larger datasets.

### B. Architecture (General View)

MedAnon accepts FHIR resources as input and returns de-identified/pseudonymized data as output. Resources are processed according to the rules in the configuration file, expressed as a set of **selection-action rules**: the *selection* identifies the FHIR fields to process using standard FHIR mechanisms; the *action* defines the transformation to apply (redaction, perturbation, hashing, substitution, generalization for anonymization; encryption or TTP mapping for pseudonymization). The breadth of actions gives expressive control over the privacy-utility trade-off, so the solution adapts to different contexts and needs.

The solution is not a single program but a small set of cooperating services, each with one clear responsibility. Separating these concerns lets each part be secured and scaled independently and keeps **identified data physically isolated from de-identified data**.

**Diagram 1 — Services the engine connects to**

```
                         ┌─────────────────┐
                         │   Web UI        │  legal + IT experts build
                         │ (browser app)   │  policies, run jobs, review
                         └────────┬────────┘
                                  │
                                  ▼
        ┌──────────────────────────────────────────────────┐
        │            DE-IDENTIFICATION ENGINE               │
        │   (selection-action rules → transform → output)   │
        └──────────────────────────────────────────────────┘
            │           │            │           │        │
   reads    │           │            │           │        │  measures
   ┌────────▼───┐  ┌────▼─────┐  ┌───▼────┐  ┌───▼────┐ ┌─▼────────┐
   │ IDENTIFIED │  │   TTP    │  │  NLP   │  │   AI   │ │ SCORING  │
   │  SOURCE    │  │ pseudo-  │  │ text   │  │ assist │ │ privacy/ │
   │ (isolated) │  │ nymizer  │  │ scrub  │  │(local) │ │ utility/ │
   └────────────┘  └──────────┘  └────────┘  └────────┘ │ quality  │
                                                         └──────────┘
        │   writes de-identified data only
        ▼
   ┌────────────────┐        ┌──────────────────────────────────┐
   │ DE-IDENTIFIED  │        │  Supporting services:            │
   │    TARGET      │        │  job queue · cache · identity/    │
   │  (separate)    │        │  auth · analytics · monitoring    │
   └────────────────┘        └──────────────────────────────────┘
```

In general terms the engine coordinates: an **identified source** (kept on an isolated network, never exposed directly); a **de-identified target** that holds only transformed data; a **TTP pseudonymization service** that manages the reversible mapping between identifiers and pseudonyms; an **NLP service** that finds identifiers hidden in narrative text; a **scoring service** that measures each result; an **AI assistant** running on a local model; and supporting infrastructure for large jobs, authentication, and observability. The solution is **cloud-agnostic**: it is designed for native integration with other FHIR services through Web APIs and deploys easily using containers, on-premises or in an orchestrator.

### C. Methodology and Approach

Anonymization and pseudonymization are defined by **selection-action rules**. For the selection phase, MedAnon uses **FHIRPath** — the expression language defined by HL7 for navigating FHIR resources — to select classes of data or individual elements. Using the official standard for selection ensures full compatibility with FHIR while granting flexible filtering. The rules are written in a human-readable **YAML configuration file**, and users can specify custom rules tailored to their application and to extend compliance (for example, to GDPR). A rule has the shape:

```yaml
rules:
  - name: "redact patient name"
    match: "Patient.name"     # selection — FHIRPath
    action: "redact"          # action
    params:                   # action-specific options
      replacement: "[REDACTED]"
```

A default configuration applies anonymization to the **18 PHI identifiers** of the HIPAA Safe Harbor method out of the box. Correct behavior is validated against official sample FHIR resources and against synthetic patient data, acknowledging the known concern that de-identification reduces informative content and may introduce bias.

---

## III. De-identification Techniques

MedAnon applies a layered set of techniques, because no single method covers direct identifiers, quasi-identifiers, and free-text PHI at once. Two dimensions organize them: **reversibility** (anonymization vs. pseudonymization) and whether a technique works on each value independently or offers *enhanced* anonymity across the whole dataset (preventing single-out, linkability, and inference).

### A. Anonymization (irreversible)
- **keep** — leave the data as is (for non-sensitive fields).
- **redact** — remove the data.
- **perturb** — add random noise to a value.
- **cryptoHash** — replace the value with a keyed cryptographic digest; the same input always yields the same token, preserving consistency without revealing the original. A keyed (HMAC) construction prevents the brute-force reversal that plagues naïve hashing of small-domain fields such as dates.
- **substitute** — replace the value with a constant.

### B. Generalization
Quasi-identifiers are *generalized* rather than removed, to preserve analytical value: dates are reduced to year or year-month, postal codes to a regional prefix, ages to ranges. This keeps the data useful while eliminating the precision that enables re-identification.

### C. k-Anonymity (enhanced anonymity)
Generalizing fields independently does not guarantee anonymity. MedAnon includes a **k-anonymity** technique that generalizes quasi-identifiers across the whole dataset until every individual is indistinguishable from at least *k–1* others on those attributes — "hiding in the crowd" — choosing the combination of generalizations that reaches the required *k* with the least information loss. This directly counters the single-out and linkability risks that per-field methods leave open.

### D. Clinical-text scrubbing (NLP)
Structured fields are only half of the disclosure surface; clinical narratives routinely embed names, dates, and identifiers in free text. MedAnon applies **Natural Language Processing** (named-entity recognition) to detect such identifiers and replace them with surrogate tokens. This step is **fail-closed**: if detection cannot be performed, the text is withheld rather than released unscrubbed, so narrative PHI can never leak by being silently skipped.

### E. Pseudonymization and De-pseudonymization (reversible)
For identifiers that must remain consistent yet recoverable under control:

- **encrypt** — substitute the value with its ciphertext, reversible only with the decryption key. The supported schemes provide the "perfectly hiding" property required by international guidelines (e.g., ENISA, ISO 25237), favoring asymmetric algorithms and modern constructions over fragile modes such as AES-ECB.
- **mapping** — substitute the value with a dynamically generated pseudonym, keeping the association so the operation can be reversed.

### F. Pseudonymization with a Trusted Third Party
Rather than managing the pseudonym mapping locally, MedAnon can delegate it to an external **TTP Pseudonym Management Platform** (gPAS). The mapping between original values and pseudonyms is held by the trusted service, so re-identification is possible **only under controlled conditions** — for example, to contact a patient after an adverse event, or to link a longitudinal cohort over time. The integration follows a generate → pseudonymize → de-pseudonymize flow and batches requests so that large datasets remain efficient.

### G. Producing valid, usable output
A guiding constraint is that de-identification must yield data that is still *valid* and *referentially intact* — not merely transformed text. Coded fields keep valid codes, internal IDs are modified consistently, and cross-references between resources are rewritten so relationships survive the transformation. De-identified data that downstream systems reject has no value.

---

## IV. Workflow and Life Cycle

A single resource and a large batch follow the same conceptual life cycle. The engine never sends identifiers to a service it does not control, and never releases output that has not been checked.

**Diagram 2 — De-identification life cycle**

```
 ┌──────────┐
 │  INPUT   │  FHIR resource (or batch), from a source or uploaded
 └────┬─────┘
      ▼
 ┌──────────────┐   SELECTION: load the rules, use FHIRPath to identify
 │  1. SELECT   │   the fields to process; decide the action per field.
 └────┬─────────┘
      ▼
 ┌─────────────────────────────────────────────┐
 │  2. ACTION (transform)                       │
 │   • redact / substitute / perturb            │
 │   • cryptoHash (irreversible)                │
 │   • generalize quasi-identifiers             │
 │   • scrub narrative          ──► NLP service │
 │   • encrypt / mapping / TTP  ──► gPAS        │
 └────┬────────────────────────────────────────┘
      ▼
 ┌──────────────┐   Rewrite references, keep coded fields valid,
 │ 3. FINALIZE  │   ensure output is still valid, usable FHIR.
 └────┬─────────┘
      ▼
 ┌──────────────┐   Privacy / utility / quality scoring.
 │  4. MEASURE  │   PRIVACY IS A HARD GATE:
 │   & GATE     │   if it fails ──► result is blocked, not released.
 └────┬─────────┘
      │ pass
      ▼
 ┌──────────────┐
 │   OUTPUT     │  de-identified data → target / export
 └──────────────┘
```

**Why this order.** Fields are selected first, transformed in the middle, and the result is released only after it has been *measured and gated*. The privacy gate is what turns "we de-identified it" into "we verified it meets the threshold before letting it out."

**Large jobs.** For bulk work — exporting an entire patient cohort — the same life cycle runs as a **background workflow**: the job is submitted, processed in stages by dedicated workers, made resumable if a step fails, and collected when ready. Every record still passes through the identical select → action → finalize → measure life cycle, so scale never weakens the guarantees.

---

## V. The Use of AI

AI is used in MedAnon as an **assistant and a safety net**, never as an uncontrolled processor of patient data. It lowers the expertise barrier and adds a second line of defence, while being deliberately constrained so that it does not introduce new privacy risk.

The AI capabilities are:

- **Configuration generation from natural language** — describe the goal in plain language ("share this cohort with an EU partner under GDPR") and the system proposes a validated de-identification policy, making the tool usable by non-experts.
- **An interactive configuration assistant** that suggests and refines rules in context.
- **Field/PII discovery** — given a sample resource, the AI suggests which fields likely carry identifiers and which action to apply, reducing the chance of missing a sensitive field.
- **A second-pass PII check** — after de-identification, AI re-examines the output for residual identifiers that rule-based and NLP layers might have missed, layered on top of deterministic checks rather than replacing them.
- **Plain-language rule explanation and compliance analysis** against frameworks such as HIPAA and GDPR.

The guiding principles are:

- **No protected data to untrusted models.** The AI features can run entirely against a **local, self-hosted model**, so patient data never leaves the environment; the PII-checking step in particular is bound to a local model.
- **AI assists; deterministic rules decide.** De-identification itself is performed by auditable rules and established techniques. The privacy guarantees rest on the deterministic pipeline and the measurable scoring — not on the model's judgment.
- **Graceful fallback.** Every AI feature degrades to a deterministic alternative when AI is disabled, so the core solution never depends on it.

In short, AI improves *usability and assurance* while the privacy guarantees remain grounded in verifiable, auditable techniques.

---

## VI. Benefits and Use Cases

In the EU privacy framework (GDPR), the solution benefits the data controller/processor — by reducing the risk of involuntary disclosure — and the data subject, whose privacy is protected; under Recital 26, properly anonymized data is no longer personal data, so the regulation no longer applies. The solution supports a broad set of scenarios:

- research data export to a research warehouse,
- GDPR- and HIPAA-compliant sharing with external partners,
- integration with data-space connectors,
- real-time, per-request de-identification in data pipelines,
- multi-site pseudonymization through a TTP,
- scrubbing of clinical narrative, and
- compliance auditing of de-identified output via the scoring engine.

Its principal benefits: **auditable** (a readable, reviewable policy); **reversible where the law allows** (controlled re-linkage, or irreversible hashing where it does not); **comprehensive** (layered techniques covering structured and unstructured data); **measured** (a defensible, regulation-mapped privacy/utility/quality score per run); and **AI-assisted but privacy-safe** (easier to use, harder to get wrong, with no protected data leaving the environment).

---

## VII. Conclusion

Current regulatory frameworks, especially in Europe, do not provide explicit operative instructions for processing the personal information in healthcare data, making de-identification complicated and non-standardized and often requiring human experts. What can be done is to simplify the operative part — once the data to be processed and the processing mode have been identified — by providing flexible support tools. MedAnon does this: it facilitates collaboration between legal and IT experts in defining de-identification rules to be applied automatically to FHIR data using a human-readable syntax, offering a range of anonymization and pseudonymization actions configurable through flexible parameters, with pseudonymization optionally delegated to an external Trusted Third Party. Beyond rule-based transformation, it adds NLP scrubbing of clinical narrative, a self-scoring engine that makes "de-identified" a measured property rather than an assertion, and locally-run AI assistance that improves usability without ever exposing protected data. We believe MedAnon is a valuable tool for meeting current legislative requirements and creating added value from the secondary use of healthcare data.

---

## References

- Samarati, P. & Sweeney, L. *k-anonymity: protecting privacy through generalization and suppression* (1998); Sweeney, L. *k-anonymity: a model for protecting privacy* (2002).
- U.S. Department of Health & Human Services. *45 CFR §164.514(b)* — HIPAA Safe Harbor de-identification standard.
- Regulation (EU) 2016/679 (GDPR), Articles 4(5), 25, 32, and Recital 26.
- Article 29 Data Protection Working Party. *Opinion 05/2014 on Anonymisation Techniques (WP216)*.
- ENISA. *Pseudonymisation techniques and best practices.*
- ISO/TS 25237 — *Health informatics — Pseudonymization.*
- HL7 International. *Fast Healthcare Interoperability Resources (FHIR)* and *FHIRPath* specifications.
- University Medicine Greifswald. *gPAS — Generic Pseudonym Administration Service.*
