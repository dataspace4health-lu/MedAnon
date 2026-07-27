# Trust Gate: how it works

This is the plain-language guide to the Trust Gate. It explains what the pieces
are, how data flows through them, how each quality check is measured, and how
those measurements roll up into a single graded verdict. Read this first; the
deeper references are [quality-evaluation-methodology.md](../quality-evaluation-methodology.md)
(the methods and citations) and [services/trust-gate/ARCHITECTURE.md](../../services/trust-gate/ARCHITECTURE.md)
(the code layering).

## 1. What the Trust Gate is

The Trust Gate is a pre-privacy data-quality barrier. Before any clinical data is
de-identified, the gate reads it, runs a suite of quality checks, and returns a
graded, purpose-bound verdict called a **Quality Passport**.

It answers one question: *is this dataset good enough, for the stated use, to
process?* The answer is one of three decisions:

- **PASS** the data meets every quality floor for its declared use.
- **CONDITIONAL_PASS** usable, but one or more quality floors were missed; remediate before high-stakes use.
- **BLOCK** a critical defect was found; the data must not be processed until it is fixed.

Four rules hold everywhere in the gate. They are the reason the verdict is
trustworthy:

1. **It never mutates data.** The gate reads and reports; it does not edit resources.
2. **It fails closed, not open.** A check that cannot run (no validator, a timeout, a missing input) is marked **NA** (not applicable) and dropped from scoring. It is never counted as a pass. A missing measurement never inflates the score.
3. **The verdict is deterministic.** Only reproducible checks drive the decision. Statistical checks (outlier, drift) are reported as advisory and can never move PASS/CONDITIONAL/BLOCK. Same bytes in, same verdict out.
4. **Measurement and policy are separate.** One layer *measures* quality (percent of checks passing, no invented weights). A second layer *decides* fitness for use. Changing "what counts as good enough" never touches "how we measure."

## 2. The components

The service is a small, strictly layered FastAPI app. Each layer depends only on
the layer below it. Nothing depends on the orchestrator.

| Layer | Location | Job |
|---|---|---|
| **API** | `src/api/` | HTTP surface: endpoints, request/response schemas, persistence, metrics. |
| **Engine** | `src/engine.py` | The orchestrator. `assess()` for FHIR, `assess_omop()` for OMOP CDM. Runs the pipeline stages in order and assembles the passport. |
| **Verdict** | `src/verdict/` | Pure functions over the check list: `runner` (runs the checks), `scoring` (rolls them up), `decision` (the PASS/CONDITIONAL/BLOCK policy), `coverage` (what actually ran), `fitness` (purpose-bound approvals). |
| **Checks** | `src/checks/` | One measurement concern per module: conformance, completeness, plausibility, timeliness, accuracy, identity, governance, plus the OMOP DQD suite. Each returns `CheckResult` objects. |
| **Domain model** | `src/passport.py`, `dimensions.py`, `phases.py`, `constants.py` | Pure data: the `CheckResult` and `QualityPassport` shapes, the dimension and phase taxonomies, and the thresholds. No I/O, no rendering. |
| **Reporting** | `src/reporting/` | Renders a passport to Markdown. Depends on the model, never the reverse. |
| **Infra** | `validator_client.py`, `terminology_client.py`, `breaker.py` | Calls to the external FHIR validator and terminology server, each behind a circuit breaker and timeout. |
| **Store / connectors** | `src/store/`, `src/connectors/`, `src/cdm/` | Passport and findings persistence; file and SQL ingestion; FHIR-to-OMOP mapping. |

The single most important object is the **`CheckResult`**. Everything above it is
a roll-up of a list of these. Everything below it produces them.

## 3. The workflow (how the gate is invoked)

The gate sits in front of the de-identification pipeline as an *intake barrier*,
the mirror image of the output barrier that guards what leaves the pipeline.

```
FHIR / OMOP dataset
      │
      ▼
[ anonymizer: pipeline/intake_gate.py ]     enforce_intake()
      │  (calls the Trust Gate microservice over HTTP)
      ▼
[ Trust Gate: POST /v1/trust/assess ]       engine.assess()
      │  runs the pipeline (section 4)
      ▼
  Quality Passport  { decision, scores, grade, fitness, coverage, checks }
      │
      ├─ decision = BLOCK  and  TRUST_GATE_MODE = block  ──►  IntakeBlocked (422); data never de-identified
      └─ otherwise ─────────────────────────────────────►  passport attached, de-identification proceeds
```

