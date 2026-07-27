# Documentation ownership matrix

Canonical home: `website/`. `docs/` keeps only what has no site equivalent.

Line counts verified 2026-07-27 against the tree after Phase 1.

## Retire from docs/, website page is canonical

| docs/ file | Lines | Canonical website page | Merge verdict |
|---|---|---|---|
| `DEPLOYMENT.md` | 451 | `how-to/deploy-docker.md` | Merged: S3 profile + AI profile subsections (env wiring for `s3`/`ai` compose profiles was entirely absent). Rest is already covered, more granular on the website (per-profile subsections vs one table). |
| `RUNBOOK.md` | 424 | `how-to/operations-runbook.md` | Merged: `MEDANON_HASH_KEY_ID` row + expanded `MEDANON_HASH_KEY` impact text in the Secret rotation table (was silently missing a real config knob). Rest is a verbatim match bar em-dash normalization. |
| `api-reference.md` | 1118 | `reference/api.md` | Merged: new `## 15. Governance & EHDS (TEHDAS2 D7.2)` section (permits, reports, minimise/export/exposure/catalog/synthetic endpoints, connectors & settings) filled a numbering gap (14 -> 16) where it was entirely missing. Verified every endpoint/role against the router source and `api/auth.py` before merging. |
| `architecture.md` | 471 | `explanation/architecture.md` | Merged: (1) corrected the System topology diagram, which was stale and actively contradicted AUTH.2 (claimed source FHIR "accessed only via anon.", used `http://` not `https://`, omitted 4 of 7 proxied nginx routes) - replaced with the accurate diagram + "Where authentication actually happens" callout; (2) new `## Governance & EHDS compliance` section, with two stale paths corrected (`pipeline/governance/permit.py` -> `packages/medanon-core/src/domain/permit.py`, `pipeline/permit_context.py` -> `utils/permit_context.py`) to match the post-medanon-core-extraction layout. |
| `components.md` | 593 | `reference/components/` (5 pages) | Merged: Governance & EHDS module table (permit/disclosure/minimization/healthdcat/tool_registry) into `engine.md`, entirely missing before, with the same path correction as architecture.md. Also fixed `infrastructure.md`'s Networks section, which repeated the debunked "source FHIR accessible only through anonymizer" claim and omitted `ui` from the `source-net` members list. Rest (Docker services, Redis, PostgreSQL, edge/routing) already covered, in places more current on the website (reference-deployment profile table). |
| `connector-integration.md` | 405 | `how-to/integrate-connector.md` | Merged: `## Built-in connectors: configurable input, always-S3 output` section (3 subsections: save source+destination, reference on export job, guarantee S3 delivery) - entirely missing, verified against `api/routers/connectors.py` and `pipeline/jobs/result_publisher.py`. Rest (Patterns A-D) already covered. |
| `data-flow.md` | 525 | `explanation/data-flow.md` | Merged: (1) fixed the Network layout closing paragraph, which repeated the same debunked "reachable only via anonymizer proxy endpoints" claim CLAUDE.md explicitly flags; (2) new `## Governance & EHDS release flow (risk-driven export)` section, verified `permit_scope`, `resolve_active_permit`, `require_admin_for_reversal`, and `staged_worker/_risk.py` all still exist as named. |
| `introduction.md` | 174 | `intro.mdx` | Dropped, consciously. `intro.mdx` is a deliberate Diátaxis-aligned rewrite (mermaid diagram, "author your own rules" framing per the product-framing rule) that supersedes the old structure. The old file's unique sections are covered elsewhere: dev quick-start in `tutorials/getting-started.md`, tech stack in `reference/stack.md`. Its "Key use cases" table is obsolete (references `config_research_pseudonymous.yaml` / `config_gpas.yaml`, both deleted). |
| `policies.md` | 181 | `explanation/policies.md` | Dropped. Full line-by-line diff found no unique content beyond em-dash/en-dash normalization. Note: both files equally still document the 4 deleted profiles (`config_gpas.yaml`, `config_research_pseudonymous.yaml`, `config_structure_preserving.yaml`) - pre-existing website staleness, not something lost by this deletion; flagged for a follow-up pass, not fixed here (out of scope for a content-merge task). |
| `result-example.md` | 339 | `reference/result-examples.md` | Merged: `## NLP Scrubbing Example` section (entity-detection table + token substitution walkthrough, verified against `integrations/nlp/utils.py` token format and `services/nlp/src/recognizers.py` entity types) + one sentence on why the manifest tag is stripped before FHIR upload (`tag_display varchar(200)`, verified against `integrations/fhir/writer.py`). Dropped the three Profile 1/2/3 examples as obsolete: they use `config_gpas.yaml` and other deleted profiles and are superseded by the website's own Resource 1/2/3 examples. |
| `scoring-system.md` | 284 | `explanation/scoring-system.md` | Dropped. Full line-by-line diff confirms the website version is a superset (adds the entire Output Gate section: hard PII leak gate, composite score gate, feedback-on-failure, plus more env vars). No unique content in the retiring file. |
| `security.md` | 491 | `explanation/security-model.md` | Merged (highest priority): full `### 1.4 Edge routes that bypass authentication` section (AUTH.2), verbatim from docs/, renumbering the website's existing `1.4 Per-client API keys` -> `1.5` and `1.5 RBAC` -> `1.6`. Confirmed via full top-to-bottom read of both files that this was the only real content gap; everything else in the website version is equal or already expanded (JWKS steps, downgrade guard, SPA token handling, per-client API keys are all website-only additions, superset not gap). |
| `user-manual.md` | 480 | `how-to/use-the-web-ui.md` | Dropped. Full line-by-line diff found zero unique content; purely em-dash/en-dash normalization. One line (AI config-generation description) is actually more accurate on the website (drops the stale "7 bundled profiles" claim). |
| `TECHNICAL_DOCUMENTATION.md` | 1212 | superseded by the site as a whole | Dropped, confirmed. Spot-checked the Dependencies and Data Model sections: the Data Model section claims the app-db schema is created from `services/anonymizer/sql/init.sql`, which no longer exists (schema is created in code by each store's `ensure_schema()`, per CLAUDE.md). The file predates the medanon-core extraction and is not safe to merge from; its accurate portions are already covered by `architecture.md`, `reference/api.md`, `security-model.md`, `deploy-docker.md`. |

`overview.md` (222 lines) was deleted in Task 1.2. It duplicated `intro.mdx`
and was never tracked in git.

Total retiring: 6748 lines across 14 files.

## Keep in docs/

| Path | Reason |
|---|---|
| `INDEX.md` | Rewritten as a thin contributor index pointing at the site |
| `reference/env-vars.md` | Generated by `check_env.py`, CI-gated, not site content |
| `internal/` | Gitignored review ledgers and roadmaps |
| `superpowers/` | Specs and plans |
| `trust-gate/` (7 files) | The site carries only one Trust Gate how-to page; this deep set has no equivalent |
| `quality-evaluation-methodology.md` | Trust Gate measurement methodology, no site equivalent |

## Rule

No topic may live in both trees. A `docs/` file is deleted only after its
unique content is confirmed present in, or merged into, its canonical page.
