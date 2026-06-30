# The MedAnon Quality Evaluation Methodology

*How the Trust Gate measures FHIR data quality before privacy processing — the
framework, the algorithms, the pipeline, and the decision logic.*

---

## Abstract

Before any de-identification or pseudonymization runs, MedAnon can assess whether
the incoming FHIR data is *fit for secondary use*. This is the job of the **Trust
Gate**: a standalone microservice that ingests FHIR resources, runs a battery of
data-quality checks, and emits a **Quality Passport** — a machine- and
human-readable verdict (`PASS` / `CONDITIONAL_PASS` / `BLOCK`) backed by
per-category scores, per-check evidence, and fitness-for-use recommendations.

The evaluation is **tunable, not generic**: the caller selects which audit
**phases** run, scopes them to **sectors** (resource type / code system) for
independent per-sector verdicts, and binds the result to a declared **intended
use**. On top of the Kahn category measurement the passport renders a
**per-dimension scorecard** with letter grades (DAMA DMBOK / ISO/IEC 25012) and a
purpose-bound fitness statement — *"Fit for research cohort discovery: grade B"* —
so a consumer can see, dimension by dimension, whether the data is good and
valuable enough for *their* use.

This article describes, end to end, *how* that evaluation works: the published
methodology it is grounded in, the formal measurement model, every check
algorithm, the score roll-up, the decision policy, and the resilience semantics
that keep the gate from ever producing a false "PASS." It is written to be read
on its own; the operational quick-start lives in
[trust-gate.md](trust-gate.md).

---

## 1. Motivation: quality *before* privacy, not after

MedAnon already gates what *leaves* the pipeline. The scoring engine
(`pipeline/scoring/`) and the output barrier (`pipeline/validation.py`) ensure
de-identified output meets privacy and utility thresholds. But nothing assessed
the data *entering* the pipeline.

That gap matters for two reasons:

1. **Garbage-in.** De-identifying structurally broken, mis-coded, or
   clinically implausible data produces *trustworthy-looking but worthless*
   output. The privacy transformation is irreversible; you cannot recover the
   provenance of a problem after the fact.
2. **Fitness for use is a property of the source.** Whether a dataset is
   adequate for "AI training" vs merely "cohort discovery" is determined by the
   *input* quality, not the privacy operation.

The Trust Gate is therefore the **symmetric counterpart** to the output barrier —
an *intake* barrier that runs once per dataset, before privacy, and attaches its
verdict to the processing-run record.

```
                 ┌──────────────────────────────────────────┐
  FHIR input ──▶│  INTAKE BARRIER  (Trust Gate)            │──▶ privacy engine ──▶ OUTPUT BARRIER ──▶ output
                 │  quality / fitness-for-use BEFORE privacy│     (rules, NLP,        (PII + score
                 └──────────────────────────────────────────┘      pseudonymize)        gate)
```

---

## 2. Methodological foundation

A scoring system is only as defensible as the framework underneath it. The Trust
Gate does **not** invent domains or weights. It implements two published,
widely-adopted methodologies.

### 2.1 The Kahn et al. (2016) harmonized framework — *the taxonomy*

Kahn MG, Callahan TJ, Barnard J, et al. *"A Harmonized Data Quality Assessment
Terminology and Framework for the Secondary Use of Electronic Health Record
Data."* eGEMs 2016;4(1):18. doi:10.13063/2327-9214.1244.

This is the field standard used by **OHDSI** and **PCORnet**. It organizes every
data-quality check along two axes:

| Axis | Values | Meaning |
|---|---|---|
| **Category** | `conformance` | Do values adhere to standards/formats? |
| | `completeness` | Are values present / recorded? |
| | `plausibility` | Are values believable? |
| **Context** | `verification` | Checked against *internal* constraints (no external reference). |
| | `validation` | Checked against an *external* benchmark (a profile/IG, a terminology server). |

Every Trust Gate check declares its `(category, subcategory, context)`. For
example, structural FHIR validity is `conformance / value / verification`;
checking a code against a terminology server is `conformance / value /
validation`.

### 2.2 The OHDSI Data Quality Dashboard — *the scoring*

The DQD scoring model (Blacketer et al., the OHDSI DQD tool) defines how each
check is *scored*:

> A check has an **applicable** denominator (rows it could evaluate) and a
> **violations** numerator (rows that broke the rule). The **violation fraction**
> = violations / applicable. The check **FAILs** when that fraction exceeds a
> per-check **threshold**, and **PASSes** otherwise.

Crucially, the headline metric is the **percentage of checks passing** — *not* a
weighted average of arbitrary domain weights. This is why the Trust Gate has no
"15% conformance, 20% plausibility" magic numbers: those were removed precisely
because they were indefensible. Scores are pass-rates over checks.

### 2.3 The deliberate split: measurement vs. policy

The Trust Gate keeps two layers strictly separate:

- **Measurement (Kahn + DQD)** — objective, reproducible, framework-grounded.
  This produces the per-check results and the category pass-rates.
- **Policy (product)** — the `PASS / CONDITIONAL_PASS / BLOCK` decision and the
  fitness-for-use recommendations. This is an explicit, tunable fitness-for-use
  layer *on top of* the measurement.

This separation means the scoring stays defensible (it is pure Kahn/DQD) while
the policy stays adjustable per deployment (cut-offs are env-driven). Provenance/
auditability is part of the **policy** layer, not a Kahn DQ category — it is a
sharing-readiness signal, reported separately.

---

## 3. Pipeline flow

### 3.1 Service topology

```
┌──────────────────┐    HTTP /v1/trust/assess     ┌──────────────────────────────┐
│  Anonymizer      │ ───────────────────────────▶ │  trust-gate  (FastAPI :8400)  │
│  intake_gate.py  │                               │   engine.assess(...)          │
│  (circuit-broken)│ ◀─────────────────────────── │      ├─ conformance checks ────┼──▶ FHIR validator (sidecar)
└──────────────────┘     Quality Passport (JSON)   │      │                        ┼──▶ terminology server (optional)
        │                                          │      ├─ completeness checks   │
        ▼                                          │      ├─ plausibility checks    │
  processing_runs.trust_passport (JSONB)           │      └─ governance evidence    │
                                                   └──────────────────────────────┘
                                                            ▲
  standalone UI (:8401) ── POST /v1/trust/assess ──────────┘   (paste FHIR → passport)
```

### 3.2 Request lifecycle (`engine.assess`)

The orchestrator is `services/trust-gate/src/engine.py::assess`. Given a flat
list of FHIR resource dicts it executes the following steps in order:

```
1.  Run the conformance checks   →  list[CheckResult]   (uses validator + terminology)
2.  Run the completeness checks  →  list[CheckResult]
3.  Run the plausibility checks  →  list[CheckResult]   (uniqueness + the rule pack)
4.  Evaluate governance evidence →  auditability dict   (NOT a Kahn category)
5.  Roll up category pass-rates  →  category_scores
6.  Roll up overall pass-rate    →  overall_score
7.  Collect blockers             →  critical checks that FAILed
8.  Apply the decision policy    →  PASS | CONDITIONAL_PASS | BLOCK
9.  Derive fitness-for-use lists →  approved_for / not_approved_for
10. Assemble the QualityPassport →  to_dict() / to_markdown()
```