Enforcement is controlled by one environment variable, `TRUST_GATE_MODE`:

- **`warn`** (default) always assess and attach the passport; never block.
- **`block`** raise and stop the pipeline when the decision is BLOCK.
- **`off`** no-op (also the behaviour when no Trust Gate URL is configured).

The gate is **fail-soft as a service**: if the Trust Gate is unreachable, the
intake barrier degrades to an advisory CONDITIONAL_PASS, never a silent PASS and
never a hard block on the gate's own outage.

The API offers three assessment endpoints:

- `POST /v1/trust/assess` a single resource or Bundle.
- `POST /v1/trust/assess/batch` a list of resources.
- `POST /v1/trust/assess/omop` an OMOP CDM dataset (tabular rows, or FHIR mapped to OMOP).

Each request may carry the declared **intended use**, the **phases** to run, and
optional **sector targets** (so a mixed dataset can get one verdict per cohort).
These are usually bundled into a reusable **trust profile** and forwarded per
request.

## 4. The pipeline (inside a single assessment)

`engine.assess()` runs seven stages in a fixed order. This is the heart of the
gate.

```
1. RUN CHECKS      run every check in the registry against the resources.
                   Each check returns CheckResult(s), tagged with its phase and
                   DQ dimension. Keep only the phases the caller selected.
        │
2. SPLIT           separate deterministic checks from advisory (statistical) ones.
                   Only the deterministic set can drive the verdict.
        │
3. SCORE           roll the deterministic checks up into per-category pass-rates
                   and one overall pass-rate (section 6).
        │
4. DECIDE          apply the fitness-for-use policy to those scores plus any
                   critical failures, to get PASS / CONDITIONAL_PASS / BLOCK.
        │
5. COVERAGE        report how much of the suite actually ran (assessed vs total,
                   which depth capabilities and phases were skipped).
        │
6. FITNESS         compute the purpose-bound approved / not-approved lists and the
                   graded fitness statement, using coverage (so a verdict cannot
                   over-claim relative to what ran).
        │
7. ASSEMBLE        build the QualityPassport: decision, scores, per-dimension
                   scorecard, letter grade, fitness, advisory, coverage, evidence.
```

The OMOP path (`assess_omop`) is the same shape, but its checks come from the
OHDSI Data Quality Dashboard (DQD) suite. It reuses the identical scoring,
decision, and scorecard machinery, so an OMOP passport is structurally identical
to a FHIR one.

## 5. How one check is measured

This is the atomic unit of measurement, and it follows the OHDSI Data Quality
Dashboard method exactly. Every check computes two numbers and compares them.

- **`applicable`** how many rows or items the check could apply to (the denominator).
- **`violations`** how many of those broke the rule (the numerator).
- **`violation_fraction`** = `violations / applicable`.
- **`threshold`** the maximum violation fraction the check tolerates.

The outcome is then mechanical:

```
if applicable == 0:                       result = NA     (nothing to assess)
elif violation_fraction >  threshold:     result = FAIL
else:                                      result = PASS
```

A worked example from the completeness check
([checks/completeness.py](../services/trust-gate/src/checks/completeness.py)):
say a batch has 100 Observations and 3 of them carry neither a `value[x]` nor a
`dataAbsentReason`. Then `applicable = 100`, `violations = 3`,
`violation_fraction = 0.03`. The threshold for `completeness.value_or_absent` is
`0.0` (zero tolerance), so `0.03 > 0.0` and the check **FAILs**.

Thresholds are not magic numbers. They live in
[constants.py](../services/trust-gate/src/constants.py) (`DEFAULT_THRESHOLDS`),
are overridable per check in config or by environment, and default to zero
tolerance for hard structural defects. A few are deliberately non-zero and are
justified in the code, for example:

- `completeness.element_density` = `0.5` optional "richness" fields are not expected to be 100 percent populated.
- `plausibility.value_outlier` = `0.01` flag a value group only when more than 1 percent of it is extreme (a systemic error signal, not a few genuine extremes).

Some checks are marked **critical**. A critical check that FAILs forces a BLOCK on
its own, regardless of the overall score. The always-critical, zero-tolerance
checks are the structural prerequisites: a resource must have a type, an id, a
valid structure, and must not be marked `entered-in-error`.

## 6. The check suite: three views of the same checks

