# Trust Gate: clinical data-quality QC platform

The Trust Gate is a standalone service that evaluates the quality of clinical
data before it is used. A data provider submits a dataset (FHIR, OMOP CDM, or
SQL/tabular) and receives back a **Quality Passport**: a deterministic
PASS / CONDITIONAL_PASS / BLOCK verdict, a per-dimension scorecard, an EHDS-style
dataset label, the clinical value distributions, and a remediation findings list.
It reads the data and evaluates it; it does not transform it.

The Trust Gate sits in front of the de-identification engine as a pre-privacy
intake barrier, but that is only one consumer. The platform is a general clinical
data-quality QC tool: providers can submit data to it directly and track quality
across runs over time.

Grounded in: Kahn et al. 2016 (harmonized data-quality terminology), the OHDSI
Data Quality Dashboard (DQD), ISO/IEC 25012 + 25024 and DAMA DMBOK (quality
dimensions), the EHDS quality and utility label (Regulation Art. 56), ICH E6(R3)
RBQM (critical-to-quality), and ALCOA++ / 21 CFR Part 11 (audit t raceability).

---

## 1. Architecture

The Trust Gate is a FastAPI service (`services/trust-gate/`). It is stateful: it
owns a durable store for passports, per-check results, derived findings, and an
append-only audit trail, keyed by `provider_id` and `dataset_id`.

```
provider data (FHIR / OMOP / tabular)
        |
        v
  +-----------------------------+        +---------------------------+
  |  Trust Gate service :8400   | -----> |  store (SQLite | Postgres)|
  |  - assess (engine.py)       |        |  assessments, check_results,
  |  - checks/ + rules.py       |        |  findings, audit_log      |
  |  - cdm/ (OMOP normalize)    |        +---------------------------+
  |  - scoring + label + report |
  +-----------------------------+
        |  optional fail-soft calls
        +--> FHIR validator (HAPI $validate, OperationOutcome)
        +--> terminology server ($validate-code)
```

Consumers:

- **Trust Gate UI** (standalone single-page app, port 8401): paste or submit a
  dataset, watch the QC pipeline run, read the passport and audit report.
- **MedAnon SPA** (`/trust-gate`, `/trust-history`): the same assessment plus
  full-server patient-compartment scans, history/trend, and findings triage.
- **De-identification engine** (anonymizer): the pre-privacy intake gate
  (`pipeline/intake_gate.py`) calls `/v1/trust/assess/batch` before processing.

Persistence uses the two-backend pattern: PostgreSQL when `TRUST_GATE_STORE_DB_URL`
is set (durable, cross-replica, preserves `--scale trust-gate=N`), otherwise
SQLite at `TRUST_GATE_STORE_DB` for single-instance dev. When neither is set the
service is stateless: it still returns a passport, but history, findings, and the
audit trail are not retained. Persistence is best-effort and fail-open: a store
outage never blocks an assessment.

---

## 2. The assessment model

Quality is fitness for a declared purpose (Kahn 2016; Wang and Strong 1996). A
check is meaningful only at its correct scope:

| Scope | Checks | Why |
|---|---|---|
| Per resource | structural, format, terminology, required elements, value present | each record is valid or not, independent of others |
| Per patient | referential integrity, timeline coherence, onset/abatement, before-death | references and clinical logic need the related resources together |
| Dataset-global | value distributions, uniqueness, drift | require pooling across the whole dataset |

For a full-server scan the unit is the **patient compartment**
(`Patient/$everything`): each patient's complete record is assessed together so
relational and temporal checks are scored against real context, while per-resource
conformance is unaffected and distributions and uniqueness are pooled globally.
See section 6.

### Deterministic verdict vs statistical advisory

A QC verdict that a regulator can rely on must be reproducible. The Trust Gate
splits the signal:

- The **decision** (PASS / CONDITIONAL_PASS / BLOCK) derives only from
  deterministic checks (structure, format, terminology, completeness, referential
  and temporal integrity, clinical logic, definitional bounds).