A single Bundle, a list, or a single resource is first **flattened** into a flat
resource list (`main._flatten`: Bundle entries are unwrapped; lists recurse;
non-resources are dropped). The whole assessment is **dataset-level**: it runs
*once* per request/job, not per chunk — so cross-resource checks (reference
integrity, date-after-birth, uniqueness) see the whole batch.

---

## 4. The measurement model

Every check — built-in or rule-driven — produces one `CheckResult`
(`passport.py`):

```python
@dataclass(slots=True)
class CheckResult:
    check_id: str
    category: str       # conformance | completeness | plausibility
    subcategory: str    # value | relational | uniqueness | temporal | atemporal | completeness
    context: str        # verification | validation
    applicable: int = 0 # rows/items the check applied to (the denominator)
    violations: int = 0 # rows/items that violated the check (the numerator)
    threshold: float = 0.0  # max acceptable violation fraction (DQD failClass)
    critical: bool = False  # a FAIL here forces a BLOCK

    @property
    def violation_fraction(self) -> float:
        return 0.0 if self.applicable <= 0 else self.violations / self.applicable

    @property
    def result(self) -> str:
        if self.applicable <= 0:
            return "NA"                                   # nothing to assess
        return "FAIL" if self.violation_fraction > self.threshold else "PASS"
```

Three outcomes, with deliberate semantics:

- **PASS** — `violation_fraction ≤ threshold`.
- **FAIL** — `violation_fraction > threshold`.
- **NA** — `applicable == 0`. The check *could not be assessed* (Kahn's
  "could-not-assess"). **NA checks are excluded from every score.** This is the
  single most important safety property: a check that did not run can never be
  counted as a pass, so an unreachable validator degrades scores to *unknown*,
  never to a false *clean*.

Default thresholds are **0.0 (zero tolerance)** for the built-in checks — any
violating row fails the check (`constants.DEFAULT_THRESHOLDS`). They are
overridable per `check_id` in `config/checks.yaml` so an operator can, say, allow
a 2% terminology miss-rate for a known-noisy source without touching code.

---

## 5. The check catalogue

This is the complete set of evaluations, by category, with the algorithm each
one runs.

### 5.1 Conformance — *do values adhere to standards?*

Implemented in `checks/conformance.py`.

#### `conformance.structural` — value / verification — **critical**
**Every** resource with a `resourceType` is POSTed to the FHIR validator (not a
per-type sample: this is the BLOCK gate, so a malformed resource must not be able
to slip past a sample window). The per-resource calls fan out across a bounded
thread pool (`TRUST_GATE_VALIDATOR_CONCURRENCY`) and the off-path timeout scales
with batch size (`TRUST_GATE_VALIDATOR_PER_RESOURCE_SEC`), so a full sweep stays
off the timeout wall; `TRUST_GATE_VALIDATOR_MAX_RESOURCES` (0 = unbounded) is a
safety valve. The validator returns an `OperationOutcome`; the check counts a
**violation** for any resource whose outcome contains an `error` or `fatal` issue.
- denominator = resources validated (disclosed as `coverage.structural_validated_count`).
- If no validator is configured, or it times out / is unreachable → **NA** (not a
  false pass). The always-on, full-batch in-process structural checks
  (`resource_type_present`, `resource_id_present`, `value_format`,
  `coding_structure`) remain the BLOCK authority when the external pass is NA.
- Marked **critical**: a FAIL forces a `BLOCK`. Structurally invalid FHIR is not
  trustworthy enough to transform.

#### `conformance.profile` — value / validation — *non-critical*
For resources that assert a `meta.profile`, the resource is re-validated *against
that profile* (`?profile=...`). A violation is an error/fatal issue under the
asserted profile. Denominator = resources carrying a `meta.profile`. **Not
critical** (a FAIL downgrades to `CONDITIONAL_PASS`, not `BLOCK`): a profile
mismatch is most often an IG version skew, and the data is still structurally
valid FHIR that is safe to pseudonymize. This matches the opt-in `conformance.ig_profile`
check; true structural invalidity is the `conformance.structural` BLOCK above.

> **Mid-batch validator outage.** If the validator becomes unreachable partway
> through, both structural and profile checks **reset `applicable` and
> `violations` to 0** → NA. A partial score would be misleading, so the gate
> declines to report one.

#### `conformance.coding_structure` — value / verification
Walks every `coding` element in every resource (`_iter_codings`, a stack-based
deep traversal). A coding is a **violation** if it lacks a non-empty `system`, or
lacks a non-empty `code`, or its `code` is a redaction sentinel (`""`,
`[REDACTED]`, `unknown`, …). This catches the common failure where
de-identification strips a code's `system`/`code` and leaves a structurally dead
coding. No external dependency — pure verification.

#### `conformance.value_format` — value / verification
Validates FHIR **primitive formats** with the official FHIR R4 regexes, *without
needing the external validator* — so conformance is never fully "not assessed"
(this directly closes the gap noted in §13 / §10.3). For each resource it checks:
the `id` against the FHIR `id` pattern (`[A-Za-z0-9-.]{1,64}`), and every temporal
primitive (`birthDate`, `deceasedDateTime`, `effectiveDateTime`, `issued`,
`authoredOn`, `recordedDate`, `onset/abatement/occurrence/performedDateTime`,
`meta.lastUpdated`, plus `Period.start/.end`) against the FHIR `dateTime` pattern
(which also accepts date-only values). A non-string or malformed value is a
violation — e.g. `birthDate: 12345` or `1980-13-99` is caught here even with no
validator configured. Denominator = primitives present.

#### `conformance.code_wellformed` — value / verification (offline)
Validates a code's **format and check digit** for the major systems *without any
external server* (`checks/code_systems.py`): LOINC format (`\d+-\d`), **SNOMED CT
Verhoeff** check digit (6–18 digit SCTID), ICD-10/-CM pattern, and numeric RxNorm.
A coding in a checkable system fails if it is malformed/mistyped; codings in
systems we cannot check offline (e.g. UCUM) are **not counted** (NA for that
coding). This is *verification* (well-formedness), the always-available companion
to the server-backed `conformance.terminology` *validation* below — it gives a
real terminology signal even when no terminology server is wired.

#### `conformance.terminology` — value / validation
For codings whose `system` is a recognized clinical system (LOINC, SNOMED,
ICD-10/-10-CM, RxNorm, UCUM — `CLINICAL_CODE_SYSTEMS`), the `(system, code)` pair
is validated against a terminology server via `CodeSystem/$validate-code` — this
confirms **ValueSet membership**, which the offline format check cannot. A
violation is a code the server reports as invalid.
- denominator = codings in a recognized clinical system.
- If no terminology server (`TRUST_GATE_TERMINOLOGY_URL`) is configured → **NA**
  (fail-soft); the offline `code_wellformed` check still runs.