Every check is classified three ways at once. The same `CheckResult` carries all
three tags, so the same measurement can be reported to different audiences.

### View A: Kahn category (the scoring backbone)

The scientific framework is Kahn et al. 2016, the field standard used by OHDSI and
PCORnet. Every check belongs to exactly one of three **categories**:

- **conformance** does the data follow the rules? Structure, format, code
  systems, terminology membership, referential integrity.
- **completeness** is what should be present actually present? Required
  elements, Observation values, element density.
- **plausibility** are the values believable? Uniqueness, definitional bounds,
  cross-field concordance, outliers, temporal order.

Each check also has a **context**: **verification** (internal consistency, no
outside reference needed) or **validation** (checked against an external
benchmark, such as a FHIR profile or a terminology server). This distinction
matters for honesty: a verdict built only from verification checks is not the same
as one externally validated, and the coverage report says which you got.

### View B: audit phase (selectable suites)

A **phase** is a named quality concern the caller can switch on or off
independently, so the gate measures exactly what is wanted. The eleven phases are
in [phases.py](../services/trust-gate/src/phases.py): `structural_conformance`,
`terminology_validity`, `referential_integrity`, `completeness_core`,
`completeness_richness`, `value_plausibility`, `temporal_plausibility`,
`identity_integrity`, `provenance_auditability`, `timeliness`, `source_accuracy`.

Deselecting a phase does two things: it removes those checks from the verdict, and
if the phase needs a slow external call (the FHIR validator or terminology
server), it skips that call entirely. Descoped phases are reported in coverage so
narrowing scope visibly *lowers* coverage rather than silently raising the score.

### View C: DQ dimension (the consumer scorecard)

The **dimension** is the recognized data-quality vocabulary a data consumer knows
(DAMA DMBOK plus ISO/IEC 25012): completeness, conformity, consistency, accuracy,
plausibility, uniqueness, integrity, currency, provenance. This is the view used
for the per-dimension scorecard and letter grades in the passport. The mapping is
in [dimensions.py](../services/trust-gate/src/dimensions.py).

## 7. How QC is quantified (the roll-up)

Now the individual PASS / FAIL / NA results become numbers. Every roll-up uses one
formula and no domain weights:

> **score = 100 x (checks that PASSED) / (checks that were assessed)**

where "assessed" means result is PASS or FAIL, never NA. This is the "percent of
checks passing" metric, computed in
[verdict/scoring.py](../services/trust-gate/src/verdict/scoring.py).

Three things are computed with that one formula:

1. **Category scores.** For each Kahn category (conformance, completeness,
   plausibility), the percent of its assessed checks that passed. A category with
   no assessed checks is reported as `null` (not assessed), not as 100.

2. **Overall score.** The percent of all assessed deterministic checks that
   passed. If nothing was assessed at all, `has_assessed` is false and the
   headline reads "not assessed" rather than a vacuous 100 percent.

3. **Per-dimension scorecard.** The same percent, computed per DQ dimension, each
   with a letter grade.

### Letter grades

Grades are bands aligned to the decision floors, so the grade and the verdict tell
one story (see `grade_for` in [dimensions.py](../services/trust-gate/src/dimensions.py)):

| Grade | Score | Meaning |
|---|---|---|
| A | at least 95 | excellent |
| B | at least 90 (`PASS_MIN_RATE`) | meets the overall pass floor; a clean PASS sits here |
| C | at least 80 (`CATEGORY_MIN_RATE`) | meets the per-category floor but below the pass floor |
| D | at least 60 | marginal |
| F | below 60 | failing |

Grades are **non-compensatory** for critical failures. If any critical check
FAILed, the grade floors to **F** no matter how high the pass-rate is
(`grade_with_floor`). This stops a headline reading "A" while the decision is
BLOCK: unrelated passing checks cannot average away an inherent-characteristic
failure (DAMA DMBOK and ISO 25012 treat those as non-compensable).

## 8. The decision (measurement becomes a verdict)

The policy layer lives in
[verdict/decision.py](../services/trust-gate/src/verdict/decision.py). It reads the
scores and applies these rules in order:

```
if any critical check FAILed:                        BLOCK
elif overall < PASS_MIN_RATE (90)          →  CONDITIONAL_PASS
  or any category < CATEGORY_MIN_RATE (80)
  or any resource type below its threshold
  or required provenance is missing:
else:                                                PASS
```