- Statistical checks (`plausibility.value_outlier`, `plausibility.distribution_drift`,
  `plausibility.value_outlier_stratified`) are reported as a clearly labelled
  **advisory** that never moves the gate. They are made reproducible by a seeded
  reservoir sampler (`TRUST_GATE_SAMPLER_SEED`, default 1337) and version-pinned
  baselines, recorded in the passport `evaluation` block.

---

## 3. What it checks

Scores are OHDSI-DQD style: a check FAILs when its violation fraction exceeds its
configured threshold; a category score and the overall score are the percentage
of applicable checks passing. Each check is tagged into a Kahn category
(conformance / completeness / plausibility), a DAMA/ISO 25012 dimension, and an
audit phase.

### 3.1 FHIR checks (built in)

Conformance:

- `conformance.structural`: valid against the base FHIR spec (uses the FHIR
  validator when configured; otherwise the structural subset below).
- `conformance.profile`: conforms to an asserted `meta.profile`.
- `conformance.resource_type_present`, `conformance.resource_id_present`: a
  resource has a `resourceType` and a non-empty `id`.
- `conformance.value_format`: primitives match their FHIR format (ISO-8601
  date/dateTime, id pattern). Reports the offending value.
- `conformance.coding_structure`, `conformance.code_wellformed`: codings have a
  system and a code; codes are well formed.
- `conformance.terminology`: codes validate against their code system (offline
  format check, or a terminology server `$validate-code` when configured).
- `conformance.reference_integrity`: literal references resolve within the
  assessed set. A 100% rate on a batch slice (e.g. a single resource type without
  its referenced Patients or Encounters) is expected, not a sign of a clean dataset
  — the referenced resources exist on the server but were not included. Run a
  full-server scan or `Patient/$everything` to assess reference integrity correctly.
- `conformance.status_not_entered_in_error`: clinical resources are not
  `status=entered-in-error` (FHIR Safety Checklist; these are retracted records).
- `conformance.provenance_present`, `conformance.source_of_truth`: provenance and
  source metadata present.

Completeness:

- `completeness.required_elements`: required elements per resource type are
  populated. Includes `Observation.subject`, which is contextually required for
  secondary use even though it is not a base FHIR SHALL element — an observation
  with no subject reference cannot be attributed to a patient for analysis.
- `completeness.value_or_absent`: an Observation carries a value (top-level
  `value[x]`, a `component[].value[x]`, or a `dataAbsentReason`). Panel
  Observations such as Blood Pressure that carry their results in components are
  not flagged as missing a value.
- `completeness.element_density`: recommended elements are populated above a
  frequency threshold (Kahn completeness richness).

Plausibility (deterministic):

- `plausibility.definitional_bounds`: measurements fall within humanly-possible
  bounds for their code and unit (catches unit errors and gross mis-keying, not
  normal ranges).
- `plausibility.uniqueness`: business identifiers are not duplicated.
- `plausibility.patient_identity_stable`: patient identity is consistent.
- `plausibility.concordance`: cross-field clinical coherence (for example a
  pregnancy observation is not on a male patient).
- `plausibility.currency`, `plausibility.record_lag`: timeliness of recorded data
  (DAMA currency).

Plausibility (statistical, advisory only):

- `plausibility.value_outlier`: extreme outlier vs the per-(code, unit)
  distribution (modified z-score and Tukey IQR).
- `plausibility.value_outlier_stratified`: outlier within the (code, unit, age
  band x sex) stratum.
- `plausibility.distribution_drift`: the value distribution shifted sharply from
  the established baseline.

### 3.2 Plausibility rules (config-driven, `config/checks.yaml`)

Declarative temporal and value rules. Each becomes one check. A `blocker` rule
forces BLOCK on failure. Key rule kinds: `date_after_birth`, `date_before_death`
(with `exempt_codes` for cause-of-death Observations), `date_order`,
`period_order`, `not_in_future`, `value_range`. Examples:
`OBS_EFFECTIVE_AFTER_BIRTH_001`, `OBS_EFFECTIVE_BEFORE_DEATH_001`,
`COND_ONSET_BEFORE_ABATEMENT_001`, `ENC_PERIOD_ORDER_001`,
`PATIENT_BIRTH_NOT_FUTURE_001`, `OBS_HEART_RATE_RANGE_001`.

