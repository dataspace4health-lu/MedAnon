# Trust Gate architecture: how it evaluates the data

This document describes the internal architecture and the exact evaluation
pipeline: every phase, in execution order, what each one inspects, how a single
check turns data into a verdict, how the verdicts roll up into scores, and how the
final PASS / CONDITIONAL_PASS / BLOCK decision is reached. It is the companion to
`docs/trust-gate.md` (the reference) and reflects the real orchestration in
`services/trust-gate/src/engine.py` (a thin pipeline over the `verdict/` package).
See `services/trust-gate/ARCHITECTURE.md` for the layer diagram and design patterns.

---

## 1. Module map

The service is organized in layers; dependencies point downward only (nothing
imports `engine`, and the domain model imports nothing above it). See
`services/trust-gate/ARCHITECTURE.md` for the one-page layer diagram.

```
services/trust-gate/src/
  main.py            composition root: builds `app`, mounts routers (uvicorn main:app)
  api/               HTTP surface (split from the former monolithic main.py):
    schemas.py         request/response Pydantic models
    service.py         business logic: flatten, run_assessment, persist
    config.py          check config loaded once (RULES, THRESHOLDS, POLICY)
    deps.py            store/findings accessors (503 when unconfigured)
    metrics.py         Prometheus counters/histograms
    routers/           one module per surface:
      assess.py          /v1/trust/assess[/omop|/batch]
      connectors.py      /v1/trust/connectors/file | /sql | /sql/tables
      datasets.py        provider/dataset reads + GDPR Art. 30 record
      findings.py        remediation findings (PDSA loop)
      catalog.py         /v1/metric-catalog, /v1/use-cases
  engine.py          thin orchestrator: assess() and assess_omop() only
  verdict/           the verdict layer (extracted from engine.py):
    context.py         AssessmentContext  immutable check inputs (Parameter Object)
    registry.py        the FHIR check suite as a list of strategies (Strategy pattern)
    runner.py          iterates the registry + per-sector / per-phase sub-reports
    scoring.py         category/overall roll-up, dimension scorecard, grades, advisory
    decision.py        PASS/CONDITIONAL/BLOCK policy + per-resource-type thresholds
    coverage.py        assessment-coverage transparency (what actually ran)
    fitness.py         purpose-bound fitness-for-use lists + statement
  passport.py        CheckResult + QualityPassport models (data only)
  reporting/
    markdown.py        renders a QualityPassport as Markdown (model stays render-free)
  phases.py          audit-phase ids + phase/dimension tagging
  dimensions.py      DAMA / ISO 25012 dimension list + grading
  constants.py       thresholds, cut-offs, env-tunable knobs, sampler seed
  rules.py           declarative plausibility-rule engine (config/checks.yaml)
  baseline.py        seeded value-baseline store (outliers/drift)
  label.py           EHDS quality+utility+maturity+FAIR label
  profiling.py       descriptive data profile (incl. value distributions)
  validator_client.py    FHIR validator adapter (fail-soft)
  terminology_client.py  terminology $validate-code adapter (fail-soft)
  checks/
    conformance/     package: presence, validation, terminology, references, _shared;
                     __init__.evaluate() is the orchestrator (structural, format,
                     terminology, references, status, coding, IG)
    completeness.py  required elements, value-or-absent, element density
    plausibility.py  outliers, definitional bounds, concordance
    timeliness.py    currency, record lag, not-in-future
    accuracy.py      comparison against a supplied source-of-truth reference
    identity.py      patient-identity stability, uniqueness
    governance.py    provenance / auditability, SAM provenance
    dqd.py           OHDSI DQD checks over OMOP tables
    clinical_eval.py demographic-stratified outliers + clinical logic + distributions
  cdm/
    omop_model.py    OMOP CDM table specs, OmopData container
    fhir_to_omop.py  FHIR -> OMOP normalization
    tabular_to_omop.py  source/tabular -> OMOP via a column-mapping spec
  store/
    passport_store.py  assessments + check_results + audit_log
    findings_store.py   derived findings + triage
```

---

## 2. Request lifecycle

```
client (UI / SPA / anonymizer)
   |  POST /v1/trust/assess[/batch|/omop]   { resources|tables, dataset_id, provider_id, use_case, ... }
   v
api/routers/assess.py  ->  api/service.run_assessment
   |  resolve use_case -> (phases, thresholds, critical-to-quality ids)   [metric_catalog]
   |  normalize FHIR (flatten Bundle/list) OR normalize OMOP/tabular -> OmopData
   v
engine.assess(...) / engine.assess_omop(...)
   |  0. build an AssessmentContext (verdict.context) from the inputs
   |  1. run the check pipeline (verdict.runner over verdict.registry) -> list[CheckResult]
   |  2. split deterministic vs advisory
   |  3. elevate critical-to-quality checks (verdict.decision)
   |  4. score: category + overall + scorecard + per-phase verdicts (verdict.scoring/runner)
   |  5. decide: PASS / CONDITIONAL_PASS / BLOCK (verdict.decision)
   |  6. build: coverage, fitness, advisory report, EHDS label, data profile
   |  -> QualityPassport
   v
api/service.persist
   |  persist passport + check_results + audit_log    (best-effort, fail-open)
   |  derive findings from failing deterministic checks
   v
client  <-  QualityPassport (JSON)
```