The two floors, `PASS_MIN_RATE` (90) and `CATEGORY_MIN_RATE` (80), are the only
policy cut-points, and both are environment-overridable
(`TRUST_GATE_PASS_MIN_RATE`, `TRUST_GATE_CATEGORY_MIN_RATE`). They are the same
numbers the letter-grade bands use, so the story stays consistent.

Two refinements sit on top:

- **Per-resource-type calibration.** With `resource_thresholds` supplied, the gate
  computes a genuine per-type pass-rate (for example Observations separately from
  Patients) and downgrades to CONDITIONAL_PASS if any type is below its floor. A
  vital-sign range failure pulls down Observation without touching Patient.
- **Regulated mode** (`MEDANON_REGULATED_MODE`). By default an un-runnable check
  degrades to NA. In regulated mode a *skipped* critical or conformance check
  becomes a BLOCK: you cannot release regulated data on an assessment that could
  not run.

One shared function (`decide`) computes the headline verdict and every
sub-verdict (per phase, per sector target). This guarantees a sector badge can
never read more leniently than the overall verdict.

## 9. Fitness for use (the purpose-bound answer)

Quality is fitness for a *declared* use: a dataset fit for cohort discovery may be
unfit for outcomes research. So the gate does not just say "good" or "bad"; it says
what the data is good *for*. This is in
[verdict/fitness.py](../services/trust-gate/src/verdict/fitness.py).

Each use profile names the quality floors it needs. From loosest to strictest:

| Use | Needs conformance | Needs completeness | Needs plausibility | Needs provenance | Needs external validation |
|---|---|---|---|---|---|
| cohort discovery / feasibility | yes | | | | |
| descriptive analytics | yes | yes | | | |
| AI/ML model training | yes | yes | yes | yes | |
| outcomes / comparative-effectiveness | yes | yes | yes | yes | yes |
| regulated submission / external sharing | yes | yes | yes | yes | yes |

The caller's free-text `intended_use` is keyword-matched to one of these. The
passport then lists `approved_for` (uses whose floors are met) and
`not_approved_for` (with the specific unmet requirement). So a CONDITIONAL_PASS
still tells the provider exactly what the data can and cannot be used for, rather
than a flat rejection.

## 10. Honesty: coverage and advisory

Two mechanisms stop the verdict from over-claiming.

**Coverage** ([verdict/coverage.py](../services/trust-gate/src/verdict/coverage.py))
reports how much of the suite actually ran: assessed vs total checks, which
"depth" capabilities were not exercised (no validator, no terminology server, no
reference dataset, no clinical rule pack), which phases were descoped, and the
verification-vs-validation breakdown. A grade computed over 7 of 22 checks must
not read like one over 22 of 22. The coverage caveat is appended to the fitness
statement, so a verification-only run is labelled as such.

**Advisory** ([scoring.advisory_report](../services/trust-gate/src/verdict/scoring.py))
holds the statistical, batch-relative checks: value outliers and cross-batch
distribution drift. These use a random reservoir and a batch-relative distribution,
so they are non-deterministic. They are reported for analysis but explicitly
excluded from the verdict, the category scores, and the scorecard. Folding them in
would make the decision irreproducible. This is the mechanism behind rule 3 from
section 1.

There is also a standing honesty caveat on plausibility: a plausibility PASS means
no value was *statistically* implausible for its cohort. It does **not** certify
*clinical coherence*. A value inside every stratified distribution can still be
clinically contradictory in context. The passport surfaces this note whenever a
plausibility check ran.

## 11. What comes out: the Quality Passport

A single JSON object (renderable to Markdown). The fields that matter most:

| Field | What it is |
|---|---|
| `decision` | PASS / CONDITIONAL_PASS / BLOCK |
| `overall_score`, `has_assessed` | headline pass-rate, and whether anything was assessed |
| `category_scores` | per-Kahn-category pass-rates |
| `scorecard`, `overall_grade` | per-DQ-dimension scores and letter grades, plus one headline grade |
| `fitness` | the graded, purpose-bound statement, with the coverage caveat |
| `approved_for`, `not_approved_for` | which uses are cleared, and why others are not |
| `blockers` | plain-language lines for each failed critical check |
| `coverage` | assessed/total, descoped phases, validation depth, external-validation flag |
| `advisory` | the statistical (non-verdict) findings |
| `checks` | every `CheckResult`, with a capped, PHI-free sample of violating rows |
| `phases`, `targets` | per-phase and per-sector sub-verdicts |
| `evaluation` | reproducibility provenance: sampler seed, which external services were used, the reference timestamp |
| `privacy_processing_allowed` | the boolean the intake barrier gates on |

