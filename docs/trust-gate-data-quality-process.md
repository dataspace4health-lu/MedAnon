# The data-quality process, step by step

This document walks through exactly how the Trust Gate **measures** data quality
and then **ensures** it, from the moment a dataset arrives to the moment a verdict
is enforced and remediation is tracked. Every step names the real function or file
that does the work, so this is the process as the code actually runs it.

The process has two halves:

- **Part A: Measuring quality** (steps 1 to 9) turn raw records into a scored,
  graded verdict.
- **Part B: Ensuring quality** (steps 10 to 14) enforce the verdict, feed it back,
  and keep quality under control over time.

A short final section lists the guarantees that make every step trustworthy.

---

## Part A: Measuring quality

### Step 1: The dataset reaches the barrier

De-identification never starts cold. The anonymiser calls the intake barrier first
([anonymizer/src/pipeline/intake_gate.py](../services/anonymizer/src/pipeline/intake_gate.py),
`enforce_intake`), which forwards the records to the Trust Gate microservice over
HTTP (`POST /v1/trust/assess/batch`).

If no Trust Gate is configured, or `TRUST_GATE_MODE=off`, this is a no-op and
processing proceeds. Otherwise the barrier hands the Trust Gate the records, the
declared intended use, and (optionally) a named **trust profile** that selects which
quality concerns to run.

### Step 2: Configure the assessment

Inside the service, [api/service.py](../services/trust-gate/src/api/service.py)
`run_assessment` decides *what* to measure before measuring anything:

1. **Resolve the use case.** The declared use case maps to a subset of quality
   phases and any threshold tweaks (`resolve_use_case`). An explicit `phases`
   request overrides this; an unknown use case falls back to all phases.
2. **Merge thresholds.** The service starts from the configured `THRESHOLDS`
   (loaded from `config/checks.yaml` plus environment) and layers the use-case
   tweaks on top. Nothing is a hardcoded magic number.