Vital-sign ranges in the bundled config are wide humanly-possible bounds from
clinical physiology literature, not normal ranges; add site-specific authoritative
ranges as additional rules for your measurement context.

### 3.3 OMOP DQD checks (`config/dqd_checks.yaml`, `src/checks/dqd.py`)

OHDSI Data Quality Dashboard pattern over normalized OMOP tables:

- `dqd.table.{t}.present`: required CDM tables present (REQUIRED_TABLES: `person`).
- `dqd.{table}.{col}.not_null`: NOT NULL on required fields (measureValueCompleteness).
- `dqd.{table}.{col}.fk`: foreign-key integrity between CDM tables.
- `dqd.{table}.{col}.date_format`: date/datetime format conformance.
- `dqd.{table}.{col}.value_range`: data-driven value-range plausibility (reports
  the out-of-range value).
- `dqd.{table}.{col}.completeness`: recommended-field fill rate.

CORE_TABLES: `person`, `observation_period`, `visit_occurrence`,
`condition_occurrence`, `drug_exposure`, `measurement`, `observation`.

No hardcoded universal clinical ranges: OHDSI itself reversed that anti-pattern.
Thresholds are config and env driven and cited.

### 3.4 Clinical evaluation (`src/checks/clinical_eval.py`)

Evaluates the clinical data itself, not only its structure, over the normalized
OMOP model:

- `clinical.measurement_after_birth`: a measurement cannot predate the person's
  birth year (deterministic, drives the verdict).
- `plausibility.value_outlier_stratified`: outliers conditioned on the
  demographic stratum (age band x sex), so a normal pediatric value is not judged
  against an adult pool. Advisory.
- Value distributions per (concept, unit) and per stratum, surfaced in the
  passport profile (see section 9).

---

## 4. Decision policy

```
critical or critical-to-quality check FAILs         -> BLOCK
else overall < PASS_MIN_RATE (90%)                   -> CONDITIONAL_PASS
  or any category < CATEGORY_MIN_RATE (80%)
  or provenance missing
else                                                 -> PASS
```

`PASS_MIN_RATE` and `CATEGORY_MIN_RATE` are tunable (`TRUST_GATE_PASS_MIN_RATE`,
`TRUST_GATE_CATEGORY_MIN_RATE`). Statistical advisory results never change the
decision. RBQM critical-to-quality elements per use case can elevate a non-critical
failure to BLOCK (section 12).

---

## 5. The Quality Passport

`POST /v1/trust/assess*` returns a PHI-safe Quality Passport. Per-resource
violation details record the record, the field path, and a short reason; for the
evaluable defects (format, status, outlier, value range, before-birth) they also
carry an example of the offending value so a provider can act on it. The passport
stores tokens and field names, never names or free text.

Top-level fields:

| Field | Meaning |
|---|---|
| `decision` | PASS / CONDITIONAL_PASS / BLOCK |
| `overall_score` | percent of applicable checks passing, or `null` when no checks were assessed. Never shows 100% for an unassessed dataset. |
| `has_assessed` | `true` when at least one check produced PASS or FAIL; `false` when every check was NA. Dashboards should show "not assessed" rather than a score when `false`. |
| `overall_grade` | letter grade (A/B/C/D/F); absent when `has_assessed` is false |
| `category_scores` | conformance / completeness / plausibility pass-rates; `null` per category when no checks in that category ran |
| `checks[]` | every check: result, applicable, violations, threshold, category, dimension, phase, critical, advisory, recommendation, violation_details[] |
| `scorecard` | per-dimension DAMA/ISO 25012 grades |
| `phases` | per-audit-phase verdicts (score, decision, counts) |
| `blockers[]` | the critical failures that forced a BLOCK |
| `fitness`, `approved_for`, `not_approved_for` | purpose-bound fitness verdict; `not_approved_for` is category-aware: a conformance score below 80% blocks regulated sharing, a completeness gap blocks high-stakes analytics, a plausibility gap blocks clinical modelling |
| `label` | EHDS quality + utility + maturity + FAIR label (section 8) |
| `evaluation` | determinism provenance: decision_basis, sampler_seed, advisory_check_ids, validator_used, terminology_used, lifecycle_stage, org_role |
| `profile` | descriptive data profile incl. clinical value distributions (section 9) |
| `auditability`, `framework_versions`, `report` | provenance, version pins, markdown report |