- Results are cached in-process per `(system, code)` — codes repeat heavily
  across a batch.

#### `conformance.source_of_truth` — value / validation (accuracy vs a gold reference)
When the caller supplies a **source-of-truth reference**
(`reference = {"records": {"Type/id": {field: expected}}}`,
`checks/accuracy.py`), each present field is compared to its expected value; a
mismatch is an accuracy violation. **NA** when no reference is supplied (accuracy
against an external benchmark cannot be assessed without one). PHI-safe: the audit
records only the field *name*, never the actual or expected value.

#### `conformance.reference_integrity` — relational / verification
Builds a set of `ResourceType/id` present in the batch, then walks every literal
`reference` string (`_iter_references`). Relative references (`Type/id`) resolve
against that set. **Intra-Bundle `urn:uuid:` / absolute references resolve against
the set of entry `fullUrl` values** preserved when a Bundle is flattened — so
transaction/collection Bundles are now integrity-checked too; when no `fullUrl`
context is present (a bare resource list) those references stay **NA** rather than
falsely flagged. Contained references (`#x`) are skipped. Verification only.

**Interpreting a 100% violation rate on a batch slice.** When the batch is a
partial export (e.g. all Observations for a ward without their Patient or Encounter
resources), every reference will point outside the batch and the check will report
100% violations. This is expected behavior — the check is correct, the data is
not broken. It reflects the batch boundary, not an integrity failure in the source.
The mitigation is to run a full-server scan or `Patient/$everything` so each
patient's complete compartment (including the referenced resources) is assessed
together. The UI flags this with an explanatory note when the violation rate is
100%.

### 5.2 Completeness — *are values present?*

Implemented in `checks/completeness.py`.

#### `completeness.required_elements` — verification
A per-resource-type table of minimal required elements (`REQUIRED_ELEMENTS`): e.g.
`Patient → [gender]`, `Observation → [status, code, subject]`, `Condition → [code,
subject]`. A resource is a **violation** if any required element is missing,
`None`, or empty (`""`, `[]`, `{}`). Denominator = resources of a type that has a
required-element entry.

`Observation.subject` is included despite not being a base FHIR SHALL element:
an observation without a subject reference is unattributable to a patient for any
secondary-use purpose and represents a data-completeness gap for the secondary-use
context (Kahn "completeness" — required for the *secondary-use* purpose, not
merely for FHIR conformance).

#### `completeness.value_or_absent` — verification
For `Observation` resources, a violation is an observation that has neither a
`value[x]` element (any key starting with `value`) **nor** a `dataAbsentReason`.
This is the FHIR invariant that an observation must either carry a value or
explain its absence. Denominator = Observations.

#### `completeness.element_density` — verification (a *frequency* metric)
Kahn's completeness is fundamentally about the **frequency** of populated values,
which the binary `required_elements` check does not capture. `element_density`
measures, across a curated set of **recommended-but-optional** elements per
resource type (`RECOMMENDED_ELEMENTS` — e.g. `Patient → gender, birthDate, name,
identifier, address`), the fraction left empty. The denominator is
(recommended-fields × applicable resources) and violations are the empty ones, so
the `violation_fraction` is the dataset's **sparseness**. Choice-type elements are
satisfied by any variant (e.g. `effectiveDateTime` is counted as populated when
`effectivePeriod` is present). This is the OHDSI `measureValueCompleteness`
analogue. **Its threshold is 0.5, not 0.0** — optional richness is not expected to
be 100% present; the check fails only when more than half the recommended
elements are empty across the dataset (tunable per source in `checks.yaml`).

### 5.3 Plausibility — *are values believable?*

Implemented in `checks/plausibility.py` + the rule engine in `rules.py`.

#### `plausibility.uniqueness` — uniqueness / verification
Counts every `ResourceType/id` key in the batch. **Violations = every occurrence
beyond the first** for any duplicated key (`Σ (count − 1)` over keys with
count > 1). Denominator = total keyed resources. Zero tolerance by default.

#### `plausibility.definitional_bounds` — atemporal / verification
Flags values outside their **unit's mathematical definition** — shipped default:
a `%` value must be in `[0, 100]`. These bounds follow from the unit, not from
clinical knowledge, so they are universally true (unlike invented "normal"
ranges) and they are the only check that catches **whole-batch** corruption (a
uniformly mis-united feed). Zero-tolerance. Operators add units in
`definitional_unit_bounds` (config).

#### `plausibility.value_outlier` — atemporal / verification (data-driven)
Flags numeric `Observation.valueQuantity` values that are extreme **relative to
the distribution** for the same `(code, unit)` — *without any hardcoded clinical
range*. The reference distribution needs at least `OUTLIER_MIN_SAMPLE` (default
20) values; below that the group is **not assessed** (NA, never a false flag).
The robust estimator is the **modified z-score** `0.6745·(x − median)/MAD` with
cut-off `OUTLIER_MODZ_CUTOFF` (default 3.5; Iglewicz & Hoaglin), which tolerates
skew/heavy tails far better than mean±σ; when MAD is 0 (mostly-identical values)
it falls back to the **Tukey `K×IQR` fence** (`K = OUTLIER_IQR_K`, default 3.0).
Default tolerance is 1%, so it fails only on a *systemic* fraction of extremes
(drift/corruption), not a few genuine extreme-but-real values. With a **persisted
baseline** (§9.1) the reference is the accumulated reservoir for that `(code,
unit)` merged with the batch, so even single-resource requests are assessed and
feed drift surfaces; otherwise the reference is batch-only. See §6.5.