Any PHI-bearing value that a check needs to surface (an MRN, an SSN) is reduced to
a stable non-reversible token (`redact_token`) before it enters the passport. The
passport is safe to persist and serve.

## 12. How conformance is evaluated and calculated

Conformance answers: does the data follow the rules of structure, format, codes,
and references? It is assembled in
[checks/conformance/__init__.py](../services/trust-gate/src/checks/conformance/__init__.py)
`evaluate()`, which runs the checks below in order. The first three are the
"SAM prerequisite chain": always-on, offline, zero-tolerance, and critical (a FAIL
BLOCKs).

Each row shows the exact denominator (`applicable`) and numerator (`violations`)
the code counts.

| check_id | file | context | critical | threshold | applicable (denominator) | violation (numerator) |
|---|---|---|---|---|---|---|
| `resource_type_present` | presence.py | verification | yes | 0.0 | every dict resource | `resourceType` missing or blank |
| `resource_id_present` | presence.py | verification | yes | 0.0 | resources that have a `resourceType` | `id` missing or blank |
| `status_not_entered_in_error` | presence.py | verification | yes | 0.0 | resources of the 8 clinical types (`ENTERED_IN_ERROR_RESOURCE_TYPES`) | `status == "entered-in-error"` |
| `value_format` | presence.py | verification | no | 0.0 | each `id` field + each temporal field (`TEMPORAL_FIELD_NAMES`) | value fails the FHIR `id` / dateTime regex (`constants.py`) |
| `coding_structure` | presence.py | verification | no | 0.0 | every `coding` in the resource tree | missing `system`+`code`, or `code` is a redacted sentinel |
| `code_wellformed` | terminology.py | verification | no | 0.0 | codings whose system is checkable offline (`validate_code_format` returns non-None) | code fails format or check-digit (LOINC, SNOMED Verhoeff, ICD-10, RxNorm) |
| `reference_integrity` | references.py | verification | no | 0.0 | each literal reference (relative always; absolute/`urn:uuid:` only if `full_urls` given) | target not present in the batch id-set / fullUrl-set |
| `structural` | validation.py | validation (validator) | yes | 0.0 | every resource the external validator validated | validator returns any error/fatal structural issue |
| `profile` | validation.py | validation | no | 0.0 | resources that assert a `meta.profile` | validator returns issues against that profile |
| `ig_profile` | validation.py | validation | no | 0.0 | covered resources sampled per type (`_VALIDATOR_SAMPLE_PER_TYPE`) | validator returns issues against the selected IG profile |
| `terminology` | terminology.py | validation | no | 0.0 | codings whose system is in `CLINICAL_CODE_SYSTEMS` | terminology server says the (system, code) is invalid |

Two important calculation details from the code:

- **The uniqueness/exactness of references.** `_normalize_ref` reduces
  `Patient/1/_history/3?_format=json` to the exact key `Patient/1` and requires an
  exact match, so `Patient/1` cannot false-resolve against `RelatedPerson/x1`.
- **Structural excludes terminology.** In
  [validator_client.py](../services/trust-gate/src/validator_client.py)
  `_error_issues()` drops any issue tagged `TerminologyEngine` and any transport
  error from the structural/profile axis. Code validity is measured only by
  `conformance.terminology`, so a terminology-server outage can never masquerade as
  a structural defect, and it is never double-counted.

The conformance category score is then `100 x passed / assessed` over just these
checks (whichever were not NA).

## 13. How completeness is evaluated and calculated

Completeness ([checks/completeness.py](../services/trust-gate/src/checks/completeness.py))
answers: is what should be present actually present? Three checks, all verification:

| check_id | threshold | applicable | violations | note |
|---|---|---|---|---|
| `required_elements` | 0.0 | +1 per resource that has a `REQUIRED_ELEMENTS` entry for its type | +1 if any required field is empty | binary present/absent per resource |
| `value_or_absent` | 0.0 | +1 per Observation that has a non-empty `status` | +1 if it has no `value[x]`, no `component[].value[x]`, and no `dataAbsentReason` | panel Observations carry values in `component`, so those are not counted missing |
| `element_density` | **0.5** | += `len(recommended)` per resource (the fields count as the denominator, not the resource) | += number of those recommended fields left empty | this is a *frequency* / richness metric |