Persistence is best-effort: a store outage logs a warning and the passport is
still returned. The assessment never blocks on persistence.

---

## 3. The evaluation pipeline (FHIR path)

`engine.assess()` builds an `AssessmentContext` and calls
`verdict.runner.run_checks(ctx)`, which iterates the check registry
(`verdict.registry.CHECK_REGISTRY`). The registry runs the check families in this
exact order; each returns one or more `CheckResult` objects.

```
run_checks(ctx):   # iterates CHECK_REGISTRY

  Phase A  CONFORMANCE        checks/conformance.evaluate(resources, validator, terminology)
           - conformance.structural          (FHIR validator $validate, fail-soft -> NA)
           - conformance.profile             (meta.profile conformance)
           - conformance.resource_type_present, conformance.resource_id_present
           - conformance.value_format        (ISO-8601 dates, id pattern; reports the value)
           - conformance.coding_structure, conformance.code_wellformed
           - conformance.terminology         ($validate-code or offline format)
           - conformance.reference_integrity (literal references resolve in the set)
           - conformance.status_not_entered_in_error
           - conformance.provenance_present, conformance.source_of_truth

  Phase B  COMPLETENESS       checks/completeness.evaluate(resources)
           - completeness.required_elements
           - completeness.value_or_absent    (top-level value[x], component value[x], or dataAbsentReason)
           - completeness.element_density    (recommended-element fill rate)

  Phase C  PLAUSIBILITY       checks/plausibility.evaluate(resources, plausibility_rules, baseline)
           - plausibility.definitional_bounds  (unit-definitional limits, e.g. % in [0,100])
           - plausibility.concordance          (cross-field clinical coherence)
           - plausibility.value_outlier        (ADVISORY: robust modified z-score, computed in
                                                 log-space for strictly-positive analytes, + Tukey IQR vs baseline)
           - the declarative plausibility_rules (config/checks.yaml):
               date_after_birth, date_before_death (exempt_codes), date_order,
               period_order, not_in_future, value_range

  Phase D  TIMELINESS         checks/timeliness.evaluate(resources, extraction_time)
           - plausibility.currency, plausibility.record_lag, OBS_NOT_FUTURE_*

  Phase E  ACCURACY           checks/accuracy.evaluate_against_reference(resources, reference)
           - conformance.source_of_truth   (compare fields to a supplied authoritative reference)

  Phase F  IDENTITY           checks/identity.evaluate_patient_identity(resources)
           - plausibility.uniqueness, plausibility.patient_identity_stable

  Phase G  PROVENANCE (SAM)   checks/governance.evaluate_provenance_sam(resources)
           - conformance.provenance_present (structured-as-meaning prerequisite)

  then:    tag each check with its audit phase + DAMA dimension + advisory flag
           drop checks whose phase is not in `selection`
```

Two of these families call external services (fail-soft):

- `conformance.structural` and `conformance.profile` call the FHIR validator.
- `conformance.terminology` calls the terminology server `$validate-code`.

If a phase is deselected (the caller passed a `phases`/`use_case` subset), its
checks are skipped, and deselecting the structural/terminology phase also skips
those two slow external calls.

After `run_checks`, `engine.assess()` adds:

- `governance.evaluate()` for the `auditability` block (provenance present, etc.).
- the demographic distributions and clinical-logic checks come from the OMOP path
  (`clinical_eval`); on the FHIR path the value distributions come from
  `profiling.profile()`.

---

## 4. How a single check evaluates the data

Every check is a `CheckResult` with four counters and a derived result:

```
applicable   how many records (or values) the check could be applied to
violations   how many of those failed the check's predicate
threshold    the allowed violation fraction (OHDSI DQD failClass; 0.0 = zero tolerance)
violation_fraction = violations / applicable        (0 when applicable == 0)

result =
   NA     when applicable == 0          (the check did not apply to this data)
   FAIL   when violation_fraction > threshold
   PASS   otherwise
```

A check walks the resources, increments `applicable` for each record it inspects,
increments `violations` for each that fails, and records a PHI-safe
`violation_detail` for each failure: the record (`resource_type` + `resource_id`),
the field `path`, a short `detail`, and for the evaluable defects an example
`value` (a malformed token, a status, an out-of-range number, a before-birth date).