#### `plausibility.concordance` — atemporal / verification
Flags cross-field contradictions (HL7 EHR-S *concordance* / OHDSI gender-concept
consistency). Config-driven rules forbid a code set for a given `Patient.gender`
(resolved via the resource's subject reference) — e.g. pregnancy codes on a male
patient. Denominator = resources of the rule's type whose subject gender is
known; NA when no concordance rules are configured. Ships one *illustrative* rule;
production should bind the code set to a terminology value set (`memberOf`).

**Subject resolution.** The subject reference is normalized to a canonical
`"ResourceType/id"` form: `Patient/123abc`. The gender lookup uses an exact dict
key match against this canonical form, not a `str.endswith()` suffix match. The
suffix-match approach is a known source of false positives — a reference
`"RelatedPerson/Patient/123"` would spuriously match `"Patient/123"` against a
gender index keyed by the patient. The canonical form lookup is unambiguous.

#### `plausibility.distribution_drift` — atemporal / verification (cross-batch consistency)
Cross-time / cross-source consistency: each `(code, unit)` group's **batch median**
is compared to the **accumulated baseline** (§9.1) via the robust modified-z
`0.6745·(median_batch − median_baseline)/MAD_baseline` against `DRIFT_MODZ_CUTOFF`
(default 3.5). A shift beyond the cut-off flags a distribution drift — a unit
change, device recalibration, or switched source feed that the batch-only outlier
check cannot see (it judges values against the *same* shifted batch). Runs **before**
the outlier check updates the baseline, so it compares against prior batches only.
**NA** without a baseline store, and per-group NA until the baseline holds
`OUTLIER_MIN_SAMPLE` values.

#### `plausibility.record_lag` — temporal / verification (timeliness)
Event→record latency: when a resource carries both an event date
(`effectiveDateTime`/`occurrenceDateTime`/`performedDateTime`/`onsetDateTime`/
`issued`) and a record date (`recordedDate` or `meta.lastUpdated`), a violation is
a lag greater than `TRUST_GATE_MAX_RECORD_LAG_DAYS`. **NA** until that threshold is
set (no invented notion of "timely") or when the dates are absent.

#### `plausibility.currency` — temporal / verification (freshness)
Freshness vs the dataset's `provenance.extraction_time`: a resource whose
`meta.lastUpdated` is older than `TRUST_GATE_CURRENCY_WINDOW_DAYS` before extraction
is stale. **NA** until the window and an extraction time are supplied. Both
timeliness checks carry the **currency** scorecard dimension (§8.4).

#### The declarative clinical-plausibility rule pack
The remaining plausibility checks are **data**, not code — declarative YAML rules
in `config/checks.yaml` under `plausibility_rules`. Each rule becomes one
`CheckResult`. See §6.

### 5.4 Auditability / governance — *policy layer, not Kahn*

`checks/governance.py` returns an **evidence dict**, not `CheckResult`s, because
provenance is a sharing-readiness signal, not a DQ measurement:

```python
{
  "provenance_present":      bool,   # all of (source_system, extraction_time) supplied?
  "missing_provenance_keys": [...],
  "resource_meta_coverage":  "12/15",  # resources carrying meta.lastUpdated|source
  "resource_meta_complete":  bool,
}
```

`provenance_present` feeds the decision policy (missing provenance caps a dataset
at `CONDITIONAL_PASS`). It never affects the Kahn category scores.

### 5.5 Descriptive profiling — *analysis support, not a scored check*

The Kahn/DQD checks answer *"is the data good enough?"* Analysts also need
*"what is in the data?"* before they model it. The Trust Gate therefore computes
a **descriptive profile** (`profiling.py`) and attaches it to the passport under
`profile`. It carries **no PASS/FAIL and never affects the decision** — it is
pure profiling:

| Field | Meaning |
|---|---|
| `total_resources` / `distinct_resource_types` | dataset size + breadth |
| `resource_counts` | per-`resourceType` counts (top 15) |
| `code_system_distribution` | how many codings per terminology system (LOINC/SNOMED/…) |
| `observation_value_stats` | per Observation code: `count, min, max, mean, stddev` of `valueQuantity` (sample stddev) |
| `patient_gender_distribution` | gender mix for cohort shaping |
| `reference_density` | mean literal references per resource (linkage richness) |

This is the data-analysis layer: it lets a consumer see the resource mix,
terminology coverage, value distributions, and linkage density at a glance —
directly informing whether a dataset suits a given downstream analysis,
*independently* of the pass/fail verdict.

### 5.6 PHI-safety of violation details

Each failing check records a capped, representative sample of per-record findings
(`violation_details`, max 50/check) so a provider can locate and fix the offending
records. Because the Trust Gate is a read-only evaluator of the **provider's own**
data, evaluative checks may surface an **example value** alongside the record id
and field path (e.g. an out-of-range lab number, a malformed date token) — the
value is what is wrong, so showing it is what makes the finding actionable.

The exception is **identifier-bearing values**. A FHIR resource `id` can carry a
real identifier (the MRN-as-id anti-pattern), so the `conformance.value_format`
detail tokenizes the `id` example via `redact_token` (stable, non-reversible
`sha256:<12 hex>` per `passport.redact_token`) rather than emitting it verbatim;
the same tokenization already covers patient identifiers in
`plausibility.patient_identity_stable`. Numeric/date/unit examples are not
identifiers and are surfaced as-is. The persisted passport therefore contains
example values from the provider's own dataset (acceptable in the provider
self-QC context, keyed to provider/dataset) but never a raw identifier, and the
append-only audit log stores only decision/timestamp/id metadata, no values.

---

## 6. The clinical-plausibility rule engine

The rule engine (`rules.py`) lets analysts add plausibility checks without
touching Python — mirroring the anonymizer's own rule-schema philosophy
(typed Pydantic model, `extra="forbid"`, a pure validator that returns error
strings rather than raising).

### 6.1 Rule schema

```yaml
- rule_id: OBS_BODY_WEIGHT_RANGE_001
  description: Body weight (LOINC 29463-7) must be within a plausible kg range.
  resource_type: Observation
  kind: value_range            # date_order | period_order | date_after_birth | value_range
  params:
    codes: ["29463-7"]
    units: ["kg", "kg/m2"]
    min: 0.2
    max: 700
  severity: major              # minor | major | critical
  criticality: non_blocker     # blocker → a FAIL forces BLOCK
  threshold: 0.0
  recommendation: "Check unit/value mapping for body-weight observations."
```

Rules are validated at service start (`load_config`); invalid rules are logged
and skipped, never crash the gate.

### 6.2 Kind → Kahn taxonomy

Each `kind` maps to a `(subcategory, context)` within the plausibility category
(`_KIND_TAXONOMY`):

| `kind` | subcategory | context | what it checks |
|---|---|---|---|
| `date_order` | temporal | verification | one date field ≥ another within a resource |
| `period_order` | temporal | verification | `period.start ≤ period.end` |
| `date_after_birth` | temporal | verification | a date is on/after the subject Patient's `birthDate` (cross-resource) |
| `date_before_death` | temporal | verification | a date is on/before the subject Patient's `deceasedDateTime` — OHDSI `plausibleBeforeDeath` (cross-resource; only assessed for deceased patients) |
| `not_in_future` | temporal | verification | a date is not after "now" — DAMA *timeliness* |
| `value_range` | atemporal | verification | a coded quantity falls in `[min, max]` **and** carries an expected unit |

`date_before_death` builds a `Patient/id → deceasedDateTime` index over the batch
(`_death_index`) — symmetric to the birth index — and is `assessed=False` for any
patient with no recorded death date (a living patient cannot have a
"before-death" violation). `not_in_future` compares against the service's current
UTC time over the common prefix length, so a date-only `2030-01-01` is correctly
flagged as future.

### 6.3 Evaluation algorithm (`rules.evaluate` / `_eval_one`)

For each rule, the engine selects target resources by `resource_type`, then
evaluates each one to a `(passed, assessed)` pair. The `assessed` flag is what
implements the NA semantics — a rule that *does not apply* to a resource (missing
field, code mismatch) does **not** count toward the denominator.

- **`date_order`** — resolve both date fields via FHIRPath; if either is absent,
  `assessed=False`. Otherwise pass iff `after ≥ before` (ISO-8601 strings compare
  lexicographically, which is chronologically correct for FHIR dates).