The key calculation nuance is `element_density`. Its denominator is
`recommended_fields x applicable_resources`, and its violation_fraction is the
dataset's "sparseness" of optional richness. Its threshold is `0.5`, so it only
FAILs when more than half of all recommended-element slots are empty. That is
deliberate: optional fields are not expected to be 100 percent populated (Kahn
completeness / OHDSI `measureValueCompleteness`), so a zero tolerance would be
wrong here.

`_present()` also handles FHIR choice types: `effectiveDateTime` counts as
populated when `effectivePeriod` is present, `medicationCodeableConcept` counts
when `medicationReference` is present, etc.

## 14. How plausibility is evaluated and calculated

Plausibility ([checks/plausibility.py](../services/trust-gate/src/checks/plausibility.py))
answers: are the values believable? This category is a mix of deterministic checks
(which drive the verdict) and statistical checks (advisory only, see section 10).

| check_id | threshold | drives verdict? | applicable | violations |
|---|---|---|---|---|
| `uniqueness` | 0.0 | yes | count of `Type/id` keys | count beyond the first occurrence of any duplicated key (`sum(c-1)`) |
| `definitional_bounds` | 0.0 | yes | every numeric `(code,unit)` value whose unit has a bound in `DEFINITIONAL_UNIT_BOUNDS` | value outside `[lo, hi]` (e.g. a percent >100, a negative concentration) |
| `concordance` | 0.0 | yes | each resource of a rule's type whose subject's `Patient.gender` is known | a forbidden code appears for that gender (e.g. a pregnancy code on a male patient) |
| rule pack (`date_order`, `not_in_future`, `value_range`, ...) | per rule | yes | config-driven, per rule | the declarative clinical rule is violated |
| `value_outlier` | 0.01 | **no (advisory)** | numeric values in a `(code,unit)` group with at least `OUTLIER_MIN_SAMPLE` (20) reference points | value beyond the robust fence |
| `distribution_drift` | 0.0 | **no (advisory)** | each `(code,unit)` whose accumulated baseline has at least 20 samples | batch median drifted beyond the modified-z cut-off vs the baseline |

The statistical machinery (advisory only, so it stays reproducible):

- **Outliers** use a robust **modified z-score** `0.6745 x (x - median) / MAD`
  with cut-off 3.5 (Iglewicz and Hoaglin). For strictly-positive analytes the
  fence is computed in **log space** (right-skewed labs like CRP or ferritin are
  approximately log-normal, so a symmetric raw-scale fence would over-flag the
  right tail). When MAD is 0 it falls back to a **Tukey IQR fence**
  `[Q1 - 3xIQR, Q3 + 3xIQR]`. There are no hardcoded clinical ranges; the fence is
  learned from the data's own distribution per `(code, unit)`.
- **Drift** compares this batch's median for a `(code, unit)` against the
  accumulated cross-batch baseline reservoir (size `BASELINE_RESERVOIR_SIZE`,
  seeded sampler for reproducibility). It catches a unit change, device
  recalibration, or a switched feed that a within-batch outlier check cannot see.

Because these two are non-deterministic (random reservoir + batch-relative), the
engine marks them advisory via the determinism allow-list in
[constants.py](../services/trust-gate/src/constants.py) and reports them in the
`advisory` block, never in the category score or the decision.

The standing honesty caveat applies: a plausibility PASS means "no statistical or
definitional violation for this cohort", not "clinically coherent".

## 15. How provenance is evaluated and calculated

Provenance is handled by
[checks/governance.py](../services/trust-gate/src/checks/governance.py) and is
deliberately kept out of the Kahn category scores, because provenance presence is a
sharing-readiness signal, not a data-quality measurement. It works on two levels.

**Level 1: the auditability evidence (always on).** `governance.evaluate()` builds
a dict, not a scored check:

- `provenance_present` is `True` only when the dataset-level `provenance` payload
  contains both `source_system` and `extraction_time` (`REQUIRED_PROVENANCE_KEYS`).
- `resource_meta_coverage` is the fraction of resources carrying `meta.lastUpdated`
  or `meta.source`.

This feeds the decision policy directly. In the engine, `provenance_capped` is
`True` when the provenance phase is selected and `provenance_present` is `False`;
that alone caps the verdict at **CONDITIONAL_PASS** (see
[verdict/decision.py](../services/trust-gate/src/verdict/decision.py) `decide`),
and it is also what the fitness profiles for AI training, outcomes research, and
regulated sharing require.