3. **Resolve critical-to-quality checks.** The use case can promote specific checks
   to "critical", so a failure there blocks (this is the RBQM "focus on critical
   data" idea from clinical trials).
4. **Load rules.** The built-in plausibility rule pack plus any caller-supplied
   `custom_rules` are parsed. A custom rule that does not parse raises HTTP 422 and
   is never silently dropped (`parse_custom_rules`).

### Step 3: Normalise the input

`flatten()` turns whatever arrived (a single resource, a Bundle, or a list) into a
flat list of records plus the Bundle `fullUrl` values. The fullUrls are kept so the
reference-integrity check can later resolve `urn:uuid:` references inside a Bundle.

### Step 4: Gather every input into one context and run the checks

The engine ([engine.py](../services/trust-gate/src/engine.py) `assess`) packs every
input (records, selected phases, validator, terminology client, thresholds, rules,
baseline store, reference time, and so on) into one immutable `AssessmentContext`.

It then runs the check suite through the registry
([verdict/runner.py](../services/trust-gate/src/verdict/runner.py) `run_checks`,
[verdict/registry.py](../services/trust-gate/src/verdict/registry.py)). The registry
lists each check as a strategy, so the suite is defined in one place. The checks
that run are, by concern:

- **Conformance** presence, format, coding structure, code well-formedness,
  reference integrity, and (if enabled) the external structural, profile, IG, and
  terminology checks.
- **Completeness** required elements, value-or-absent, element density.
- **Plausibility** uniqueness, definitional bounds, concordance, the clinical rule
  pack, and the statistical outlier and drift checks.
- **Timeliness, accuracy, identity, provenance** the remaining single-concern
  checks.

Each check is then tagged with its phase and its data-quality dimension, marked
deterministic or advisory, and finally the engine keeps only the checks whose phase
was selected in Step 2.

### Step 5: Measure each check the same way

This is the atomic measurement, identical for every check (the OHDSI Data Quality
Dashboard method, defined on `CheckResult` in
[passport.py](../services/trust-gate/src/passport.py)):

1. Count `applicable` = how many rows or items the check could apply to.
2. Count `violations` = how many of those broke the rule.
3. Compute `violation_fraction = violations / applicable`.
4. Compare it to the check's `threshold`:

```
applicable == 0                      →  NA    (nothing to assess; never a pass)
violation_fraction  >  threshold     →  FAIL
violation_fraction <= threshold      →  PASS
```

Worked example: 100 Observations, 3 with no value and no dataAbsentReason. Then
`applicable = 100`, `violations = 3`, `fraction = 0.03`. The threshold for
`completeness.value_or_absent` is `0.0`, so `0.03 > 0.0` and the check FAILs. Each
violating record also gets a capped, identifier-free note added for the audit
(`add_detail`).

The exact `applicable` and `violations` definition for every check is in the
companion doc [trust-gate-how-it-works.md](trust-gate-how-it-works.md), sections 12
to 16.

### Step 6: Separate the reliable checks from the statistical hints

The engine splits the results in two:

- **Deterministic checks** (structural, format, code, completeness, referential,
  temporal, concordance, definitional bounds, uniqueness) are reproducible. These,
  and only these, drive the verdict.
- **Advisory checks** (statistical outliers and cross-batch drift) use randomness
  and batch-relative distributions, so they are reported as side notes and can
  never move the verdict, the scores, or the grades.

This split is enforced by an allow-list in
[constants.py](../services/trust-gate/src/constants.py); any check nobody has
classified defaults to advisory, so it fails safe.

### Step 7: Roll the results up into scores and grades

All of this uses one formula, `100 x passed / assessed`, over the deterministic
checks ([verdict/scoring.py](../services/trust-gate/src/verdict/scoring.py)):

1. **Category scores** the percent passing within each of the three Kahn
   categories (conformance, completeness, plausibility). A category with nothing
   assessed is reported as "not assessed", not as 100.
2. **Overall score** the percent passing across all assessed deterministic checks.
3. **Per-dimension scorecard** the same percent per data-quality dimension
   (completeness, conformity, accuracy, and so on), each with a letter grade.
4. **Letter grade** bands aligned to the decision floors (A at 95, B at 90, C at
   80, D at 60, F below). A failed **critical** check floors the grade to F no
   matter how high the pass-rate is, so a headline grade can never read "A" while
   the verdict is a block.

### Step 8: Record what actually ran (coverage)

Before deciding fitness, the engine computes coverage
([verdict/coverage.py](../services/trust-gate/src/verdict/coverage.py)): how many
checks were assessed out of the total, which "depth" capabilities did not run (no
validator, no terminology server, no reference dataset, no clinical rule pack),
which phases were descoped, and whether any external validation happened at all.

This is what stops a grade computed over a thin slice from reading like a full,
externally-validated certification. The coverage caveat is attached to the verdict.

### Step 9: Decide the verdict and the fitness

The policy layer ([verdict/decision.py](../services/trust-gate/src/verdict/decision.py)
`decide`) turns the scores into one of three decisions, in this order:

```
any critical check FAILed                                   →  BLOCK
overall < 90, or any category < 80, or a resource type      →  CONDITIONAL_PASS
   below its threshold, or required provenance missing
otherwise                                                    →  PASS
```

Then fitness-for-use ([verdict/fitness.py](../services/trust-gate/src/verdict/fitness.py))
matches the declared use to a requirement profile and produces the `approved_for`
and `not_approved_for` lists, plus a graded, purpose-bound statement. A use is
approved only when its own floors are met, so "fit for counting patients" and "fit
for outcomes research" are separate answers. Higher-stakes uses additionally
require provenance and external validation.

The engine assembles all of this into the **Quality Passport**.

---

## Part B: Ensuring quality

Measurement produces a verdict. These steps are what actually *ensures* quality:
enforcement, feedback, tracking, and control over time.

### Step 10: Enforce the verdict at the barrier

Back in the anonymiser, the intake barrier acts on the decision according to
`TRUST_GATE_MODE`:

- **block** a `BLOCK` verdict raises `IntakeBlocked`, the pipeline stops, and the
  data is never de-identified. The exception message is the plain-language reason,
  suitable for an HTTP 422 or a job error.
- **warn** (default) the passport is attached and processing continues, so teams
  can see quality without a hard gate while they roll it out.
- **off** no enforcement.

Enforcement is **fail-soft as a service**: if the Trust Gate itself is unreachable,
the barrier degrades to an advisory CONDITIONAL_PASS. It never silently passes on an
outage, and in block mode it never hard-blocks on the gate's own downtime.

`MEDANON_REGULATED_MODE` tightens this: in regulated mode a critical or conformance
check that could **not run** (validator or terminology server unavailable) becomes a
block, because you cannot release regulated data on an assessment that did not
complete.

### Step 11: Produce the passport and a headline label

The passport is serialised ([passport.py](../services/trust-gate/src/passport.py)
`to_dict`) and a short human label is derived (`build_label`) for dashboards and
lists. Any identifier-bearing value that a check needs to cite (an MRN, an SSN) is
first reduced to a stable, non-reversible token (`redact_token`), so the passport is
safe to store and share.

### Step 12: Persist the passport and open remediation findings

`persist()` in [api/service.py](../services/trust-gate/src/api/service.py) saves the
passport and then derives a set of open **findings** (the specific problems, each
tied to the checks that raised it) into the findings store. These become the
remediation backlog a data provider works through.

Two assurance properties here:

- **Idempotent writes.** A supplied `idempotency_key` collapses an at-least-once
  retry or a racing replica onto one assessment row, so retries do not create
  duplicate records.
- **Best-effort persistence.** A store outage is logged but never blocks or fails
  the assessment itself. Measuring quality must not depend on the audit database
  being up.

### Step 13: Feed the result back to the data provider

The passport is designed to be actionable, not just a score:

- **Blockers** plain-language lines for each failed critical check, with the count
  of affected records and a recommendation.
- **Approved / not-approved lists** what the data can and cannot be used for, and
  the specific unmet requirement for each blocked use.
- **Suggested thresholds** a Deequ-style advisory hint: for each currently-failing
  check, the smallest tolerance that would admit this batch, so a reviewer can adopt
  it deliberately. This is advisory only and never auto-applied.
- **Per-phase and per-sector sub-verdicts** so a mixed dataset shows which cohort
  or which concern is dragging the verdict down, each under the same policy as the
  headline (a sector can never badge greener than the whole).

### Step 14: Keep quality under control over time

Two mechanisms make this a continuous process rather than a one-shot check:

- **Cross-batch baseline and drift.** The plausibility checks read and update a
  persisted per-`(code, unit)` baseline reservoir
  ([baseline.py](../services/trust-gate/src/baseline.py)). Small batches gain
  statistical power from history, and a sudden shift in a measurement's distribution
  (a unit change, a device recalibration, a switched feed) is caught as drift. The
  sampler is seeded, so the accumulated baseline stays reproducible.
- **A Plan-Do-Study-Act loop.** Persisted passports and open findings let a provider
  measure, remediate, re-submit, and confirm the fix over successive batches. The
  idempotency key ties re-submissions to the same assessment lineage.

---

## The guarantees that make every step trustworthy

These hold across the whole process and are the reason the verdict can be relied on:

1. **No mutation.** The gate reads and reports; it never edits a record.
2. **Fail closed, never silent.** A check that cannot run is NA and is dropped from
   scoring, never counted as a pass. A missing measurement can never inflate a
   score, and (in regulated mode) an un-runnable critical check blocks.
3. **Deterministic verdict.** Only reproducible checks drive the decision;
   statistical signals are advisory. The same bytes in produce the same verdict out.
4. **Honest coverage.** The passport always says how much of the suite ran and
   whether external validation happened, so a partial or verification-only pass is
   never mistaken for a full clean bill of health.
5. **Measurement separate from policy.** One layer measures (percent of checks
   passing, no invented weights); a separate layer decides fitness. Changing "what
   is good enough" never changes "how we measure", which keeps the scoring defensible
   and the policy tunable.

## The process in one paragraph

A dataset hits the intake barrier, which asks the Trust Gate to assess it. The gate
figures out what to measure (from the declared use and selected phases), runs every
applicable check, and scores each one the same way: how many records broke the rule
versus a set tolerance, giving pass, fail, or not-applicable. It keeps only the
reproducible checks for the verdict, rolls them into category and overall scores and
letter grades, records what it could not check, and produces a traffic-light verdict
tied to what the data is fit for. The barrier then enforces that verdict (block,
warn, or off), the passport and its open findings are stored for remediation, and a
running baseline watches for drift across batches, so quality is not just measured
once but kept under control over time.