---

## 6. Source models and ingestion

`source_types` selects the path:

- **FHIR** (`POST /v1/trust/assess`, `/assess/batch`): single resource, list, or
  Bundle. The service flattens Bundles and lists.
- **OMOP** (`POST /v1/trust/assess/omop`): `tables` keyed by CDM table name.
- **Tabular / SQL** (`POST /v1/trust/assess/omop` with `mapping`): source tables
  plus a column-mapping spec (source column to OMOP column), normalized onto OMOP.
- **FHIR to OMOP**: pass `resources` to the OMOP endpoint to normalize FHIR onto
  the CDM and run the DQD checks.

### Patient-compartment full-server scan

The MedAnon SPA full-server scan ingests the whole connection with no cap,
streamed patient by patient. For each Patient it fetches `Patient/$everything`
(the complete record) and assesses each compartment together, keeping a
compartment intact within an assessment batch and deduplicating shared resources
globally by id. Non-patient resources (Organization, Practitioner, Location, and
similar) are swept afterward. This is the clinically correct unit (the person as
the unit of analysis, per OHDSI): references resolve, the patient timeline is
coherent, and value distributions plus referential integrity are pooled globally.
The trade-off is one `$everything` request per patient.

---

## 7. Determinism and reproducibility

- The verdict is computed only from deterministic checks.
- The reservoir sampler is seeded (`TRUST_GATE_SAMPLER_SEED`).
- Baseline, validator, and terminology versions are recorded in
  `framework_versions`.
- A golden dataset yields a byte-identical passport (minus timestamps) across runs
  with external services stubbed and the baseline pinned. A diff is a bug.

Note for local testing: the date rules use FHIRPath. On a Python 3.13 venv,
FHIRPath silently no-ops unless the `typing.io` shim is bootstrapped (the pytest
suite bootstraps it). The Docker image is Python 3.12 where FHIRPath works
natively.

---

## 8. EHDS dataset label

Each passport carries an EHDS Art. 56 style label artifact:

- `quality`: grade, decision, and percent of checks passing.
- `utility`: a fitness tier (high / moderate / low) for the declared use.
- `maturity`: a 1 to 5 data-lifecycle maturity level with its basis.
- `fair`: FAIR interoperable / reusable flags.

---

## 9. Clinical value distributions

For numeric measurements the passport profile carries `observation_value_stats`
(per concept and unit) and `observation_value_stats_stratified` (per concept,
unit, and age-band x sex stratum). Each entry includes count, min, max, mean,
median, standard deviation, and a histogram (bin counts). The UI renders these as
data cards: a histogram, summary statistics, and the demographic breakdown, keyed
by the LOINC code and unit so distinct metrics that share a display label stay
separate. This is the analyst-facing view of what the measured data actually looks
like, and it is the deterministic half of "does the data reflect reality"; the
generative epidemiology validator is the deferred complement.

---

## 10. Findings and PDSA remediation

Each failing deterministic check derives an open **finding** linked to the
passport that raised it. A finding has: id, dataset_id, check_id, severity
(critical / major / minor), status (open / triaged / resolved), root-cause class
(unknown / source_error / etl_error / genuine_biology), owner, and a note. This is
the study/act half of the PDSA loop that the literature requires (Houston DQMF):
the provider triages each finding, assigns a root cause, and tracks it to
resolution across runs.

Endpoints: `GET/POST /v1/findings`, `GET /v1/findings/{id}`,
`POST /v1/findings/{id}/transition`, `GET /v1/datasets/{id}/findings`.

---

## 11. Persistence, history, trend, audit

