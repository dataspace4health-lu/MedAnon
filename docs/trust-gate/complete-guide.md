# The Trust Gate: complete guide

This is the single, complete reference for the Trust Gate. It starts in plain
language for any reader and goes all the way down to the exact calculation each
check performs. Read only the parts you need.

The deeper background references still exist and are cited where relevant:
[quality-evaluation-methodology.md](../quality-evaluation-methodology.md) (methods and
citations) and [services/trust-gate/ARCHITECTURE.md](../../services/trust-gate/ARCHITECTURE.md)
(code layering).

## Contents

1. [Plain-language overview](#1-plain-language-overview)
2. [What it checks: the five questions](#2-what-it-checks-the-five-questions)
3. [The verdict and the report card](#3-the-verdict-and-the-report-card)
4. [Where the rules come from: our sources](#4-where-the-rules-come-from-our-sources)
5. [The four guarantees](#5-the-four-guarantees)
6. [The components](#6-the-components)
7. [The process, step by step](#7-the-process-step-by-step)
8. [How one check is measured](#8-how-one-check-is-measured)
9. [How the scores are rolled up](#9-how-the-scores-are-rolled-up)
10. [How the verdict is decided](#10-how-the-verdict-is-decided)
11. [Fitness for use](#11-fitness-for-use)
12. [Honesty: coverage and advisory](#12-honesty-coverage-and-advisory)
13. [Per-check detail: conformance](#13-per-check-detail-conformance)
14. [Per-check detail: completeness](#14-per-check-detail-completeness)
15. [Per-check detail: plausibility](#15-per-check-detail-plausibility)
16. [Per-check detail: provenance](#16-per-check-detail-provenance)
17. [Per-check detail: external validation](#17-per-check-detail-external-validation)
18. [The Quality Passport output](#18-the-quality-passport-output)
19. [Configuration reference](#19-configuration-reference)

---

## 1. Plain-language overview

Before any medical data is anonymised and shared, the Trust Gate inspects it and
issues a short report card that says whether the data is good enough to use, and
what it is good enough for.

Think of it like a quality inspection at the door of a factory. Nothing goes down
the line until it has been looked over. The Trust Gate never changes the data; it
only inspects and reports.

Why it matters:

1. **Bad data leads to bad conclusions.** If an export is missing key fields, has
   impossible values, or uses the wrong codes, any research built on it is wrong. It
   is far cheaper to catch that at the door than after the analysis.
2. **We anonymise data for a living.** Running broken or mislabelled records through
   anonymisation can quietly produce clean-looking records that are actually wrong.
   The Trust Gate stops that before it starts.

The rest of this guide explains what it inspects, how it scores what it finds, how
it decides, and how it keeps quality under control over time.

## 2. What it checks: the five questions

The gate asks five plain questions. Each is a group of automatic checks, and each
maps to a formal quality category used later in scoring.

1. **Is it well-formed?** (*conformance*) Does every record have the basic parts it
   needs: a type, an identity, valid dates, and real medical codes rather than
   blanks or leftovers? Records marked "entered in error" (mistakes the hospital
   already retracted) are caught here.
2. **Is it complete?** (*completeness*) Are the fields that should be filled in
   actually filled in?
3. **Are the values believable?** (*plausibility*) Do the numbers make sense? A
   temperature of 500, a percentage above 100, or a value far outside everything
   else of its kind gets flagged. We also watch for sudden shifts that suggest a
   changed unit or a miscalibrated device.
4. **Do we know where it came from?** (*provenance*) Is there a record of the source
   system and when it was extracted? This is the paper trail regulated research
   requires.
5. **Did an independent expert confirm it?** (*external validation*) For the
   strictest uses we do not only check the data ourselves; we send it to independent
   tools that confirm the records are correctly formed and the codes really exist.

## 3. The verdict and the report card

After the checks run, the Trust Gate gives one of three answers (a traffic light):

- **Green, PASS** the data meets every quality bar for its intended use.
- **Amber, CONDITIONAL_PASS** usable, but something fell short. Fix the flagged
  items before high-stakes use.
- **Red, BLOCK** a serious problem was found. The data is not processed until fixed.

A single serious defect (records with no identity, or retracted "entered in error"
records) is enough on its own to turn the light red, no matter how good everything
else looks.

The result is a document called the **Quality Passport**. In plain terms it holds
the traffic-light decision, a score and a letter grade (A to F) with a separate
grade per category, a clear statement of what the data is and is not fit for, the
specific problems found and how many records were affected, and an honest note of
what could not be checked this time.

**Fit for purpose is the whole point.** Quality is not one number. A car that is
safe for city driving may not be safe for a race track. The same dataset can be good
enough to *count* how many patients might qualify for a study, but not good enough to
draw *medical conclusions* from. The passport always states the bar for the specific
job you asked for, and lists which other jobs the data would and would not pass for.

## 4. Where the rules come from: our sources

We did not invent the quality rules. The Trust Gate is built on recognised,
published standards that hospitals, regulators, and research networks already use.
That is what makes the verdict credible rather than just our opinion.

| What we build on | What it is, in plain terms | What it gives the Trust Gate |
|---|---|---|
| **HL7 FHIR (R4)** | The international standard format for sharing health records. | The common language our data arrives in, so the checks apply anywhere. |
| **The Kahn framework (2016)** | The widely accepted rulebook for judging health-data quality, used by large research networks (OHDSI, PCORnet). | The five questions above and the definition of "good quality" (conformance / completeness / plausibility, each verification or validation). |
| **OHDSI Data Quality Dashboard** | A proven, published way to score quality by counting how many records break each rule. | Our scoring method (violation-rate versus a per-check tolerance). |
| **DAMA DMBOK and ISO/IEC 25012** | International standards listing the official "dimensions" of data quality. | The dimension names and letter grades on the report card. |
| **PIQI HDQT v2.0 (ASTP/ONC 2024)** | A US healthcare data-quality taxonomy. | Extra metadata tags on each check for downstream analytics. |
| **Medical code books: LOINC, SNOMED CT, ICD-10, RxNorm, UCUM, ATC, EDQM** | The official dictionaries for lab tests, clinical terms, diagnoses, medicines, and units. | The reference for checking codes are real and well-formed. |
| **Standard statistics (Tukey fences; Iglewicz-Hoaglin modified z-score)** | Textbook methods for spotting values that look out of place. | How we flag unbelievable numbers without inventing clinical thresholds. |
| **Fitness-for-use (Juran; Wang and Strong 1996; Kahn 2012)** | The classic idea that quality means "fit for the job it is needed for". | Why the passport is always tied to a stated purpose. |
| **Safety and governance standards (FHIR Safety Checklist; ISO/HL7 21089; ICH E6(R3))** | Established rules for retracted records, provenance trust anchors, and critical-data focus. | The safety checks, the paper-trail requirements, and critical-to-quality promotion. |

Two independent services do the "outside expert" checks so we are not marking our
own homework: an independent **FHIR validator** (the open HL7 / Inferno validator)
that confirms each record is correctly built, and a **terminology service** that
confirms a medical code genuinely exists. If either is unavailable, we do not
pretend the check passed; we mark it "not checked this time".

## 5. The four guarantees

These principles hold across the whole system and are the reason the verdict can be
relied on.

1. **It never changes your data.** It only inspects and reports.
2. **It fails safe, never silently.** A check that cannot run is marked "not
   applicable" (NA) and dropped from scoring, never counted as a pass. A missing
   check can never make the data look better than it is.
3. **It gives the same answer every time.** The same data in produces the same
   verdict out. Parts that rely on chance (statistical outlier hints) are kept as
   side notes and never move the traffic light.
4. **It is honest about what it did not check.** If only part of the inspection ran,
   the report says so, so a partial pass is never mistaken for a full clean bill of
   health.

A fifth structural principle underpins these: **measurement is separate from
policy.** One layer measures quality (percent of checks passing, no invented
weights); a separate layer decides fitness for use. Changing "what is good enough"
never touches "how we measure".

## 6. The components

The Trust Gate is a small, strictly layered service. Each layer depends only on the
layer below it; nothing depends on the orchestrator.

| Layer | Location | Job |
|---|---|---|
| **API** | `src/api/` | HTTP surface: endpoints, request/response schemas, persistence, metrics. |
| **Engine** | `src/engine.py` | The orchestrator. `assess()` for FHIR, `assess_omop()` for OMOP CDM. Runs the pipeline and assembles the passport. |
| **Verdict** | `src/verdict/` | Pure functions over the check list: `runner`, `scoring`, `decision`, `coverage`, `fitness`. |
| **Checks** | `src/checks/` | One measurement concern per module: conformance, completeness, plausibility, timeliness, accuracy, identity, governance, plus the OMOP DQD suite. Each returns `CheckResult` objects. |
| **Domain model** | `passport.py`, `dimensions.py`, `phases.py`, `constants.py` | Pure data: the `CheckResult` and `QualityPassport` shapes, the dimension and phase taxonomies, the thresholds. No I/O. |
| **Reporting** | `src/reporting/` | Renders a passport to Markdown. Depends on the model, never the reverse. |
| **Infra** | `validator_client.py`, `terminology_client.py`, `breaker.py`, `baseline.py` | External FHIR validator and terminology server (behind circuit breakers and timeouts); the cross-batch baseline store. |
| **Store / connectors** | `src/store/`, `src/connectors/`, `src/cdm/` | Passport and findings persistence; file and SQL ingestion; FHIR-to-OMOP mapping. |

The single most important object is the **`CheckResult`**. Everything above it is a
roll-up of a list of these; everything below produces them.

The service exposes three assessment endpoints: `POST /v1/trust/assess` (a single
resource or Bundle), `POST /v1/trust/assess/batch` (a list), and
`POST /v1/trust/assess/omop` (an OMOP CDM dataset).

## 7. The process, step by step

The process has two halves: measuring quality (steps 1 to 9) and ensuring it (steps
10 to 14).

### Measuring quality

**Step 1: The dataset reaches the barrier.** De-identification never starts cold.
The anonymiser calls the intake barrier first
([anonymizer/src/pipeline/intake_gate.py](../services/anonymizer/src/pipeline/intake_gate.py),
`enforce_intake`), which forwards the records to the Trust Gate over HTTP. If no
Trust Gate is configured, or `TRUST_GATE_MODE=off`, this is a no-op.

**Step 2: Configure the assessment.**
[api/service.py](../services/trust-gate/src/api/service.py) `run_assessment` decides
*what* to measure before measuring anything: resolve the declared use case to a
subset of phases and threshold tweaks (`resolve_use_case`), merge thresholds over
the configured defaults, resolve which checks are promoted to critical-to-quality,
and parse the rule pack plus any caller `custom_rules` (a rule that does not parse
raises HTTP 422, never silently dropped).

**Step 3: Normalise the input.** `flatten()` turns a single resource, a Bundle, or a
list into a flat record list plus the Bundle `fullUrl` values (kept so
reference-integrity can resolve `urn:uuid:` references).

**Step 4: Gather inputs and run the checks.** The engine
([engine.py](../services/trust-gate/src/engine.py) `assess`) packs every input into
one immutable `AssessmentContext`, then runs the check suite through the registry
([verdict/registry.py](../services/trust-gate/src/verdict/registry.py)). Each result
is tagged with its phase and data-quality dimension, marked deterministic or
advisory, and filtered to the selected phases.

**Step 5: Measure each check** (see section 8).

**Step 6: Separate reliable checks from statistical hints.** Deterministic checks
drive the verdict; advisory (statistical) checks become side notes. Enforced by the
allow-list in [constants.py](../services/trust-gate/src/constants.py); anything
unclassified defaults to advisory (fail-safe).

**Step 7: Roll the results up** (see section 9).

**Step 8: Record what actually ran** (coverage, see section 12).

**Step 9: Decide and compute fitness** (see sections 10 and 11). The engine
assembles all of this into the Quality Passport.

### Ensuring quality

**Step 10: Enforce the verdict at the barrier.** Back in the anonymiser, the intake
barrier acts on the decision according to `TRUST_GATE_MODE`:

- **block** a BLOCK raises `IntakeBlocked`, the pipeline stops, the data is never
  de-identified.
- **warn** (default) the passport is attached and processing continues.
- **off** no enforcement.

Enforcement is **fail-soft as a service**: if the Trust Gate is unreachable, the
barrier degrades to an advisory CONDITIONAL_PASS. It never silently passes on an
outage, and in block mode never hard-blocks on the gate's own downtime.
`MEDANON_REGULATED_MODE` tightens this: a critical or conformance check that could
not run becomes a block, because you cannot release regulated data on an assessment
that did not complete.

**Step 11: Produce the passport and a headline label.** The passport is serialised
(`to_dict`) and a short label derived (`build_label`). Any identifier-bearing value a
check cites (an MRN, an SSN) is first reduced to a stable, non-reversible token
(`redact_token`), so the passport is safe to store and share.

**Step 12: Persist the passport and open remediation findings.** `persist()` saves
the passport and derives a set of open **findings** (the specific problems, each tied
to the checks that raised them) into the findings store. Writes are idempotent (an
`idempotency_key` collapses retries or racing replicas onto one row) and best-effort
(a store outage is logged but never fails the assessment).

**Step 13: Feed the result back to the data provider.** The passport is actionable:
plain-language blockers with affected-record counts, approved and not-approved use
lists with the specific unmet requirement, Deequ-style suggested thresholds (the
smallest tolerance that would admit this batch, advisory only), and per-phase and
per-sector sub-verdicts (each under the same policy as the headline).

**Step 14: Keep quality under control over time.** The plausibility checks read and
update a persisted per-`(code, unit)` baseline reservoir
([baseline.py](../services/trust-gate/src/baseline.py)), so small batches gain power
from history and a sudden distribution shift is caught as drift. Persisted passports
and open findings support a Plan-Do-Study-Act loop: measure, remediate, re-submit,
confirm the fix, with the idempotency key tying re-submissions to one lineage.

## 8. How one check is measured

This is the atomic unit, identical for every check, and it is the OHDSI Data Quality
Dashboard method (defined on `CheckResult` in
[passport.py](../services/trust-gate/src/passport.py)):

1. Count `applicable` = how many rows or items the check could apply to (the
   denominator).
2. Count `violations` = how many of those broke the rule (the numerator).
3. Compute `violation_fraction = violations / applicable`.
4. Compare it to the check's `threshold`:

```
applicable == 0                   →  NA    (nothing to assess; never a pass)
violation_fraction  >  threshold  →  FAIL
violation_fraction <= threshold   →  PASS
```

Worked example: 100 Observations, 3 with no value and no dataAbsentReason. Then
`applicable = 100`, `violations = 3`, `fraction = 0.03`. The threshold for
`completeness.value_or_absent` is `0.0`, so `0.03 > 0.0` and the check FAILs. Each
violating record also gets a capped, identifier-free note added for the audit
(`add_detail`, capped at 50 per check).

Thresholds live in [constants.py](../services/trust-gate/src/constants.py)
(`DEFAULT_THRESHOLDS`), are overridable per check in config or environment, and
default to zero tolerance for hard structural defects. A few are deliberately
non-zero and justified in code, for example `completeness.element_density = 0.5`
(optional richness fields are not expected to be fully populated) and
`plausibility.value_outlier = 0.01` (flag a value group only when more than 1
percent of it is extreme).

Some checks are marked **critical**: a critical FAIL forces a BLOCK on its own. The
always-critical, zero-tolerance checks are the structural prerequisites: a resource
must have a type, an id, a valid structure, and must not be "entered in error".

## 9. How the scores are rolled up

Every roll-up uses one formula and no domain weights
([verdict/scoring.py](../services/trust-gate/src/verdict/scoring.py)):

> **score = 100 x (checks that PASSED) / (checks that were assessed)**

where "assessed" means PASS or FAIL, never NA. Three things use it:

1. **Category scores** the percent passing within each Kahn category (conformance,
   completeness, plausibility). A category with nothing assessed is `null` (not
   assessed), not 100.
2. **Overall score** the percent passing across all assessed deterministic checks.
   If nothing was assessed, `has_assessed` is false and the headline reads "not
   assessed" rather than a vacuous 100.
3. **Per-dimension scorecard** the same percent per DQ dimension, each with a
   letter grade.

Letter grades are bands aligned to the decision floors
([dimensions.py](../services/trust-gate/src/dimensions.py)):

| Grade | Score | Meaning |
|---|---|---|
| A | at least 95 | excellent |
| B | at least 90 (`PASS_MIN_RATE`) | meets the overall pass floor; a clean PASS sits here |
| C | at least 80 (`CATEGORY_MIN_RATE`) | meets the per-category floor but below the pass floor |
| D | at least 60 | marginal |
| F | below 60 | failing |

Grades are **non-compensatory** for critical failures: if any critical check FAILed,
the grade floors to F no matter how high the pass-rate is (`grade_with_floor`). This
stops a headline reading "A" while the decision is BLOCK.

## 10. How the verdict is decided

The policy layer ([verdict/decision.py](../services/trust-gate/src/verdict/decision.py)
`decide`) applies these rules in order:

```
any critical check FAILed                                     →  BLOCK
overall < PASS_MIN_RATE (90), or any category < CATEGORY_MIN  →  CONDITIONAL_PASS
   _RATE (80), or a resource type below its threshold, or
   required provenance is missing
otherwise                                                     →  PASS
```

The two floors (`PASS_MIN_RATE` 90, `CATEGORY_MIN_RATE` 80) are the only policy
cut-points and are environment-overridable. Two refinements sit on top:

- **Per-resource-type calibration.** With `resource_thresholds` supplied, the gate
  computes a genuine per-type pass-rate (Observations separately from Patients) and
  downgrades to CONDITIONAL_PASS if any type is below its floor.
- **Regulated mode** (`MEDANON_REGULATED_MODE`). A *skipped* critical or conformance
  check becomes a BLOCK, because you cannot release regulated data on an assessment
  that could not run.

One shared function computes the headline verdict and every sub-verdict (per phase,
per sector), so a sector badge can never read more leniently than the headline.

## 11. Fitness for use

Quality is fitness for a *declared* use: a dataset fit for cohort discovery may be
unfit for outcomes research
([verdict/fitness.py](../services/trust-gate/src/verdict/fitness.py)). Each use
profile names the floors it needs:

| Use | Needs conformance | Needs completeness | Needs plausibility | Needs provenance | Needs external validation |
|---|---|---|---|---|---|
| cohort discovery / feasibility | yes | | | | |
| descriptive analytics | yes | yes | | | |
| AI/ML model training | yes | yes | yes | yes | |
| outcomes / comparative-effectiveness | yes | yes | yes | yes | yes |
| regulated submission / external sharing | yes | yes | yes | yes | yes |

The caller's free-text `intended_use` is keyword-matched to one of these. The
passport lists `approved_for` (uses whose floors are met) and `not_approved_for`
(with the specific unmet requirement), so a CONDITIONAL_PASS still says exactly what
the data can and cannot be used for.

## 12. Honesty: coverage and advisory

Two mechanisms stop the verdict from over-claiming.

**Coverage** ([verdict/coverage.py](../services/trust-gate/src/verdict/coverage.py))
reports assessed versus total checks, which depth capabilities were not exercised (no
validator, no terminology server, no reference dataset, no clinical rule pack), which
phases were descoped, and the verification-versus-validation breakdown. A grade over
7 of 22 checks must not read like one over 22 of 22. `external_validation_performed`
is true only when at least one validation-context check was assessed; that flag gates
the higher-stakes fitness approvals.

**Advisory** holds the statistical checks (value outliers, cross-batch drift). They
use randomness and batch-relative distributions, so they are reported for analysis
but excluded from the verdict, scores, and scorecard. This is what keeps the decision
reproducible.

A standing plausibility caveat also applies: a plausibility PASS means no value was
*statistically* implausible for its cohort; it does not certify *clinical coherence*.

## 13. Per-check detail: conformance

Conformance asks: does the data follow the rules of structure, format, codes, and
references? Assembled in
[checks/conformance/__init__.py](../services/trust-gate/src/checks/conformance/__init__.py)
`evaluate()`. The first three are the always-on, offline, zero-tolerance, critical
"SAM prerequisite chain".

| check_id | file | context | critical | threshold | applicable (denominator) | violation (numerator) |
|---|---|---|---|---|---|---|
| `resource_type_present` | presence.py | verification | yes | 0.0 | every dict resource | `resourceType` missing or blank |
| `resource_id_present` | presence.py | verification | yes | 0.0 | resources that have a `resourceType` | `id` missing or blank |
| `status_not_entered_in_error` | presence.py | verification | yes | 0.0 | resources of the 8 clinical types | `status == "entered-in-error"` |
| `value_format` | presence.py | verification | no | 0.0 | each `id` field + each temporal field | value fails the FHIR `id` / dateTime regex |
| `coding_structure` | presence.py | verification | no | 0.0 | every `coding` in the resource tree | missing `system`+`code`, or `code` is a redacted sentinel |
| `code_wellformed` | terminology.py | verification | no | 0.0 | codings whose system is checkable offline | code fails format or check-digit (LOINC, SNOMED Verhoeff, ICD-10, RxNorm) |
| `reference_integrity` | references.py | verification | no | 0.0 | each literal reference (absolute only if `full_urls` given) | target not present in the batch id-set / fullUrl-set |
| `structural` | validation.py | validation | yes | 0.0 | every resource the external validator validated | validator returns any error/fatal structural issue |
| `profile` | validation.py | validation | no | 0.0 | resources that assert a `meta.profile` | validator returns issues against that profile |
| `ig_profile` | validation.py | validation | no | 0.0 | covered resources sampled per type | validator returns issues against the selected IG profile |
| `terminology` | terminology.py | validation | no | 0.0 | codings whose system is in `CLINICAL_CODE_SYSTEMS` | terminology server says the (system, code) is invalid |

Two calculation details: references are normalised to an exact `ResourceType/id` key
(`_normalize_ref`) so `Patient/1` cannot false-resolve against `RelatedPerson/x1`;
and the structural axis excludes terminology issues (`_error_issues`), so a
terminology outage never reads as a structural defect and is never double-counted.

## 14. Per-check detail: completeness

Completeness ([checks/completeness.py](../services/trust-gate/src/checks/completeness.py))
asks: is what should be present actually present? Three checks, all verification.

| check_id | threshold | applicable | violations | note |
|---|---|---|---|---|
| `required_elements` | 0.0 | +1 per resource with a `REQUIRED_ELEMENTS` entry for its type | +1 if any required field is empty | binary present/absent per resource |
| `value_or_absent` | 0.0 | +1 per Observation with a non-empty `status` | +1 if no `value[x]`, no `component[].value[x]`, and no `dataAbsentReason` | panel Observations carry values in `component` |
| `element_density` | 0.5 | += `len(recommended)` per resource | += number of recommended fields left empty | a richness/frequency metric |

`element_density` has a denominator of `recommended_fields x resources`; its fraction
is the dataset's sparseness of optional richness. Its threshold is 0.5 because
optional fields are not expected to be fully populated. `_present()` handles FHIR
choice types (for example `effectiveDateTime` counts as populated when
`effectivePeriod` is present).

## 15. Per-check detail: plausibility

Plausibility ([checks/plausibility.py](../services/trust-gate/src/checks/plausibility.py))
asks: are the values believable? It mixes deterministic checks (drive the verdict)
with statistical ones (advisory only).

| check_id | threshold | drives verdict? | applicable | violations |
|---|---|---|---|---|
| `uniqueness` | 0.0 | yes | count of `Type/id` keys | count beyond the first occurrence of any duplicate key |
| `definitional_bounds` | 0.0 | yes | numeric `(code,unit)` values whose unit has a defined bound | value outside `[lo, hi]` (percent >100, negative concentration) |
| `concordance` | 0.0 | yes | resources of a rule's type whose subject gender is known | a forbidden code appears for that gender |
| rule pack (`date_order`, `not_in_future`, `value_range`, ...) | per rule | yes | config-driven, per rule | the declarative clinical rule is violated |
| `value_outlier` | 0.01 | no (advisory) | numeric values in a `(code,unit)` group with at least 20 reference points | value beyond the robust fence |
| `distribution_drift` | 0.0 | no (advisory) | each `(code,unit)` whose baseline has at least 20 samples | batch median drifted beyond the cut-off vs baseline |

The statistical methods: outliers use a robust **modified z-score**
`0.6745 x (x - median) / MAD` with cut-off 3.5, computed in **log space** for
strictly-positive analytes (right-skewed labs like CRP would otherwise over-flag the
right tail), falling back to a **Tukey IQR fence** `[Q1 - 3xIQR, Q3 + 3xIQR]` when
MAD is 0. There are no hardcoded clinical ranges; the fence is learned from the
data's own distribution per `(code, unit)`. Drift compares the batch median for a
`(code, unit)` against an accumulated cross-batch baseline reservoir (seeded sampler,
so reproducible). Because both are non-deterministic they are marked advisory and can
never move the verdict.

## 16. Per-check detail: provenance

Provenance ([checks/governance.py](../services/trust-gate/src/checks/governance.py))
is deliberately kept out of the Kahn category scores, because provenance presence is
a sharing-readiness signal, not a data-quality measurement. It works on two levels.

**Level 1: auditability evidence (always on).** `governance.evaluate()` builds a
dict, not a scored check: `provenance_present` is true only when the dataset-level
payload has both `source_system` and `extraction_time` (`REQUIRED_PROVENANCE_KEYS`),
and `resource_meta_coverage` is the fraction of resources carrying `meta.lastUpdated`
or `meta.source`. This feeds the decision policy: `provenance_capped` (provenance
phase selected but not present) alone caps the verdict at CONDITIONAL_PASS, and it is
what the AI, outcomes, and regulated fitness profiles require.

**Level 2: the Provenance SAM (opt-in).** `evaluate_provenance_sam()` becomes a
scored `conformance.provenance_present` check only when
`TRUST_GATE_PROVENANCE_SEVERITY=block` (or `score`); in the default `warn` mode it
returns `None`. When active, `applicable` = clinical resources expected to have
provenance (Observation, MedicationRequest, DiagnosticReport, Condition, Procedure),
and `violations` = expected ids minus the ids actually referenced by a `Provenance`
resource's `target`. In `block` mode the check is critical.

## 17. Per-check detail: external validation

External validation is the Kahn **validation context**: checks measured against an
outside authority rather than internal consistency. Two authorities are called, both
strictly fail-soft.

**The FHIR validator**
([validator_client.py](../services/trust-gate/src/validator_client.py), driven from
`checks/conformance/validation.py`):

1. Powers `conformance.structural`, `conformance.profile`, `conformance.ig_profile`.
2. For each resource it POSTs to `/validate` (optionally `?profile=...`) and reads
   the returned `OperationOutcome`. A resource violates when the outcome carries any
   error/fatal structural issue; terminology-tagged and transport issues are filtered
   out.
3. `conformance.structural` validates every resource (it is the critical BLOCK gate,
   so sampling could let a malformed resource slip past), fanned out across a bounded
   thread pool with a batch-size-scaled timeout.
4. Failure handling is the crux of "fail closed": a full outage
   (`ValidatorUnavailable`, a 502/503/504, transport error, or an open circuit
   breaker) degrades the checks to NA/SKIPPED, never a false PASS and never a false
   BLOCK on slowness. A per-request rejection (`ValidatorBadRequest`, for example a
   500 "Unable to resolve profile") skips just that resource without tripping the
   breaker.
5. An SSRF guard blocks link-local and cloud-metadata targets.

**The terminology server**
([terminology_client.py](../services/trust-gate/src/terminology_client.py), driving
`conformance.terminology`):

1. For each coding whose system is a recognised clinical system
   (`CLINICAL_CODE_SYSTEMS`), it calls `CodeSystem/$validate-code` and reads the
   boolean `result`. `result == false` is a violation.
2. Results are cached per `(system, code)` on a process-wide singleton, so common
   codes are not re-fetched every batch.
3. Unreachable or 5xx → `TerminologyUnavailable` → the check degrades to NA.

**Gating and reporting.** The two slow calls only run when their phase is selected
and, for the validator, when `external_validation` is true. Coverage then reports
`external_validation_performed`, and fitness for outcomes research or regulated
sharing requires it, so a verification-only run is honestly labelled and cannot be
approved for those uses.

## 18. The Quality Passport output

A single JSON object (renderable to Markdown). The fields that matter most:

| Field | What it is |
|---|---|
| `decision` | PASS / CONDITIONAL_PASS / BLOCK |
| `overall_score`, `has_assessed` | headline pass-rate, and whether anything was assessed |
| `category_scores` | per-Kahn-category pass-rates |
| `scorecard`, `overall_grade` | per-dimension scores and letter grades, plus one headline grade |
| `fitness` | the graded, purpose-bound statement, with the coverage caveat |
| `approved_for`, `not_approved_for` | which uses are cleared, and why others are not |
| `blockers` | plain-language lines for each failed critical check |
| `coverage` | assessed/total, descoped phases, validation depth, external-validation flag |
| `advisory` | the statistical (non-verdict) findings |
| `checks` | every `CheckResult`, with a capped, identifier-free sample of violating rows |
| `phases`, `targets` | per-phase and per-sector sub-verdicts |
| `evaluation` | reproducibility provenance: sampler seed, which external services were used, the reference timestamp |
| `privacy_processing_allowed` | the boolean the intake barrier gates on |

Any identifier-bearing value a check surfaces is reduced to a stable non-reversible
token (`redact_token`) first, so the passport is safe to persist and serve.

## 19. Configuration reference

The behaviour-gating knobs, all environment-overridable.

| Variable | Default | Effect |
|---|---|---|
| `TRUST_GATE_MODE` | `warn` | Enforcement at the intake barrier: `warn` (assess and attach, never block), `block` (BLOCK stops the pipeline), `off` (no-op). |
| `TRUST_GATE_PASS_MIN_RATE` | `90` | Overall pass-rate floor for a clean PASS. |
| `TRUST_GATE_CATEGORY_MIN_RATE` | `80` | Per-category floor below which a clean PASS is impossible. |
| `TRUST_GATE_VALIDATOR_URL` | unset | The external FHIR validator. Unset → structural/profile/IG checks are NA. |
| `TRUST_GATE_TERMINOLOGY_URL` | unset | The terminology server. Unset → the terminology check is NA. |
| `TRUST_GATE_IG_PROFILE` | unset | Selects an Implementation Guide (for example US Core) for `conformance.ig_profile`. |
| `TRUST_GATE_PROVENANCE_SEVERITY` | `warn` | `block`/`score` turns the Provenance SAM into a scored (and, in block, critical) check. |
| `MEDANON_REGULATED_MODE` | off | A skipped critical or conformance check becomes a BLOCK. |
| `TRUST_GATE_OUTLIER_IQR_K` / `_MODZ` / `_MIN_SAMPLE` | `3.0` / `3.5` / `20` | Statistical outlier method parameters (advisory checks). |
| `TRUST_GATE_SAMPLER_SEED` | `1337` | Seed for the reproducible cross-batch baseline reservoir. |
| Per-check thresholds | see `constants.py` | Overridable per `check_id` in `config/checks.yaml` under `thresholds:`. |

## In one paragraph

A dataset hits the intake barrier, which asks the Trust Gate to assess it. The gate
figures out what to measure (from the declared use and selected phases), runs every
applicable check, and scores each one the same way: how many records broke the rule
versus a set tolerance, giving pass, fail, or not-applicable. It keeps only the
reproducible checks for the verdict, rolls them into category and overall scores and
letter grades that floor to F on any critical failure, records what it could not
check, and produces a traffic-light verdict tied to what the data is fit for. The
barrier then enforces that verdict, the passport and its open findings are stored for
remediation, and a running baseline watches for drift across batches, so quality is
not just measured once but kept under control over time. Every rule comes from a
recognised published standard, the gate never edits your data, it fails safe, it is
honest about what it did not check, and it gives the same answer every time.