**Level 2: the Provenance SAM (opt-in).** `evaluate_provenance_sam()` becomes a
real scored `conformance.provenance_present` check only when
`TRUST_GATE_PROVENANCE_SEVERITY` is `block` (or `score`); in the default `warn`
mode it returns `None` and stays out of scoring. When active:

- `applicable` = the number of clinical resources that are *expected* to have
  provenance (`Observation, MedicationRequest, DiagnosticReport, Condition,
  Procedure`).
- `violations` = expected ids minus the ids actually referenced by a `Provenance`
  resource's `target[].reference` (`len(expected_ids - covered)`).

In `block` mode this check is critical, so an unprovenanced regulated batch BLOCKs;
in `warn` mode provenance only ever caps to CONDITIONAL via level 1.

## 16. How external validation is evaluated and calculated

"External validation" is the Kahn **validation context**: checks measured against
an outside authority rather than internal consistency. Exactly two authorities are
called, and both are strictly fail-soft.

**The FHIR validator** ([validator_client.py](../services/trust-gate/src/validator_client.py),
driven from `checks/conformance/validation.py`):

1. It powers `conformance.structural`, `conformance.profile`, and
   `conformance.ig_profile`.
2. For each resource it POSTs to `/validate` (optionally `?profile=...`) and reads
   the returned `OperationOutcome`. A resource **violates** when the outcome
   carries any `error`/`fatal` structural issue; terminology-tagged and transport
   issues are filtered out (`_error_issues`).
3. `conformance.structural` validates **every** resource (it is the critical BLOCK
   gate, so sampling could let a malformed resource slip past). The calls fan out
   across a bounded thread pool with a **batch-size-scaled timeout**
   (`_validator_timeout_for`).
4. Failure handling is the crux of "fail closed": a full outage
   (`ValidatorUnavailable`, a 502/503/504, transport error, or an open circuit
   breaker) degrades the checks to **NA/SKIPPED**, never a false PASS and never a
   false BLOCK on slowness. A per-request rejection (`ValidatorBadRequest`, for
   example a 500 "Unable to resolve profile" for an IG that is not loaded) skips
   **just that resource/axis** without tripping the breaker, so sibling checks stay
   intact.
5. An SSRF guard (`_guard_url`) blocks link-local and cloud-metadata targets.

**The terminology server**
([terminology_client.py](../services/trust-gate/src/terminology_client.py), driving
`conformance.terminology`):

1. For each coding whose system is a recognized clinical system
   (`CLINICAL_CODE_SYSTEMS`), it calls `CodeSystem/$validate-code` and reads the
   boolean `result`. `result == false` is a violation.
2. Results are cached per `(system, code)` on a process-wide singleton, so repeated
   common codes (LOINC vitals, SNOMED problem list) are not re-fetched every batch.
3. Unreachable or 5xx → `TerminologyUnavailable` → the check degrades to NA.

**Gating and reporting.** The two slow calls only run when their phase is selected
and, for the validator, when `external_validation` is true:
`run_validator = external_validation and STRUCTURAL in selection`,
`run_terminology = TERMINOLOGY in selection` (see
[verdict/registry.py](../services/trust-gate/src/verdict/registry.py)). The
`coverage` block then reports whether any external validation actually happened:
`external_validation_performed` is true only when at least one validation-context
check was assessed (not NA). That flag is what
[verdict/fitness.py](../services/trust-gate/src/verdict/fitness.py) requires before
it will approve a dataset for outcomes research or regulated sharing. So a
verification-only run (no validator, no terminology server) is honestly labelled
and cannot be approved for the uses that demand external validation.

## 17. The whole thing in one paragraph

The Trust Gate reads a clinical dataset, runs a suite of quality checks where each
check measures a violation fraction against a tolerance and returns PASS, FAIL, or
NA. It rolls the deterministic checks up into per-category and overall pass-rates
(percent of assessed checks passing, no invented weights), converts those into
letter grades that floor to F on any critical failure, and applies a fitness-for-use
policy to produce PASS, CONDITIONAL_PASS, or BLOCK. It reports what it could not
measure rather than pretending it did, keeps statistical signals advisory so the
verdict is reproducible, and states what the data is fit for given the declared
use. The result is one Quality Passport that the de-identification pipeline gates
on before any privacy processing begins.