When a store is configured the service retains every passport and serves:

- `GET /v1/providers/{id}/assessments`: a provider's assessments.
- `GET /v1/datasets/{id}/history`: a dataset's runs over time.
- `GET /v1/datasets/{id}/trend`: the metric-level score trend.
- `GET /v1/assessments/{id}`: a stored passport by id.
- `GET /v1/datasets/{id}/audit`: the append-only ALCOA++ audit trail
  (attributable, contemporaneous, traceable; for 21 CFR Part 11). Each row records
  the decision, source model, provider, generated and recorded timestamps, and the
  assessment id. The trail is append-only by design.

---

## 12. Metric governance

- `GET /v1/metric-catalog`: the documented metric cards, one per built-in check
  (id, level, applicability, citation, doc), from `config/metric_catalog.yaml`.
- `GET /v1/use-cases`: the use-case decision-tree profiles
  (`config/use_case_profiles.yaml`) that map a declared `intended_use` to the
  metric and phase subset that matters for it: `ai_training`, `cohort_discovery`,
  `regulatory_rwe`, `lab_analytics`, `exchange_interoperability`. An unmatched use
  case runs all phases (backward compatible).

`regulatory_rwe` also declares critical-to-quality checks (RBQM): failing these
elevates the verdict to BLOCK for that use.

---

## 13. API reference

Health and ops:

```
GET  /health        liveness
GET  /ready         readiness
GET  /metrics       Prometheus metrics
```

Assessment:

```
POST /v1/trust/assess          single resource / list / Bundle (FHIR)
POST /v1/trust/assess/batch    a list of resources as one batch (FHIR)
POST /v1/trust/assess/omop     OMOP tables, or tabular tables + mapping, or FHIR resources
```

Request fields (shared): `dataset_id`, `provider_id`, `source_types`,
`config_profile`, `provenance`, `phases`, `targets`, `intended_use`, `use_case`,
`reference`, `lifecycle_stage`, `org_role`. OMOP request: `tables`, `mapping`,
`resources`.

Platform reads and workflow:

```
GET  /v1/providers/{id}/assessments
GET  /v1/datasets/{id}/history
GET  /v1/datasets/{id}/trend
GET  /v1/datasets/{id}/audit
GET  /v1/datasets/{id}/findings
GET  /v1/assessments/{id}
GET  /v1/findings            ?dataset_id=&status=
POST /v1/findings
GET  /v1/findings/{id}
POST /v1/findings/{id}/transition   { status, root_cause, owner, note }
GET  /v1/metric-catalog
GET  /v1/use-cases
```

---

## 14. Configuration

Environment variables (defaults in parentheses):

| Var | Default | Purpose |
|---|---|---|
| `TRUST_GATE_SERVICE_URL` | unset | anonymizer-side: URL of the Trust Gate; unset disables the intake gate |
| `TRUST_GATE_MODE` | `warn` | intake-gate enforcement: `warn` / `block` / `off` |
| `TRUST_GATE_PASS_MIN_RATE` | `90` | overall pass cut-off (percent) |
| `TRUST_GATE_CATEGORY_MIN_RATE` | `80` | per-category pass cut-off (percent) |
| `TRUST_GATE_VALIDATOR_URL` | HAPI sidecar | FHIR validator endpoint (OperationOutcome) |
| `TRUST_GATE_VALIDATOR_ENDPOINT` | `/{type}/$validate` | validator path template |
| `TRUST_GATE_TERMINOLOGY_URL` | unset | terminology server for `$validate-code` |
| `TRUST_GATE_STORE_DB_URL` | unset | Postgres DSN for the durable store |
| `TRUST_GATE_STORE_DB` | unset | SQLite path for the single-instance store |
| `TRUST_GATE_SAMPLER_SEED` | `1337` | seed for the reproducible statistical sampler |
| `TRUST_GATE_OUTLIER_MODZ` | `3.5` | modified z-score outlier cut-off |
| `TRUST_GATE_OUTLIER_IQR_K` | `3.0` | Tukey IQR multiplier |
| `TRUST_GATE_OUTLIER_MIN_SAMPLE` | `20` | minimum sample before outlier flagging |
| `TRUST_GATE_DRIFT_MODZ` | `3.5` | drift cut-off |
| `TRUST_GATE_BASELINE_DB_URL`, `TRUST_GATE_BASELINE`, `TRUST_GATE_BASELINE_RESERVOIR` | unset, unset, `500` | value-baseline store for drift/outliers |
| `TRUST_GATE_UI_PORT` | `8401` | standalone UI host port |