Worked example, `completeness.value_or_absent`:

```
for each Observation with a status:
    applicable += 1
    has_value = top-level value[x] present
                OR any component[].value[x] present
                OR any component dataAbsentReason
    if not has_value and no top-level dataAbsentReason:
        violations += 1
        add_detail(record, "value[x] | component.value[x] | dataAbsentReason", ...)
result = FAIL if violations/applicable > 0.0 (threshold) else PASS
```

This is why a Blood Pressure panel (results in `component[].valueQuantity`) is not
flagged: the component branch sets `has_value`.

---

## 5. Phase tagging and selection

Each check is tagged with:

- an **audit phase** (`phases.phase_for_builtin` / `phase_for_kind`): one of
  `structural_conformance`, `terminology_validity`, `referential_integrity`,
  `completeness_core`, `completeness_richness`, `value_plausibility`,
  `temporal_plausibility`, `identity_integrity`, `provenance_auditability`,
  `timeliness`, `source_accuracy`.
- a **DAMA / ISO 25012 dimension** (`dimensions`): completeness, conformity,
  consistency, accuracy, uniqueness, integrity, currency, provenance.
- an **advisory flag**: true for the statistical checks in `ADVISORY_CHECK_IDS`
  (`plausibility.value_outlier`, `plausibility.distribution_drift`,
  `plausibility.value_outlier_stratified`).

`normalize_selection(phases)` resolves the requested phases (None means all). Only
checks whose phase is in the selection survive; the rest are dropped before
scoring. This is how a `use_case` or trust profile narrows the assessment to the
metrics that matter for the declared purpose.

---

## 6. Determinism split

```
checks = run_checks(ctx)                              # verdict.runner over the registry
det_checks = [c for c in checks if not c.advisory]   # drive the verdict
adv_checks = [c for c in checks if c.advisory]        # reported, never decide
```

The decision, the category scores, the scorecard, and the phase verdicts are all
computed from `det_checks` only. `adv_checks` produce a separate advisory report.
The statistical checks are made reproducible by the seeded reservoir sampler
(`TRUST_GATE_SAMPLER_SEED`) and version-pinned baselines, so a re-run yields a
byte-identical passport (minus timestamps).

---

## 7. Scoring

Three roll-ups, all over `det_checks`:

Category and overall (`verdict.scoring.category_and_overall`):

```
for cat in (conformance, completeness, plausibility):
    assessed = checks in cat with result != NA
    category_score[cat] = 100 * (#PASS in assessed) / len(assessed)   (None if empty)

overall = 100 * (#PASS over all assessed det_checks) / (#assessed det_checks)
```

Per-dimension scorecard (`verdict.scoring.scorecard`): the same percentage and a
letter grade computed per DAMA/ISO 25012 dimension. The grade bands are aligned to
the decision floors so the grade and the verdict agree: A >= 95, B >= PASS_MIN_RATE
(90), C >= CATEGORY_MIN_RATE (80), D >= 60, F otherwise. The grade is
non-compensatory: a failed *critical* check floors its dimension's grade (and the
overall grade) to F, so a grade can never read "A" next to a BLOCK.

Per-phase verdict (`verdict.runner.phase_report`) and per-sector verdict
(`verdict.runner.targets_report`): each is computed by the SAME shared decision
policy as the headline (`verdict.decision.subset_decision`), so a phase or sector
badge can never read more leniently than the overall verdict. These drive the
horizontal QC pipeline in the UI.

---

## 8. Decision policy

```
verdict.decision.decide(category_scores, overall, blockers, rt_below, provenance_capped):

  below = [cat for cat, s in category_scores if s is not None and s < CATEGORY_MIN_RATE]

  if blockers:                                          return BLOCK
  if overall < PASS_MIN_RATE                            return CONDITIONAL_PASS
     or below
     or rt_below                  (per-resource-type pass-rate under its tuned threshold)
     or provenance_capped         (dataset provenance missing)
  else                                                  return PASS
```

`blockers` are the failed checks marked `critical` (`verdict.decision.blockers`). `PASS_MIN_RATE`
is 90 and `CATEGORY_MIN_RATE` is 80 by default, both env-tunable. The advisory
checks are not in `det_checks` and so cannot move this decision.

---

## 9. Critical-to-quality elevation (RBQM)

Before scoring, `_elevate_critical(det_checks, critical_check_ids)` marks the use
case's critical-to-quality checks as `critical`. A failure on any of them then
appears in `blockers` and forces BLOCK. This implements ICH E6(R3): inspect the
critical data elements deeply for the declared purpose (for example
`regulatory_rwe` marks terminology, required-elements, and reference integrity as
critical-to-quality).

---

## 10. The OMOP / DQD evaluation path

