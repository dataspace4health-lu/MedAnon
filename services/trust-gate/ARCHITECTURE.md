# Trust Gate architecture

Trust Gate is a pre-privacy clinical data-quality barrier. It ingests FHIR (or OMOP
CDM), runs a suite of data-quality checks, and returns a graded, purpose-bound
"Quality Passport" verdict (PASS / CONDITIONAL_PASS / BLOCK). It never mutates data
and never blocks on its own outage (checks that cannot run become NA, not a false
PASS).

## Layers

The code is organized by responsibility. Dependencies point downward only; nothing
imports `engine`, and the domain model imports nothing from the layers above it.

```
 api/            HTTP surface: FastAPI app, request/response schemas, routers.
   │             (main.py assembles `app`; the container runs `uvicorn main:app`.)
   ▼
 engine.py       Orchestration: assess() / assess_omop() run the pipeline:
   │               run checks → split deterministic vs advisory → score →
   │               decide → coverage → fitness → assemble the QualityPassport.
   ▼
 verdict/        The verdict layer (pure functions over the check list):
   │  context      AssessmentContext  the immutable inputs (Parameter Object)
   │  registry     the FHIR check suite as a list of strategies (Strategy pattern)
   │  runner       iterates the registry + per-sector / per-phase sub-reports
   │  scoring      category/overall roll-up, dimension scorecard, grades, advisory
   │  decision     the PASS/CONDITIONAL/BLOCK policy + per-resource-type thresholds
   │  coverage     assessment-coverage transparency (what actually ran)
   │  fitness      purpose-bound fitness-for-use lists + statement
   ▼
 checks/         One measurement concern per module (Kahn 2016 categories):
   │  conformance/  package: presence, validation, terminology, references, _shared
   │  completeness, plausibility, timeliness, accuracy, identity, governance,
   │  code_systems, clinical_eval (OMOP), dqd (OMOP)
   ▼
 domain          passport.py  CheckResult, CategoryResult, QualityPassport, vocab.
                 dimensions.py (DAMA/ISO), phases.py (selectable suites), constants.py.
                 Data only; no I/O, no rendering, no policy.

 reporting/      Renders a QualityPassport (markdown). Depends on the model, never
                 the reverse (passport.to_markdown delegates here via a lazy import).

 infra           validator_client, terminology_client, breaker (external FHIR
                 validator + terminology server, with circuit breakers + timeouts).
 store/          passport + findings persistence (SQLite/Postgres).
 connectors/     file + SQL ingestion. cdm/ FHIR→OMOP + OMOP model.
```

## Design decisions

- **Measurement vs policy are separate.** `scoring` computes Kahn/OHDSI-DQD pass-rates
  (the % of applicable checks passing, no domain weights). `decision` applies the
  product fitness-for-use policy. Changing "what is good enough" never touches "how
  we measure."
- **One shared decision.** The headline verdict and every sub-verdict (per sector,
  per phase) go through `decision.decide` / `subset_decision`, so a sector badge can
  never read more leniently than the overall verdict.
- **Determinism.** The verdict is computed only from deterministic checks; statistical
  (outlier/drift) checks are fenced into an advisory block and never move the verdict.
  Given the same bytes (and provenance), the passport is reproducible.
- **NA over false-PASS.** Any check whose prerequisite is missing (no validator, no
  terminology server, a timeout) is NA and excluded from scoring, never counted as a pass.
- **Honesty.** `coverage` reports how much of the suite ran (assessed/total, descoped
  phases, verification-vs-validation), so a grade over a thin subset cannot read as a
  comprehensive, externally-validated certification.

## Key patterns

- **Parameter Object** (`verdict/context.py`): every check input travels in one
  immutable `AssessmentContext`; a per-sector re-run is `ctx.scoped_to(subset)`
  instead of re-threading a dozen arguments.
- **Strategy + Registry** (`verdict/registry.py`): each check is a strategy
  `ctx -> list[CheckResult]`; `runner.run_checks` iterates the registry, so the
  orchestrator does not know about individual checks.

## Adding a new check

1. Implement it in a `checks/` module returning a `CheckResult` (or extend the
   relevant `conformance/` submodule). Set `check_id`, `category`, `context`
   (verification | validation), `critical`, and a threshold via `threshold_for`.
2. Register a one-line adapter for it in `verdict/registry.CHECK_REGISTRY` (the one
   place that defines the suite), reading its inputs from the `AssessmentContext`.
3. Tag it: add the `check_id` to `phases.py` (`_BUILTIN_PHASE`) and `dimensions.py`
   (`_BUILTIN_DIM`), and register determinism in `constants.py` if it is advisory.
4. Add a test under `tests/`. Run `python3 -m pytest tests/ -q` and `ruff check src`.