- **`period_order`** — read `period.start`/`period.end`; assessed only when both
  are strings; pass iff `start ≤ end`.
- **`date_after_birth`** — cross-resource. The engine first builds a
  `Patient/id → birthDate` index over the batch (`_birth_index`). For each target
  it resolves the date (FHIRPath), finds the `subject`/`patient` reference, looks
  up the patient's birthDate, and passes iff `date ≥ birthDate` (compared over
  the common prefix length, so `2020` vs `2020-01-01` compares correctly). Absent
  date, reference, or unknown patient → `assessed=False`.
- **`value_range`** — applies only when the observation's `code.coding` intersects
  the rule's `codes`. Then:
  1. **Unit check first.** If the rule lists `units` and the quantity's
     `code`/`unit` is *not* in that set, that is itself a violation — this is the
     classic "weight recorded in grams against a kg range" error from the data-
     quality literature. A wrong unit is implausible regardless of the number.
  2. **Range check.** Pass iff `min ≤ value ≤ max`.

FHIRPath evaluation is wrapped so a malformed expression logs and returns `[]`
rather than crashing the gate.

### 6.5 Verifying clinical *values* — why we don't ship universal ranges

A natural request is: *"check that observations/labs are in range and not trash."*
The naïve implementation — a bundled table of per-measurement min/max — is a
known anti-pattern:

- **OHDSI tried it and reversed course.** They *removed* most
  `plausibleValueLow`/`plausibleValueHigh` measurement ranges from their concept
  threshold files because "many of these ranges included plausible values and as
  such were causing unexpected check failures" ([OHDSI DQD changelog]). Universal
  bounds either fire on legitimate extremes or are set so wide they catch
  nothing.
- **No authoritative machine-readable LOINC→range table exists.** The serious
  efforts (ELaPro's 55 labs, reportable-interval studies covering 493 LOINC
  codes, the *lab2clean* algorithm) all conclude that usable bounds require
  *"clinical and terminologist expertise and a large dataset to prepare
  laboratory value distributions"* — i.e. they must be *derived* or *curated*,
  not invented generically.

So the Trust Gate verifies clinical values in a **layered pipeline**, in
precision order, none of which hardcodes universal clinical ranges:

1. **Unit correctness (UCUM).** The `value_range` kind treats an unexpected unit
   as a violation in its own right — a weight in `g` against a `kg` measurement
   fails on the unit alone. Mis-units are the highest-yield, least-ambiguous
   value error.
2. **Definitional unit bounds** (`plausibility.definitional_bounds`, §5.3). A
   value outside its *unit's mathematical definition* — a percentage `> 100` or
   `< 0` — is always wrong, independent of clinical context. This is the **only**
   layer that catches *whole-batch* corruption (e.g. an entire SpO₂ feed in the
   wrong unit), which the distribution-relative check is blind to because a
   uniformly-wrong batch looks internally clean. Ships only the universal `%`
   bound; operators may add unit-definitional bounds in config.
3. **Data-driven outliers** (`plausibility.value_outlier`, §5.3). A robust
   **modified z-score** (median + MAD, cut-off 3.5; Tukey IQR fallback) over the
   per-`(code,unit)` distribution flags values extreme *for this data* — the
   *lab2clean* approach. With a **persisted baseline** (§9.1) the reference is
   accumulated across batches, so small batches gain power and feed-level drift
   is caught. No invented numbers; only citable statistical methods.
4. **Concordance** (`plausibility.concordance`, §5.3). Cross-field
   contradictions — HL7 EHR-S's *concordance* dimension / OHDSI
   `plausibleGenderUseDescendants` (Verily's flagship example: a female-sex
   patient with a prostate-cancer condition). Config-driven gender↔code rules;
   ships one *illustrative* example and is designed to bind to terminology
   `memberOf` value sets in production.
5. **Operator-supplied authoritative ranges.** The `value_range` kind remains for
   when a deployment *has* a citable range (a lab's reportable interval, an
   IG-published limit). The repo ships **no invented vital/lab ranges** — the one
   bundled `value_range` rule is explicitly labelled *illustrative*.

What this pipeline deliberately does **not** claim is *accuracy* — whether a
plausible value is the *true* value. Per Kahn, accuracy is a *validation*-context
property requiring an external gold standard (source-system concordance), a later
phase. `withinVisitDates` (event dates inside the referenced Encounter period)
remains on the roadmap as a multi-resource ("N-ary") rule.

[OHDSI DQD changelog]: https://ohdsi.github.io/DataQualityDashboard/news/index.html

---

## 7. Score roll-up

Once every `CheckResult` exists, scoring is pure counting (`engine.assess`).

### 7.1 Per-category pass-rate

For each Kahn category, take only the **assessed** checks (drop NA), then:

```
category_score = 100 × (# checks PASSed) / (# checks assessed)
```

If a category has *no* assessed checks, its score is `null` — surfaced in the
passport as **"not assessed."** (E.g. conformance is `null` when no validator is
configured.)

### 7.2 Overall pass-rate

The same formula over **all** assessed checks across all categories:

```
overall_score = 100 × (# all checks PASSed) / (# all checks assessed)
```

When no checks were assessed at all, the passport sets `overall_score = null` and
`has_assessed = false` rather than claiming a 100% pass rate — "100% of 0 checks
passing" is vacuously true and would mislead a consumer into thinking the data
was clean. Dashboards and the UI must gate on `has_assessed` before rendering the
score; when `false` they should show "not assessed" rather than any numeric value.
The decision policy still applies provenance and category-floor gates regardless
of whether any checks ran.

---

## 8. The decision policy

The decision is a fitness-for-use *policy* on top of the measurement, with three
env-tunable cut-offs (`constants.py`):

| Constant | Env var | Default | Role |
|---|---|---|---|
| `PASS_MIN_RATE` | `TRUST_GATE_PASS_MIN_RATE` | 90 | overall pass-rate floor for a clean PASS |
| `CATEGORY_MIN_RATE` | `TRUST_GATE_CATEGORY_MIN_RATE` | 80 | per-category floor for a clean PASS |

### 8.1 Algorithm

```
blockers = [ critical checks where result == FAIL ]          # e.g. structural / profile
below    = [ category for category, score in category_scores
             if score is not None and score < CATEGORY_MIN_RATE ]

if blockers:                                  →  decision = BLOCK            (privacy NOT allowed)
elif (overall_score < PASS_MIN_RATE
      or below
      or not provenance_present):             →  decision = CONDITIONAL_PASS (privacy allowed, advisory)
else:                                          →  decision = PASS            (privacy allowed)
```

The structure mirrors the anonymizer's own "hard-constraint" scoring philosophy:
a single critical failure cannot be averaged away by otherwise-high scores. A
structurally invalid resource → `BLOCK`, full stop, regardless of how clean
everything else is.

### 8.2 Fitness-for-use