`engine.assess_omop()` reuses the same scoring, decision, scorecard, and label
machinery so an OMOP passport is comparable to a FHIR one. The check set is:

```
checks  = dqd.run_dqd_checks(omop, value_checks)        # OHDSI DQD pattern
checks += clinical_eval.evaluate(omop)                  # stratified outliers + clinical logic
```

`dqd.run_dqd_checks` produces, over the normalized OMOP tables:

- `dqd.table.{t}.present`        required CDM tables (REQUIRED_TABLES: person)
- `dqd.{table}.{col}.not_null`   NOT NULL on required fields
- `dqd.{table}.{col}.fk`         foreign-key integrity between tables
- `dqd.{table}.{col}.date_format`  date/datetime format
- `dqd.{table}.{col}.value_range`  data-driven value ranges (reports the value)
- `dqd.{table}.{col}.completeness` recommended-field fill rate

`clinical_eval.evaluate` adds `clinical.measurement_after_birth` (deterministic)
and `plausibility.value_outlier_stratified` (advisory), and
`clinical_eval.value_distributions(omop)` attaches the per-(concept, unit) and
per-stratum distributions to the passport profile.

Source adapters (`cdm/`) normalize the input first:

```
FHIR resources  --fhir_to_omop-->   OmopData  --> dqd + clinical_eval
tabular tables + mapping  --tabular_to_omop-->  OmopData
OMOP tables (already CDM)  -------------------->  OmopData
```

---

## 11. Clinical value distributions and stratification

`clinical_eval.value_distributions(omop)` returns two lists:

- `observation_value_stats`: per (concept, unit). For each: count, min, max, mean,
  median, standard deviation, and a histogram (bin counts over [min, max]).
- `observation_value_stats_stratified`: the same per (concept, unit, stratum),
  where the stratum is `sex | age-band` derived from the OMOP `person` table.

Stratification matters clinically: creatinine, hemoglobin, and many hormones
differ by sex, and pediatric vitals differ from adult. The stratified outlier
check flags a value as extreme within its own demographic stratum, so a normal
pediatric value is not judged against an adult pool. The UI renders each concept
as a data card (histogram + stats + the stratum breakdown).

---

## 12. The patient-compartment full-server scan

A full-server scan is orchestrated client-side (`client/src/api/fhirScan.ts`,
`scanByPatient`) because the engine assesses one batch at a time. The unit is the
patient compartment so relational and temporal checks see real context.

```
scanByPatient(connection):
  presentIds = {}            # global dedupe + reference index
  buffer = []                # complete compartments awaiting a batch assess

  for each Patient page:
     for each Patient:
        compartment = GET Patient/{id}/$everything        # the complete record
        if buffer + compartment would exceed chunkSize: flush(buffer)   # keep compartments intact
        for each resource in compartment:
            if id already seen: skip                       # dedupe shared Practitioner/Organization
            else: add to buffer, record id + references
        if buffer >= chunkSize: flush(buffer)

  # sweep non-patient-compartment resources (Organization, Practitioner, ...)
  for each non-compartment type:
     for each resource: add (deduped) to buffer; flush when full

  flush(buffer)
  referenceIntegrity = references whose target is not in presentIds   # global
  return aggregateChunks(per-batch passports, referenceIntegrity)

flush(buffer) = POST /v1/trust/assess/batch  -> one per-batch passport
```

`aggregateChunks` folds the per-batch passports into one server-wide passport: it
sums each check's applicable/violations and recomputes PASS/FAIL/NA, pools the
category and overall scores, recomputes referential integrity globally, and merges
the per-(code, unit) value distributions (pooled count/min/max + count-weighted
mean, standard deviation reconstructed from each batch, histograms re-binned onto a
global range). There is no resource cap: the whole server is ingested, streamed
patient by patient, so memory stays bounded regardless of dataset size.

Correctness by scope on a scan:

- per-resource conformance and completeness: correct (each record assessed once).
- referential integrity: correct (global id/reference index, not per batch).
- patient timeline / clinical-logic: correct (a patient's record is one batch).
- distributions and uniqueness: correct (pooled across the whole scan).

---

## 13. Failure and determinism guarantees

- External validator / terminology outage: the affected checks return NA, never
  FAIL and never silent PASS. Per the determinism contract a timing-induced NA
  does not move the deterministic verdict.
- Store outage: persistence is skipped (logged), the passport is still returned.
- Trust Gate unreachable from the anonymizer intake gate: the verdict degrades to
  an advisory CONDITIONAL_PASS, never a silent PASS and never a hard block.
- Reproducibility: deterministic verdict + seeded sampler + version-pinned
  baselines means the same input yields a byte-identical passport (minus
  timestamps). A diff is a bug.
- PHI safety: the passport stores tokens, field paths, and example values of the
  evaluable defects only, never names or free text.
```