Config files (`services/trust-gate/config/`):

- `checks.yaml`: per-check thresholds, concordance rules, and the declarative
  plausibility rules.
- `dqd_checks.yaml`: OMOP value-range and completeness checks.
- `metric_catalog.yaml`: one documentation card per built-in check.
- `use_case_profiles.yaml`: the use-case to phase/threshold decision tree, with
  critical-to-quality lists.

---

## 15. The FHIR validator

`conformance.structural` and `conformance.profile` use a real FHIR validator when
configured. The default is the bundled HAPI sidecar exposing type-level `$validate`
(returns an OperationOutcome). For full implementation-guide and profile
validation, point `TRUST_GATE_VALIDATOR_URL` and `TRUST_GATE_VALIDATOR_ENDPOINT`
at the HL7 / Inferno validator-wrapper. The call is fail-soft: a validator outage
returns NA for these checks and never blocks (and never silently passes), and per
the determinism contract a timing-induced NA does not move the verdict.

---

## 16. Deployment

The Trust Gate is opt-in via the `trust` Docker Compose profile:

```bash
docker compose --profile trust up -d trust-gate trust-gate-ui fhir-validator
```

Services: `trust-gate` (:8400), `trust-gate-ui` (:8401), `fhir-validator` (HAPI
sidecar). To populate history, findings, and the audit trail set a store, for
example `TRUST_GATE_STORE_DB=/tmp/trust_gate.db` (single instance) or
`TRUST_GATE_STORE_DB_URL` to a Postgres DSN (durable, supports
`--scale trust-gate=N`). The main UI's nginx proxies `/trust/` to `trust-gate:8400`;
when the `trust` profile is not running, those calls return 502.

---

## 17. Anonymizer integration (intake gate)

The de-identification engine calls the Trust Gate as a pre-privacy barrier
(`pipeline/intake_gate.py`, `integrations/trust_gate/client.py`):

- `TRUST_GATE_MODE=warn` (default): always assess and attach the passport, never
  block.
- `block`: raise an HTTP 422 when the decision is BLOCK.
- `off`: no-op.

Fail-soft: if the Trust Gate is unreachable the verdict degrades to an advisory
CONDITIONAL_PASS, never a silent PASS and never a hard block on the gate's own
outage. A reusable **trust profile** (selectable phases, sector targets, intended
use, and an additive `use_case`) is forwarded per request.

---

## 18. Testing

```bash
cd services/trust-gate
python3 -m pytest tests/ -q
```

The suite covers the store, the determinism contract, the metric catalog, OMOP and
DQD, clinical evaluation, the EHDS label, critical-to-quality elevation, the audit
trail, findings, and the check correctness regressions (BP-panel component values,
cause-of-death exemption, onset/abatement ordering).

---

## 19. References

- Kahn MG et al. A Harmonized Data Quality Assessment Terminology and Framework.
  eGEMs 2016.
- OHDSI Data Quality Dashboard (DQD).
- ISO/IEC 25012 and 25024; DAMA DMBOK (data-quality dimensions).
- European Health Data Space Regulation, Art. 56 (quality and utility label).
- ICH E6(R3) (risk-based quality management; critical-to-quality).
- FDA Real-World Data / Real-World Evidence; EMA RWD data-quality framework.
- 21 CFR Part 11 and ALCOA++ (audit traceability).
- Houston et al. Data Quality Management Framework (DQMF).
- HL7 FHIR R4; LOINC; UCUM; SNOMED CT; RxNorm.

See also `docs/quality-evaluation-methodology.md` for the detailed methods and the
statistical estimators.