The decision drives `approved_for` / `not_approved_for` (`_fitness`). For
`CONDITIONAL_PASS`, the disallowed uses are **category-aware**: only the
dimensions that actually have gaps below their floor trigger the corresponding
restriction, so a dataset that passes conformance but has a completeness gap does
not inherit a conformance-driven restriction.

| decision | approved_for | not_approved_for |
|---|---|---|
| `PASS` | secondary use (research, analytics, AI training) | — |
| `CONDITIONAL_PASS` | cohort discovery; encounter-level descriptive statistics | category-specific: conformance < 80% → regulated sharing blocked; completeness < 80% → high-stakes analytics blocked; plausibility < 80% → clinical modelling blocked; provenance missing → regulated sharing blocked |
| `BLOCK` | — | any secondary use until blockers are resolved; if specific blockers are known, the check IDs are surfaced in the `not_approved_for` entry |

The restrictions accumulate: a dataset with both a completeness gap and missing
provenance will list both in `not_approved_for`. A CONDITIONAL_PASS where no
specific category is below the floor (e.g. overall score 88%, categories all at
or above 80%, provenance present) still restricts "high-stakes clinical modelling
without prior remediation" as the minimum advisory restriction.

### 8.3 Selectable audit phases — *measure exactly what you care about*

Every check belongs to an **audit phase** (`phases.py`) — a named suite the caller
can enable independently instead of running one generic pass:
`structural_conformance`, `terminology_validity`, `referential_integrity`,
`completeness_core`, `completeness_richness`, `value_plausibility`,
`temporal_plausibility`, `identity_integrity`, `provenance_auditability`,
`timeliness`, `source_accuracy`.

`assess(phases=[...])` runs only the selected phases (None → all, fully backward
compatible) and the passport reports a per-phase verdict
(`passport.phases[id] = {decision, score, blockers, …}`). Deselecting a phase that
owns an external call (the FHIR validator, the terminology server) **skips that
network call entirely** — a real cost/latency lever. Unknown phase ids are dropped
so a typo cannot silently disable everything.

### 8.4 The dimension scorecard + letter grade

On top of the Kahn *category* roll-up, the passport carries a **DQ-dimension
scorecard** (`dimensions.py`) mapping every check to a standard dimension
(**DAMA DMBOK + ISO/IEC 25012**): completeness, conformity, consistency, accuracy,
**plausibility**, uniqueness, integrity, currency, provenance. The `plausibility`
dimension (Wang & Strong 1996 *believability*) carries the outlier / definitional-bound
/ range checks: a statistical implausibility is not *accuracy* (agreement with a
real-world gold standard), which the passport does not claim. Terminology / value-set
membership is **conformity** (validity), not accuracy; only gold-reference agreement
(`conformance.source_of_truth`) is accuracy.

Each dimension gets a pass-rate and a **letter grade**, with the bands aligned to
the gate's decision floors so the grade and the verdict agree: **A ≥95, B ≥
`PASS_MIN_RATE` (90), C ≥ `CATEGORY_MIN_RATE` (80), D ≥60, F <60**. The two middle
cut-points are the policy floors (not magic numbers); A/D are conventional
excellent/marginal marks. The grade is **non-compensatory**: a failed *critical*
check floors that dimension's grade (and the `overall_grade`) to **F**, so a grade
can never read "A" next to a `BLOCK` decision (DAMA/ISO inherent-characteristic
independence). The authoritative verdict remains the decision + fitness statement.
A dimension only appears when at least one of its checks ran, so the grades reflect
what was actually measured.

```json
"scorecard": {
  "conformity":   {"grade": "A", "score": 100.0, "checks_passed": 6, "checks_assessed": 6},
  "completeness": {"grade": "C", "score": 78.0,  "checks_passed": 7, "checks_assessed": 9},
  "currency":     {"grade": "B", "score": 90.0,  "checks_passed": 9, "checks_assessed": 10}
},
"overall_grade": "B"
```

### 8.5 Sector targeting + purpose-bound fitness

**Sectors.** `assess(targets=[...])` re-runs the selected phases scoped to each
*sector* (by `resource_types`, `code_systems`, and/or a `fhirpath` cohort
predicate such as `Patient.gender = 'female'`) and emits an independent
per-sector verdict (`passport.targets[id]`) — so "labs PASS, demographics
CONDITIONAL" is visible at a glance. Each sector (and each per-phase) verdict is
computed by the **same shared decision policy** as the headline (the
`CATEGORY_MIN_RATE` floor, the provenance cap, and blockers all apply), so a
sector badge is never *more lenient* than the overall verdict.

**Purpose-bound fitness.** Data quality is **fitness for a *declared* use** (Juran;
Wang & Strong 1996; Kahn 2012: a dataset fit for cohort discovery may be unfit for
outcomes research), so the verdict is genuinely **task-dependent**: `intended_use`
selects a requirement profile, and a use is in `approved_for` only when *its* floors
hold. Cohort discovery / feasibility needs conformance only; outcomes research and
regulated submission additionally require completeness + plausibility floors,
dataset provenance, and **external validation** — so the same verification-only
dataset can be "fit for cohort discovery" yet "not fit for regulated submission
(requires: no external validation performed)". The statement always names the use,
e.g. *"Fit for research cohort discovery: grade B"* or *"Not fit for …"* on a BLOCK.
The intended use, phases, sector targets, and per-check thresholds are bundled into
reusable **trust profiles** stored by the anonymizer (`/v1/trust-profiles`) and
forwarded to the gate per request.

---

## 9. Resilience and safety semantics

The gate is designed so it **never produces a false PASS** and **never blocks
processing because of its own outage**.

- **NA over false-pass.** Any check that cannot run (no validator, no terminology
  server, mid-batch outage) becomes NA and is excluded from scoring — never
  counted as a pass (§4).
- **Circuit breakers.** Both the validator and terminology clients wrap calls in
  a `CircuitBreaker` (`breaker.py`): after `*_CB_THRESHOLD` failures the breaker
  opens and calls fail fast with `*Unavailable`, which the checks translate to
  NA. The breaker half-opens after `*_CB_RECOVERY_SEC`.
- **SSRF guard.** `validator_client._guard_url` blocks non-`http(s)` schemes and
  link-local / cloud-metadata targets (`169.254.169.254`) while allowing private
  RFC1918 ranges (sidecars resolve to private DNS by design). Overridable with
  `TRUST_GATE_ALLOW_ANY_HOST=true` for trusted test setups.
- **OperationOutcome shape-agnostic.** `_error_issues` parses both a bare
  `OperationOutcome` (`issue[]` with `severity`) and the HL7/Inferno
  validator-wrapper envelope (`outcomes[].issues[]` with `level`), so either
  validator works unmodified.
- **Anonymizer-side fail-soft.** When the anonymizer calls a Trust Gate that is
  unreachable, `intake_gate` returns a *degraded* advisory `CONDITIONAL_PASS`
  passport — never a silent `PASS`, and (in block mode) never a hard block caused
  by the gate's own failure.

### 9.1 The persisted baseline (optional, stateful)

The data-driven outlier check (§5.3) is by default **stateless** — its reference
distribution is the current batch. Two optional backends accumulate a baseline
*across* batches so small batches gain statistical power and whole-feed drift is
detectable:

- **Storage is a bounded reservoir.** Per `(code, unit)` the store keeps a
  fixed-size **reservoir sample** (Vitter's Algorithm R, default 500 values) — an
  unbiased sample of the population at O(1) storage regardless of volume, enough
  for a stable median/MAD/IQR. The Postgres backend merges under a row lock
  (`FOR UPDATE`) so concurrent batches accumulate safely.
- **Backends** (`get_baseline_store`): none (default, stateless);
  `TRUST_GATE_BASELINE=memory` (process-local, for single-process/dev/tests — not
  shared across uvicorn workers); `TRUST_GATE_BASELINE_DB_URL` (Postgres,
  durable + cross-replica). `psycopg2` is imported lazily, so the dependency is
  only needed when a baseline DB is configured.
- **Fail-soft.** Any baseline-store init error logs and falls back to stateless —
  the gate never fails to assess because the baseline backend is down.

---

## 10. Worked examples

### 10.1 A clean dataset → PASS

Input: a valid `Patient` (`gender`, `birthDate`, `meta.lastUpdated`) plus a
body-weight `Observation` (LOINC 29463-7, 72 kg), with **complete** dataset
provenance `{source_system: "LIS", extraction_time: "2024-01-01"}`, assessed
against a live validator. (Both `source_system` *and* `extraction_time` are
required — `REQUIRED_PROVENANCE_KEYS` — for `provenance_present` to be true.)

- conformance.structural: 0/2 violations → PASS; profile: NA (no `meta.profile`).
- coding_structure: 0 violations → PASS; terminology: NA (no server) ; reference_integrity: PASS.
- completeness.required_elements: PASS; value_or_absent: PASS.
- plausibility.uniqueness: PASS; OBS_BODY_WEIGHT_RANGE_001: 72∈[0.2,700], unit "kg" ok → PASS; date rules: NA or PASS.
- category_scores → conformance 100, completeness 100, plausibility 100; overall 100.
- no blockers, no category below floor, provenance present → **PASS**;
  approved for *secondary use (research, analytics, AI training)*.

### 10.2 A structurally invalid resource → BLOCK

Input: `{"resourceType": "Patient", "gender": "not-a-real-gender", "birthDate":
12345}` against a live validator.

- conformance.structural: the validator returns an `error` issue
  (`birthDate` not a date; `gender` not in the value set) → 1/1 violations →
  `violation_fraction = 1.0 > 0.0` → **FAIL**, and the check is **critical**.
- blockers = `["conformance.structural: 1/1 violating (100.0%) — Resolve FHIR
  validator error/fatal issues before sharing."]`
- decision → **BLOCK**; conformance category score 0%; privacy processing **not
  allowed**.

### 10.3 Incomplete provenance → CONDITIONAL_PASS

Same clean data and live validator as §10.1, but provenance carries only
`{source_system: "LIS"}` (no `extraction_time`):

- all checks PASS; category_scores conformance/completeness/plausibility all 100;
  overall 100.
- `provenance_present` is **false** (a required key is missing) → the policy
  caps the verdict at **CONDITIONAL_PASS**, with `not_approved_for` including
  *"regulated sharing (missing dataset provenance)."*

> **Note — the "no validator" case.** Run the §10.1 data with *complete*
> provenance but **no validator**: the authoritative `conformance.structural` /
> `conformance.profile` checks are NA, but conformance is **no longer fully
> unknown** — the validator-independent checks (`value_format`,
> `coding_structure`, `reference_integrity`) still run and score the category. So
> the gate catches *format-level* conformance errors (e.g. `birthDate: 12345`)
> even with no validator, while transparently marking the deep structural
> validation as "not assessed." If your deployment requires authoritative
> structural validation for any clean PASS, configure the validator (the
> structural check is critical → it will BLOCK genuinely invalid data). This
> trade-off is summarized in §13.

---

## 11. Integration with the anonymizer

The gate is wired into the privacy pipeline through two anonymizer modules.

- **`integrations/trust_gate/client.py`** — `TrustGateClient.assess_batch(...)`,
  circuit-broken, active only when `TRUST_GATE_SERVICE_URL` is set (else a no-op
  `None`).
- **`pipeline/intake_gate.py`** — the intake barrier. `assess_intake` always
  returns a verdict (never raises); `enforce_intake` raises `IntakeBlocked` only
  in block mode. Governed by `TRUST_GATE_MODE`:

| `TRUST_GATE_MODE` | behaviour |
|---|---|
| `warn` (default) | always assess + attach the passport; never block |
| `block` | raise `IntakeBlocked` (HTTP 422) when the decision is `BLOCK` |
| `off` | no-op (also when no service is configured) |

Wired at `/v1/process` and `/v1/process/batch`, dataset-level, before the privacy
stage. The passport is persisted to `medanon.processing_runs.trust_passport`
(JSONB) and served as Markdown at `GET /v1/processing-runs/{id}/passport`. The
React SPA renders it (chip + panel) on the Processing History page, and the
standalone Trust Gate UI (`:8401`) renders it for ad-hoc assessment.

---

## 12. Configuration reference

| Variable | Default | Effect |
|---|---|---|
| `TRUST_GATE_SERVICE_URL` | — | anonymizer-side: enables the intake gate when set |
| `TRUST_GATE_MODE` | `warn` | `warn` / `block` / `off` enforcement |
| `TRUST_GATE_VALIDATOR_URL` | — | FHIR validator base URL (conformance NA when unset) |
| `TRUST_GATE_VALIDATOR_ENDPOINT` | `/validate` | path; `{type}` substituted with resourceType (use `/{type}/$validate` for a FHIR server) |
| `TRUST_GATE_TERMINOLOGY_URL` | — | terminology server for `CodeSystem/$validate-code` (membership NA when unset; offline `code_wellformed` always runs) |
| `TRUST_GATE_PASS_MIN_RATE` | `90` | overall pass-rate floor for a clean PASS |
| `TRUST_GATE_CATEGORY_MIN_RATE` | `80` | per-category pass-rate floor |
| `TRUST_GATE_OUTLIER_MODZ` | `3.5` | modified z-score cut-off for value outliers |
| `TRUST_GATE_OUTLIER_IQR_K` | `3.0` | Tukey IQR multiplier (MAD-zero fallback) |
| `TRUST_GATE_OUTLIER_MIN_SAMPLE` | `20` | min values to assess a `(code,unit)` group |
| `TRUST_GATE_DRIFT_MODZ` | `3.5` | modified-z cut-off for cross-batch distribution drift |
| `TRUST_GATE_MAX_RECORD_LAG_DAYS` | — | max event→record lag for `record_lag` (NA when unset) |
| `TRUST_GATE_CURRENCY_WINDOW_DAYS` | — | freshness window vs extraction_time for `currency` (NA when unset) |
| `TRUST_GATE_AUDIT_SALT` | — | salt for hashing PHI-bearing identifier values in audit details |
| request `phases` / `targets` / `intended_use` / `reference` | — | select phases, request per-sector verdicts, purpose-bind the fitness verdict, supply a source-of-truth reference |
| `TRUST_GATE_BASELINE` | — | `memory` enables a process-local accumulated baseline |
| `TRUST_GATE_BASELINE_DB_URL` | — | Postgres DSN for a durable cross-replica baseline |
| `TRUST_GATE_BASELINE_RESERVOIR` | `500` | reservoir sample size per `(code,unit)` |
| `config/checks.yaml` `thresholds:` | per-check | per-check violation-fraction tolerance |
| `config/checks.yaml` `plausibility_rules:` | bundled | declarative clinical rules |
| `config/checks.yaml` `definitional_unit_bounds:` | `%`:[0,100] | unit-definitional value bounds |
| `config/checks.yaml` `concordance_rules:` | 1 illustrative | gender↔forbidden-code rules |
| `TRUST_GATE_*_CB_THRESHOLD` / `_CB_RECOVERY_SEC` | 5 / 30 | validator/terminology circuit breaker |
| `TRUST_GATE_ALLOW_ANY_HOST` | `false` | disable the SSRF guard (test only) |

---

## 13. Limitations and deferred work

- **FHIR-only.** Tabular/SQL data contracts, source-concordance connectors
  (LIS/pharmacy/MPI), and lineage (OpenLineage) are deferred to later phases.
- **Terminology membership is opt-in; well-formedness is always on.** Offline
  `conformance.code_wellformed` (format + SNOMED Verhoeff check digit, etc.) runs
  with no dependencies; authoritative ValueSet *membership* (`conformance.terminology`)
  needs a server with a licensed SNOMED CT import (e.g. Snowstorm) and is honestly
  NA until `TRUST_GATE_TERMINOLOGY_URL` is set.
- **IG conformance still needs the validator.** `conformance.profile` validates
  against an asserted `meta.profile`; validating against a target Implementation
  Guide (US Core, etc.) requires the FHIR validator loaded with that IG — deferred.
- **Plausibility coverage** is only as deep as the rule pack. The four bundled
  rules are starters; real deployments should grow `checks.yaml` per data source.
- **Dataset-level granularity.** The gate assesses the whole batch once; it does
  not currently gate streaming/bulk-export per-chunk (would need a buffering
  strategy).
- **Authoritative structural validation still needs the validator.** With
  `conformance.value_format` the conformance category is no longer fully "not
  assessed" without a validator — format, coding-structure, and reference checks
  run regardless. But the deep, profile-aware structural validation
  (`conformance.structural` / `conformance.profile`, the *critical* checks that
  can `BLOCK`) is only available when a validator is configured; otherwise it is
  transparently NA. A deployment that requires authoritative structural
  validation for any clean PASS should mandate the validator as policy (§10.3).
- **Concordance is config-driven and unbound.** `plausibility.concordance` ships
  one *illustrative* gender↔code rule with literal SNOMED codes; production should
  bind it to terminology value sets via `memberOf` rather than literal lists, and
  expand beyond gender. `withinVisitDates` (event dates inside the referenced
  Encounter period) is still a roadmap multi-resource ("N-ary") rule.
- **Plausibility ≠ accuracy (unless a reference is supplied).** The value checks
  verify a value is *possible* (verification), not that it is the *true* recorded
  value. True accuracy is now assessed by `conformance.source_of_truth` **when the
  caller supplies a gold reference**; without one it is NA. Live source-system
  concordance connectors (LIS/pharmacy/MPI) remain a later phase.
- **Cross-time consistency needs a baseline.** `plausibility.distribution_drift`
  catches feed/unit shifts only with a persisted baseline (§9.1); it is NA while
  stateless or until enough history has accumulated.
- **Timeliness is opt-in.** `record_lag` / `currency` are NA until their thresholds
  (and, for currency, `provenance.extraction_time`) are configured — the gate does
  not invent a notion of "timely" for your use.
- **Outlier detection needs volume.** `value_outlier` only assesses `(code,unit)`
  groups with ≥ `OUTLIER_MIN_SAMPLE` values *in the reference distribution*.
  Stateless (batch-only) it is therefore NA on small/single-resource requests;
  enabling a persisted baseline (§9.1) removes this limitation once enough history
  has accumulated. The complementary `definitional_bounds` check has no volume
  requirement and works on any batch size.

---

## 14. References

1. Kahn MG, Callahan TJ, Barnard J, et al. *A Harmonized Data Quality Assessment
   Terminology and Framework for the Secondary Use of Electronic Health Record
   Data.* eGEMs 2016;4(1):18. doi:10.13063/2327-9214.1244.
2. OHDSI *Data Quality Dashboard* — violation-rate-vs-threshold check scoring.
   https://ohdsi.github.io/DataQualityDashboard/
3. HL7 FHIR Validator / Inferno `fhir-validator-wrapper`.
   https://github.com/inferno-community/fhir-validator-wrapper
4. FHIR `$validate` and `CodeSystem/$validate-code` operations (HL7 FHIR R4).
5. HL7 EHR Work Group — *Data Quality* (dimensions: completeness, correctness,
   concordance, plausibility, currency).
   https://confluence.hl7.org/display/EHR/Data+Quality
6. Verily — *Elevating FHIR Data Quality* (tiered Binary/N-ary/Population rules;
   FHIRPath `resolve()`/`memberOf()`; CQL).
   https://verily.com/perspectives/elevating-fhir-data-quality
7. lab2clean: automated cleaning of retrospective clinical laboratory results.
   BMC Med Inform Decis Mak 2024. doi:10.1186/s12911-024-02652-7
8. Tukey JW. *Exploratory Data Analysis*, 1977 — the IQR fence used by the
   data-driven value-outlier check.
9. Iglewicz B, Hoaglin DC. *How to Detect and Handle Outliers*, ASQC 1993 — the
   modified z-score (median/MAD) used by the outlier and drift checks.
10. DAMA International. *DMBOK* — the data-quality dimensions used by the scorecard
   (completeness, conformity, consistency, accuracy, uniqueness, integrity,
   currency, provenance).
11. ISO/IEC 25012 *Data quality model* — the dimension framing for the scorecard.
12. Wang RY, Strong DM. *Beyond Accuracy: What Data Quality Means to Data
   Consumers.* J. of MIS 1996 — data quality as fitness for the consumer's use
   (the basis for the purpose-bound fitness verdict).
13. SNOMED CT Technical Implementation Guide — SCTID Verhoeff check digit, used by
   the offline `code_wellformed` check.

---

*Source of truth: `services/trust-gate/src/` (`engine.py`, `passport.py`,
`constants.py`, `rules.py`, `checks/`) and `services/anonymizer/src/pipeline/
intake_gate.py`. Operational quick-start: [trust-gate.md](trust-gate.md).*
