# MedAnon: A Rule-Driven, Self-Measuring Engine for De-identifying and Pseudonymizing FHIR Health Data

*Technical article — Health Data Preparation & Privacy Toolkit (MedAnon)*

---

## Abstract

Electronic Health Records carry enormous secondary value for clinical research, epidemiology, and AI, yet data-protection regulations block their free exchange because they contain patient identifiers. De-identification is the key to unlocking this value safely — but the regulations say *what* must be protected, not *how*, leaving every implementation to bridge a gap between abstract legal obligation and concrete engineering. **MedAnon** bridges that gap with a rule-driven de-identification engine for FHIR health data. Privacy decisions are expressed as human-readable **selection-action rules** that legal and engineering staff author together; the engine executes them through a layered set of techniques — redaction, generalization, keyed hashing, perturbation, encryption, reversible pseudonymization via a Trusted Third Party, k-anonymity, and fail-closed NLP scrubbing of free text. Crucially, MedAnon does not merely transform data and assert it is safe: a built-in **scoring engine measures** every output along privacy, utility, and quality dimensions and **blocks release** when the residual re-identification risk exceeds a configured threshold. The system runs entirely on the data controller's own premises as a set of cooperating containers, ingests FHIR as well as DICOM, HL7 v2, CDA, and relational sources, and offers locally-run AI assistance that never sends protected data to an external model. This article presents the problem, the solution and its architecture, the full capability surface, the pipeline, the scoring mathematics, and reproducible performance measurements.

**Keywords:** de-identification, pseudonymization, FHIR, FHIRPath, k-anonymity, l-diversity, t-closeness, HIPAA, GDPR, clinical NLP, privacy scoring, Trusted Third Party.

---

## I. The Problem

### A. The regulatory gap

Regulatory frameworks such as the US HIPAA Privacy Rule and the EU General Data Protection Regulation (GDPR) govern how patient identifiers must be handled, but they operate at very different levels of concreteness.

HIPAA defines two procedures: *Expert Determination* (a qualified expert assesses residual re-identification risk using accepted statistical methods) and *Safe Harbor* (removal or generalization of 18 specific identifier categories listed in 45 CFR §164.514(b)). GDPR is deliberately less prescriptive: it recognizes pseudonymization as a risk-reducing measure (Art. 4(5), 25, 32) and, in Recital 26, exempts truly anonymous data from the regulation entirely — but it gives abstract guidance rather than operative steps, delegating the specifics to national regulators and organizational risk assessments.

The result is a gap. The law states *what* outcome is required but not *how* to achieve it. Deciding which fields to transform, by which technique, and to what degree is left to each implementation — and getting it wrong exposes patients to real harm: discrimination in employment or insurance, blackmail, and stigmatization. **Closing this operative gap is the problem MedAnon exists to solve.**

### B. Why de-identification is hard

Translating a regulatory outcome into a correct implementation is non-trivial:

- **Direct identifiers are not the whole story.** Alongside names and IDs sit **quasi-identifiers** — birth date, postal code, gender, diagnosis codes — that are not unique alone but, correlated with external datasets, can re-link a record to a person. Robust de-anonymization of "anonymized" data is a demonstrated threat, not a hypothetical one [Narayanan & Shmatikov 2008; Sweeney 2002].
- **The FHIR data model is rich.** A FHIR resource carries internal resource IDs (used for referential integrity across a Bundle), external patient identifiers (MRN, national ID), coded clinical values, and free-text narratives. Internal IDs must be transformed *consistently* so references survive; external ones must be redacted or pseudonymized by role.
- **Free text is half the surface.** A large fraction of clinical knowledge lives in discharge summaries, radiology reports, and nursing notes. Transforming structured fields alone leaves narrative PHI wide open.
- **The privacy–utility trade-off is real.** Over-scrubbing destroys analytical value: removing geography hides health disparities, perturbing dates breaks survival analyses. Protection and utility must be balanced, not maximized one at a time.
- **Pseudonymization has subtle pitfalls.** A field from a small domain (a date, a postal code) can be brute-forced by hashing the entire domain. A constant IV in AES-CBC leaks repeated values. International guidance (ENISA; ISO 25237) specifies properties a correct implementation must satisfy.

### C. Why existing approaches fall short

Existing tools each cover part of the surface. NLP-only scrubbers (e.g. Presidio-based pipelines, Philter) handle free text well but ignore structured-field transformation, referential integrity, and policy governance. Fixed-field FHIR anonymizers cover structured data but not narrative, offer no Trusted-Third-Party pseudonymization, and — critically — **none of them measure their own output**: they transform data and assert it is de-identified, without quantifying the residual risk or refusing to release output that fails. MedAnon's contribution is to **unify all of these layers behind one auditable rule language and gate every result through a measurable privacy threshold.**

---

## II. The Solution — Overview and Added Value

### A. What MedAnon is

MedAnon is a **rule-driven, self-measuring de-identification engine** for health data. Three ideas define it:

1. **Policy as readable rules.** What to de-identify and how is expressed as selection-action rules in human-readable YAML — a shared language a Data Protection Officer and an engineer can author and review together, instead of code only one of them understands.
2. **A layered technique portfolio.** One pipeline applies suppression, generalization, keyed hashing, perturbation, encryption, reversible TTP pseudonymization, k-anonymity, and NLP free-text scrubbing — covering direct identifiers, quasi-identifiers, and unstructured PHI together.
3. **Privacy as a measured gate, not a claim.** Every output is scored, and release is **blocked** when the residual re-identification risk exceeds a configured threshold. "De-identified" becomes a property the system *verifies*, not one it merely asserts.

### B. Added value

| Property | What it gives the user |
|---|---|
| **Auditable** | Policy is a readable, versionable YAML file that legal and IT staff jointly review and sign off. |
| **Measured** | Every output carries a privacy / utility / quality score; release is blocked if the privacy threshold is not met. |
| **Reversible where the law allows** | TTP pseudonymization supports controlled re-linkage; cryptographic hashing is used where irreversibility is required. |
| **Comprehensive** | Layered techniques cover structured fields, quasi-identifiers, and free-text narrative — plus DICOM, HL7 v2, CDA, and relational sources — in one governed pipeline. |
| **Privacy-safe AI** | AI lowers the authoring barrier without exposing patient data; every AI feature degrades gracefully when disabled. |
| **Standards-aligned** | FHIRPath selection, FHIR referential integrity, HIPAA/GDPR mapping, gPAS TTP, ISO 25237, ENISA guidance. |
| **On-premises** | All components run in containers; no cloud dependency; patient data never leaves the environment. |

### C. Design rationale — why this shape

Three deliberate design choices follow from the problem:

- **Rules as the legal/IT shared language.** De-identification decisions are legal and ethical, not purely technical. A readable rule format lets the people accountable for compliance author and audit the policy directly, rather than delegating it to whoever can read the code.
- **Separation of concerns + physical data isolation.** The system is a set of cooperating services, not a monolith, so each part can be secured and scaled independently — and identified data is kept on an isolated network, physically separated from de-identified output, as both HIPAA and GDPR require.
- **Privacy as a hard gate.** Because regulations demand an *outcome*, the engine measures the outcome and refuses to emit output that fails. This is what turns a transformation tool into a compliance tool.

---

## III. The Components — Service Catalogue

MedAnon runs as a set of cooperating containers. The diagram shows the core data path; the table below names every service in the stack.

**Figure 1 — Service topology**

```
                  ┌─────────────────────────┐
                  │   Privacy Workbench      │  legal + IT experts:
                  │   (browser SPA)          │  author rules, submit jobs,
                  │                          │  review scores and audit logs
                  └───────────┬─────────────┘
                              │ HTTPS / REST
                              ▼
        ┌──────────────────────────────────────────────────────────┐
        │               MedAnon Gateway (nginx)                    │
        └──────────┬───────────────────────┬────────────────────────┘
                   │                       │
                   ▼                       ▼
   ┌─────────────────────────────────────────────────────────┐
   │                Privacy Engine (FastAPI)                  │
   │                                                          │
   │  ┌─────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
   │  │ MATCH   │→ │  ACTION  │→ │ FINALIZE │→ │ MEASURE  │  │
   │  │FHIRPath │  │transform │  │ rewrite  │  │& GATE    │  │
   │  │rule idx │  │dispatch  │  │ refs/IDs │  │(RiskLens)│  │
   │  └─────────┘  └───┬┬─────┘  └──────────┘  └──────────┘  │
   └───────────────────┼┼────────────────────────────────────┘
                       ││
          ┌────────────┘└─────────────────┐
          ▼                               ▼
   ┌──────────────┐               ┌──────────────┐
   │  PseudoVault │               │ PHI Sentinel │
   │  (gPAS TTP)  │               │ (Presidio +  │
   │              │               │  spaCy NER)  │
   └──────────────┘               └──────────────┘

   ┌─────────────────┐  ┌──────────────────┐  ┌────────────────────┐
   │ Clinical Source │  │ Protected Archive│  │ Supporting infra   │
   │ (FHIR — isolated│  │ (FHIR — de-id'd) │  │ DataFlow Worker ·  │
   │   network)      │  │                  │  │ cache · auth · logs│
   └─────────────────┘  └──────────────────┘  └────────────────────┘
```

**Core services**

| Service | Role | Why it is a separate service |
|---|---|---|
| **Privacy Workbench** | React SPA; rule authoring, job monitoring, analytics dashboards, audit review | Gives non-engineers a UI; can be locked down or omitted independently |
| **MedAnon Gateway** | nginx reverse proxy; routes `/api/*` → Privacy Engine, `/fhir/*` → Clinical Source, `/fhir-target/*` → Protected Archive | Single TLS edge; keeps source and target FHIR servers off the public surface |
| **Privacy Engine** | FastAPI service; runs the 4-stage pipeline; REST API + CLI | The core; stateless, so it scales horizontally |
| **PseudoVault** | Trusted Third Party pseudonymization registry (gPAS); reversible identifier → pseudonym mapping | Keeps the sensitive linkage table *outside* the processing environment |
| **PHI Sentinel** | Presidio + spaCy NER; detects & replaces PHI in free text; fail-closed | Heavy ML deps isolated; scaled independently of the engine |
| **RiskLens** | Privacy × utility × quality scoring; the hard release gate | Scoring can run in-process or as its own service under load |
| **Policy Advisor** | Local LLM (Ollama); config generation, PII discovery, rule explanation, compliance analysis | Optional, local-only; never on the critical path |
| **DataFlow Worker** | Background executor for bulk/async jobs; checkpoint/resume | Long jobs run off the request path; can scale to many replicas |
| **Clinical Source** | HAPI FHIR server holding identified data; isolated network, no host port | Physical isolation of identified data |
| **Protected Archive** | Separate HAPI FHIR server receiving only post-gate de-identified data | Physical separation of de-identified output |

**Supporting infrastructure**

| Component | Role |
|---|---|
| **Shared cache** (Redis) | gPAS pseudonym L2 cache, NLP detection cache, job queue |
| **Application database** (PostgreSQL) | Jobs, config profiles, subscriptions, processing-run history, staging, API keys |
| **Auth provider** | Pluggable: single API key, per-client API keys, or OIDC (Keycloak / Azure AD) |
| **Message bus** (RabbitMQ, opt-in) | Macro-stage work streaming for DAG workflows |
| **Object storage** (MinIO/S3, opt-in) | Result NDJSON storage backend (alternative to filesystem) |
| **Monitoring** (Prometheus / Grafana / Jaeger, opt-in) | Metrics, dashboards, distributed tracing |
| **Analytics service** | k-anonymity / l-diversity risk assessment and synthetic-data generation |

The whole stack deploys via Docker Compose or Helm (Kubernetes), on-premises or in any cloud.

### How the supporting services work

The Privacy Engine is the conductor, but most of the privacy work is done by services it calls out to. Each is a small, independently deployable unit with one responsibility, so it can be scaled, secured, or replaced on its own.

**PHI Sentinel** is the free-text de-identifier. The engine sends it the narrative fragments it has extracted from a batch of resources in a single call; PHI Sentinel runs named-entity recognition (Presidio with spaCy models) to locate person names, dates, locations, contact details, and identifiers, and returns the spans to replace. Two design choices matter. First, it is **batched** — one network call covers many resources — which is what lets the detection stage run concurrently with pseudonymization without a per-resource round-trip. Second, it is **fail-closed**: if the service is unreachable or errors, the engine substitutes a placeholder and withholds the text rather than emitting it unscrubbed, so a service outage can never leak narrative PHI. A detection cache (in-process plus an optional shared layer) absorbs the cold-start cost of repeated text.

```
  Privacy Engine                  PHI Sentinel
  ┌───────────────┐  texts[]      ┌─────────────────────┐
  │ extract free  │ ────────────► │ cache hit? ─► spans  │
  │ text spans    │  (1 batched   │     │ miss            │
  │               │     call)     │     ▼                │
  │               │               │ Presidio + spaCy NER │
  │ replace spans │ ◄──────────── │ → PHI spans          │
  └──────┬────────┘    spans[]    └─────────────────────┘
         │ service down / error
         ▼
   [ withhold text ]   ◄── fail-closed: never emit unscrubbed
```

**PseudoVault** is the reversible-pseudonymization registry, an adapter over the gPAS Trusted Third Party. The engine asks it to map a batch of original identifiers to pseudonyms; PseudoVault stores the mapping and returns stable pseudonyms, so the same input always yields the same pseudonym across runs and across sites. The mapping lives **outside** the processing environment, which is the whole point: re-identification requires controlled, audited access to PseudoVault, not access to the de-identified dataset. The engine guards the call with a circuit breaker and a concurrency bulkhead, and caches results, so a slow or saturated TTP degrades gracefully instead of stalling a bulk job.

```
  Privacy Engine                         PseudoVault (gPAS TTP)
  ┌────────────────┐  ids[]              ┌──────────────────────┐
  │ collect IDs    │ ─────────────────►  │  mapping store        │
  │                │  (circuit-breaker   │  id ⇄ pseudonym       │
  │ apply          │   + bulkhead        │  (stable, audited)    │
  │ pseudonyms     │ ◄─────────────────  │                       │
  └────────────────┘   pseudonyms[]      └──────────────────────┘
        ▲  cache                                   ▲
        └─ LRU / shared cache                      └─ controlled
           (skip repeat lookups)                      re-identification
                                                      (separate access)
```

**RiskLens** is the scoring and gating service. After a resource is transformed, it computes the privacy, utility, and quality scores described in Section VI and returns a release decision. It can run in-process for low latency or as its own service under heavy load; either way it is the component that turns "de-identified" into a measured, enforceable property.

```
   de-identified resource
            │
            ▼
  ┌──────────────────── RiskLens ────────────────────┐
  │  privacy R ──┐                                    │
  │  utility U ──┼─►  R > threshold ? ── yes ─► BLOCK  │
  │  quality Q ──┘          │ no                       │
  │                         ▼                          │
  │            composite = (1−R/thr)·U·Q·100  ─► PASS  │
  └───────────────────────────────────────────────────┘
            │ PASS                       │ BLOCK
            ▼                            ▼
        released                    quarantined
```

**The Analytics service** answers dataset-level questions the per-record pipeline cannot. It assesses re-identification risk across a whole cohort using k-anonymity and l-diversity, and it generates **synthetic data** — statistically similar records carrying no real patient information — for teams that need realistic test data without touching protected data at all.

```
                  ┌─────────────── Analytics ───────────────┐
   cohort  ─────► │  risk assessment:  k-anonymity / l-div   │ ─► risk report
                  │                                          │
   (distributions)│  synthetic generation:                   │ ─► synthetic
                  │  learn distributions → emit fake records  │     dataset
                  └──────────────────────────────────────────┘
                         no real patient data leaves
```

**Policy Advisor** is the optional AI layer. It runs against a local language model and assists the human authors: drafting a rule profile from a plain-language goal, suggesting which fields in a sample look identifying, explaining what a rule does, checking the output for residual PII as a second pass, and flagging compliance gaps. It only ever *proposes*; the deterministic rules and RiskLens make the binding decisions, and every Policy Advisor feature falls back to a non-AI alternative when the model is unavailable.

```
   human author                Policy Advisor            local LLM
   ┌───────────┐  goal / sample ┌────────────────┐  prompt ┌─────────┐
   │ "share EU │ ─────────────► │ draft rules    │ ──────► │ Ollama  │
   │  cohort"  │                │ suggest fields │ ◄────── │ (on-prem)│
   │           │ ◄───────────── │ explain / check│  text   └─────────┘
   └─────┬─────┘   proposals    └───────┬────────┘
         │ review & accept              │ model unavailable
         ▼                              ▼
   deterministic rules           deterministic fallback
   + RiskLens decide             (keyword / static checklist)
```
*Proposals only — the deterministic pipeline always makes the binding decision; no patient data leaves for an external model.*

**DataFlow Worker** carries the long jobs. When a bulk export is submitted, a worker claims it, fetches resources from the source server in pages, and feeds them through the identical four-stage pipeline in shards, checkpointing as it goes. Workers are stateless and coordinate through the shared job queue, so several can run in parallel and any one can crash and resume without losing or duplicating work.

```
   submit job ─►  ┌──────── shared job queue ────────┐
                  └──────┬───────────┬───────────────┘
                  claim  │           │  claim
                         ▼           ▼
                  ┌───────────┐ ┌───────────┐   (stateless,
                  │ Worker A  │ │ Worker B  │    run in parallel)
                  │ fetch →   │ │ fetch →   │
                  │ shard →   │ │ shard →   │ ─► 4-stage pipeline
                  │ checkpoint│ │ checkpoint│
                  └─────┬─────┘ └───────────┘
                        │ crash → resume from last checkpoint
                        ▼
                  no work lost or duplicated
```

---

## IV. Capabilities — Feature Catalogue

MedAnon is more than a FHIR scrubber. This section catalogues the full capability surface at a high level; the pipeline and scoring mathematics are detailed in Sections V and VI.

### A. Ingestion and formats

The engine is **not FHIR-only**. It accepts:

| Source | Detail |
|---|---|
| **FHIR** | JSON, NDJSON, XML — single resource, Bundle, or streamed NDJSON |
| **DICOM** | Imaging metadata de-identification |
| **HL7 v2** | Message de-identification (pipe-delimited segments) |
| **CDA / CCDA** | Clinical Document Architecture de-identification |
| **Relational / SQL** | Connect to a database, reflect the schema, preview, and export de-identified rows (host allow-list + statement timeouts) |
| **Tabular** | CSV, Parquet, and JSON tables |

### B. Processing modes

The engine serves both interactive and server-to-server work:

| Mode | Use |
|---|---|
| Single / raw | One resource or Bundle, de-identified synchronously |
| NDJSON stream | Line-by-line, one resource at a time, for large files |
| Batch | A list of resources processed together, with cross-resource pseudonym batching |
| From server | Pull resources directly from a FHIR server, de-identify, and return them |
| Round-trip / upload | Fetch from the source server, de-identify, and write the result to the target server |

### C. Bulk and asynchronous work

| Feature | Detail |
|---|---|
| **Async jobs** | Long operations (bulk export, cohort export, patient export) are accepted immediately and run in the background; the caller polls for status and downloads the result when ready |
| **DAG workflows** | Multi-stage workflows streamed over a message bus, with dead-letter and retry queues |
| **Checkpoint / resume** | Jobs checkpoint after each shard; a worker restart resumes from the last completed shard rather than restarting the export |
| **Two-phase staging** | For k-anonymity, resources are staged to the database, then partitions are claimed and processed in parallel |
| **Durable backends** | The job queue runs on Redis Streams, PostgreSQL, or SQLite, selected by configuration |

### D. The action portfolio

Eleven transformation actions, one rule each. Reversibility and purpose:

| Action | Reversible? | Purpose |
|---|---|---|
| **keep** | n/a | Pass a non-sensitive field through unchanged |
| **redact** | No | Replace with a fixed placeholder |
| **substitute** | No | Replace with a constant value |
| **perturb** | No | Add calibrated random noise (numeric quasi-identifiers) |
| **mask** | No | Partial masking (e.g. last-4 of an SSN) — 5 strategies |
| **generalize** | No | Reduce precision (date → year, ZIP → 3-digit, age → band) |
| **cryptohash** | No | HMAC-SHA3-256 keyed digest; consistent token, brute-force resistant |
| **date_shift** | No | Deterministic per-subject offset; keeps a patient's timeline gaps intact |
| **scrub_text** | No | Route free text through PHI Sentinel (NLP) |
| **tokenize** | Yes* | Format-preserving token |
| **encrypt** / **decrypt** | Yes | RSA-OAEP / AES-GCM ciphertext; reversible with the key |
| **map** (TTP) | Yes | Pseudonym from PseudoVault; reversible only under controlled access |

\*reversible via the token registry.

### E. Privacy techniques (enhanced anonymity)

| Technique | What it adds |
|---|---|
| **Generalization** | Preserves analytical value while removing re-identifying precision |
| **k-anonymity** | OLA-style lattice solver generalizes quasi-identifiers across the dataset until every record hides among ≥ *k − 1* others, with minimal information loss |
| **l-diversity / t-closeness** | Supplementary checks that sensitive attributes are diverse within each equivalence class |
| **NLP scrubbing** | Named-entity detection over free text; **fail-closed** — text is withheld if detection cannot run |
| **TTP pseudonymization** | Reversible mapping held by an external trusted service, not inside the processing environment |

### F. Governance and operations

| Feature | Detail |
|---|---|
| **Config-profile CRUD** | Create / version / validate rule profiles; a schema validator reports errors before a profile is applied |
| **Processing-run history** | Every run recorded; list, stats, get, purge |
| **Audit log + query API** | Centralized audit trail (file plus an optional event stream), queryable for recent events |
| **RBAC** | `admin` / `analyst` / `viewer` roles, including parameterized paths |
| **Pluggable auth** | Open mode, single API key, per-client API keys, or OIDC (Keycloak / Azure AD) — env-selected |
| **Resilience** | Circuit breakers and per-upstream bulkheads on gPAS, NLP, FHIR; fast-fail when saturated |
| **Hardening** | Body-size cap, rate limiting, SSRF guard, path-traversal protection, read-only containers |
| **Result storage** | Filesystem or S3-compatible object storage, with TTL-based cleanup |
| **Observability** | Prometheus metrics throughout; Grafana dashboards; optional Jaeger tracing |

### G. Analytics and AI

| Service | Capability |
|---|---|
| **Analytics** | Re-identification risk assessment (k-anonymity, l-diversity); synthetic-data generation |
| **Policy Advisor (AI)** | (1) generate a config from a natural-language goal; (2) interactive rule refinement; (3) field/PII discovery from a sample; (4) second-pass residual-PII check; (5) plain-language rule explanation; (6) compliance gap analysis. All run against a **local** model; every feature has a deterministic fallback. |

---

## V. How It Works — The Pipeline

### A. The rule language

Policy is expressed as **selection-action rules** in YAML:

```yaml
rules:
  - name: "pseudonymize patient MRN"
    match: "Patient.identifier.where(system='http://hospital.org/mrn').value"
    action: "map"           # reversible TTP pseudonymization
    priority: 10

  - name: "generalize birth date to year"
    match: "Patient.birthDate"
    action: "generalize"
    params: { precision: "year" }

  - name: "scrub narrative text"
    match: "*.text.div"
    action: "scrub_text"    # NLP-backed
```

**Selection** uses **FHIRPath**, HL7's standard expression language for navigating FHIR structures, with where-clauses, type qualifiers, and wildcard traversal. Using the official standard guarantees correct semantics across FHIR releases. Rules are ordered by `priority` (lower number wins); the first match per field decides the action. Eight bundled profiles exist as starting points; users compose their own by adding or overriding rules.

### B. The four-stage pipeline

**Figure 2 — De-identification pipeline**

```
 ┌────────────────┐
 │     INPUT      │  FHIR resource (JSON / NDJSON / XML)
 └───────┬────────┘
         ▼
 ┌───────────────────────────────────────────────────────┐
 │  STAGE 1 — MATCH                                      │
 │  Load profile → compile FHIRPath → per-resource index │
 │  Emit field→action work items + NLP work items        │
 └───────┬───────────────────────────────────────────────┘
         ▼
 ┌─────────────────────────────────────┐  ┌──────────────────────────────────┐
 │  STAGE 2a — PSEUDONYMIZE            │  │  STAGE 2b — PHI DETECTION        │
 │  Batch identifiers → PseudoVault    │  │  Batch narrative → PHI Sentinel  │
 │  Circuit-breaker + bulkhead guard   │  │  Named-entity recognition        │
 │  Cache pseudonym results (LRU)      │  │  Replace detected spans          │
 └─────────────────────────────────────┘  └──────────────────────────────────┘
         │  (run CONCURRENTLY — disjoint field sets)      │
         └──────────────────────┬─────────────────────────┘
                                ▼
 ┌───────────────────────────────────────────────────────┐
 │  STAGE 3 — FINALIZE                                   │
 │  Apply remaining actions (redact, hash, generalize…)  │
 │  Rewrite Bundle references + text-embedded IDs        │
 │  Validate output structure                            │
 └───────┬───────────────────────────────────────────────┘
         ▼
 ┌───────────────────────────────────────────────────────┐
 │  STAGE 4 — MEASURE & GATE  (RiskLens)                 │
 │  Privacy · utility · quality scores                   │
 │  PRIVACY IS A HARD GATE:                              │
 │  risk > threshold → BLOCKED, quarantined, not emitted │
 └───────┬───────────────────────────────────────────────┘
         │ pass
         ▼
 ┌────────────────┐
 │    OUTPUT      │  de-identified FHIR → target / export
 └────────────────┘
```

- **Stage 1 (Match).** The profile's FHIRPath expressions are compiled and cached (ANTLR parse is expensive; an LRU cache amortizes it across a batch). A per-resource rule index maps each matched field to its action.
- **Stages 2a + 2b run concurrently.** PseudoVault (pseudonymization) and PHI Sentinel (NLP) touch **disjoint** field sets, so they are scheduled as concurrent async tasks. On a 1,000-resource batch this roughly halves the combined I/O-bound latency.
- **Stage 3 (Finalize).** Remaining transformations are applied in-process; Bundle references and text-embedded IDs are rewritten so relationships survive.
- **Stage 4 (Measure & Gate).** RiskLens scores the output; if the privacy risk exceeds the threshold, the result is quarantined rather than released.

### C. Technique rationale — why these choices

- **Keyed hashing, not plain hashing.** A plain SHA-256 of a small-domain field (a postal code, a date) is brute-forced by hashing the whole domain. MedAnon uses a **keyed HMAC** so recovery needs the secret key, injected as a deployment secret and never stored with the data.
- **Deterministic date shifting.** Longitudinal data requires that the gap between admission and discharge survive even when absolute dates change. A per-subject offset derived from the patient pseudonym shifts all of a patient's dates equally, with no per-patient offset table to leak.
- **Fail-closed NLP.** If PHI Sentinel is unavailable, the text field is **withheld**, never released unscrubbed — narrative PHI cannot leak by being silently skipped.
- **TTP over local mapping.** Holding the linkage table inside the processing environment recreates the very risk being managed. Delegating it to PseudoVault keeps re-identification possible only under controlled, audited access.
- **Referential integrity as a first-class constraint.** Internal IDs are rewritten consistently, coded fields keep valid codes, and the output passes FHIR validation — because de-identified data that downstream systems reject has no value.

### D. Bulk, streaming, and staging

A single resource and a million-record cohort follow the **same** four stages. For bulk work, the client submits a job and a DataFlow Worker fetches resources in paginated batches and processes them in shards through the identical pipeline, checkpointing after each shard. k-anonymity additionally uses the two-phase staging path (stage to the database, then claim partitions in parallel). Scale never weakens the per-record guarantees.

---

## VI. The Research Behind It — Scoring and Measurement

The scoring engine (RiskLens) is what makes "de-identified" a measured property. It runs at Stage 4 on every execution. The privacy dimension is a **hard release gate**; utility and quality are continuous metrics recorded in the audit report.

### A. Three-dimensional model

Every output gets three independent scores in [0, 1]:

| Dimension | Measures |
|---|---|
| **Privacy risk** *R* | Residual re-identification risk — lower is better; above threshold blocks release |
| **Utility** *U* | Analytical value retained — higher is better |
| **Quality** *Q* | Structural correctness and pipeline completeness — higher is better |

When the gate passes (*R* ≤ threshold), they combine **multiplicatively**:

```
privacy_norm = 1 − R / threshold
composite    = privacy_norm × U × Q × 100      (0–100 scale)
```

Multiplication is intentional: **no dimension compensates for another.** Zero utility (everything redacted) or zero quality (structural errors) yields a composite of zero regardless of privacy. If the gate fails, the composite is 0 and the resource is blocked — utility and quality are not even computed. The threshold is configurable (`MEDANON_SCORE_RISK_THRESHOLD`, default **0.30**).

### B. Privacy risk

The per-resource risk is the **maximum** across three sub-evaluators (a fourth, rule-coverage, joins the max only when explicitly enabled):

```
R = max( R_attacker,  R_identifier,  R_text )
```

**R_attacker — attacker model (k-anonymity).** Quasi-identifiers {birth date, gender, postal code} are extracted across Patient resources; the smallest equivalence class *k_min* maps to a risk score:

```
attacker_risk = max(0,  (k_safe − k_min) / (k_safe × 5)),   k_safe = 5
```

*k_min ≥ k_safe* gives zero risk; a singleton (*k_min* → 0) gives 0.20. For cohorts, two further checks apply per equivalence class:
- **l-diversity** [Machanavajjhala et al. 2007]: ≥ *l* = 2 distinct sensitive values per class; risk `1 − l_observed / l_required`.
- **t-closeness** [Li et al. 2007]: Earth Mover's Distance between each class's sensitive-attribute distribution and the global one, scaled against a 0.30 threshold.

The batch attacker risk is `max(k_risk, l_risk, t_risk)`.

**R_identifier — direct identifier detection.** Each output is scanned for HIPAA-sensitive fields still holding non-sentinel values. The 18 Safe Harbor categories are mapped to concrete FHIR paths for 30+ resource types; each exposed field contributes a level from `{ low: 0.05, medium: 0.20, high: 0.45, critical: 0.80 }`. The identifier risk is the maximum exposed-field level.

**R_text — free-text residual.** A two-layer scan over every string value: (1) regex for SSN, phone, email, URL (a hit contributes 0.80); (2) an optional NER re-scan using the same recognizer as the scrubbing stage, at confidence ≥ 0.50. The text risk is the maximum hit.

### C. Utility

A weighted sum of six sub-evaluators:

```
U = 0.20·F_retention + 0.25·F_semantic + 0.15·F_temporal
  + 0.20·F_info_loss + 0.10·F_pseudonym + 0.10·F_longitudinal
```

| Term | Formula | Measures |
|---|---|---|
| F_retention | retained / original fields | Fields still present |
| F_semantic | checks_pass / checks_total | Clinical codes & references intact |
| F_temporal | valid / total orderings | Date ordering preserved |
| F_info_loss | 1 − avg(action weight) | Severity of transformations applied |
| F_pseudonym | min(1, refs_out / refs_in) | ID mapping injective (no entity collapse) |
| F_longitudinal | deterministic / total id-actions | Identity fields linkable across encounters |

Action loss weights: `redact 1.00 · scrub_text 0.60 · substitute 0.40 · generalize 0.35 · perturb 0.30 · cryptohash 0.10 · gpas_pseudonymize 0.10 · encrypt 0.00`. For batches, a seventh term — distribution fidelity (Kahn et al. 2016), via Total Variation Distance `TVD(p,q)=½·Σ|pᵢ−qᵢ|` — contributes 10%.

### D. Quality

Eight sub-checks mapped to the Kahn et al. 2016 data-quality framework (conformance / completeness / plausibility): transformation success rate, rule coverage, FHIR schema validity, reference integrity, real-world plausibility, terminology binding, cardinality, and structural diff. They combine as:

```
Q = success_rate × mean( schema, reference, real_world, terminology,
                         cardinality, structural_diff, rule_coverage )
```

### E. A measured example

The numbers below are the **actual output** of `score_resource()` on a sample Patient whose name and address were redacted, birth date generalized to year, `id` and identifier cryptohashed, and narrative text scrubbed. They are reproducible by running the scoring engine on this input — not hand-chosen.

```
Privacy
  attacker_risk   = 0.15      (single-resource context → modest k)
  identifier_risk = 0.00      (no HIPAA-sensitive field left exposed)
  text_risk       = 0.00      (no PII pattern in scrubbed text)
  R               = 0.15      threshold = 0.30  → PASS

Utility   (U = 0.9167)
  field_retention        = 1.00
  semantic_preservation  = 1.00
  temporal_consistency   = 1.00
  information_loss        = 0.5833
  pseudonym_consistency  = 1.00
  longitudinal_linkability = 1.00

Quality   (Q = 1.00)

Composite
  privacy_norm = 1 − 0.15/0.30 = 0.50
  composite    = 0.50 × 0.9167 × 1.00 × 100 = 45.8     → PASS
```

The audit report stores every sub-score and its evidence, so a reviewer can trace each deduction back to a specific field or action — here, the composite is held down chiefly by `information_loss` (0.5833), reflecting the two `redact` actions, which is the expected cost of suppressing name and address.

---

## VII. Proof — Measured Performance

The figures below are produced by `scripts/benchmark_pipeline.py` on a single host (Intel Xeon Gold 6330 @ 2.00 GHz, 12 cores, 16 GB RAM, Python 3.13.5). The benchmark exercises the **real** engine code paths with a mocked PseudoVault (instant in-process pseudonym map) and no external PHI Sentinel, so the numbers characterize the **Privacy Engine itself** — they exclude network latency to PseudoVault and PHI Sentinel, which dominates in production. The workload is a realistic mix of Patient, Observation, and Encounter resources under 11 rules including `where()`-clauses and wildcards. Numbers vary ±5% run to run.

**Full-pipeline throughput** (`process_data_batch`)

| Batch size | Throughput | Notes |
|---|---|---|
| 1 | ~1,200 resources/s | Cold per-resource overhead |
| 10 | ~2,500 resources/s | Rule cache warming |
| 50 | ~2,800 resources/s | FHIRPath LRU warm |
| 100 | ~3,100 resources/s | Cross-resource PseudoVault dedup active |
| 200 | ~3,500 resources/s | Approaching plateau |
| 500 | ~3,600 resources/s | Cache + dedup fully amortized |

**Streaming** (`process_data_stream`): ~4,000 resources/s — slightly higher than batch mode, as the generator avoids the upfront list allocation.

**Memory:** ~48 MB baseline RSS; +~9–10 MB to process 2,000 resources (pseudonym map + compiled-expression cache), ~59 MB peak. Overhead is released after each batch; peak does not grow across sequential batches.

**FHIRPath evaluation** (three-tier evaluator)

| Evaluator | Throughput | Use |
|---|---|---|
| Native dict traversal (fast-path) | ~1.36M evals/s | Simple dot-paths (`Patient.name`, `*.id`) |
| Native `.where()` evaluator | ~640K evals/s | Extension filtering |
| fhirpathpy (ANTLR interpreter) | ~95–100K evals/s | Full FHIRPath fallback |

The fast-path is roughly **14× faster** than the fhirpathpy interpreter on equivalent expressions; because typical rules use simple and `where()` forms, the interpreter is invoked only for advanced syntax.

**Text-ID replacement** (Aho-Corasick vs. regex). When rewriting reference IDs embedded in narrative text, an Aho-Corasick automaton replaces a compiled alternation regex; the advantage grows with the ID-map size — roughly 1.0–1.1× at 10–100 IDs, ~1.8× at 500, ~2.3× at 1,000, and ~9× at 5,000 IDs.

**Post-processing walk:** merging the reference-rewrite and text-ID-rewrite passes into a single tree walk gives ~1.2× (≈ 55K → 67K resources/s), avoiding a second full traversal per resource.

**Gate behaviour.** The privacy gate was exercised against the engine, not a particular profile: with a rule set that addresses every quasi-identifier present, outputs pass at the default threshold; with the same rule set minus the address/postal-code rules, the gate **rejects** the outputs with a privacy score above threshold. This confirms the gate evaluates the *output* independently of operator intent and blocks release when residual risk is too high — regardless of how the rules were authored.

---

## VIII. Conclusion

Regulations state the outcome de-identification must achieve but not the operative steps, and the engineering is complicated by the richness of the FHIR data model, the disclosure surface of clinical free text, and the correctness requirements of pseudonymization. MedAnon closes this gap by giving legal and IT experts a shared, readable rule language for encoding de-identification decisions, an engine that executes them through a layered portfolio of techniques across FHIR and non-FHIR formats, and — distinctively — a scoring engine that **measures** every output and **blocks** release when the residual re-identification risk is too high. "De-identified" becomes a verified property rather than an assertion, locally-run AI lowers the authoring barrier without ever exposing patient data, and the whole system runs on the data controller's own premises. The performance and scoring figures in this article are reproducible from the engine itself, and we continue to develop MedAnon in step with the evolving FHIR standard and regulatory landscape.

---

## References

1. Article 29 Data Protection Working Party (2014). *Opinion 05/2014 on Anonymisation Techniques (WP216).*
2. El Emam, K. et al. (2009). *Evaluating Common De-Identification Heuristics for Personal Health Information.* JMIR.
3. ENISA (2019). *Pseudonymisation techniques and best practices.*
4. ISO/TS 25237:2017. *Health informatics — Pseudonymization.*
5. Kahn, M. G. et al. (2016). *A Harmonized Data Quality Assessment Terminology and Framework for the Secondary Use of Electronic Health Record Data.* eGEMs.
6. Li, N., Li, T., & Venkatasubramanian, S. (2007). *t-Closeness: Privacy Beyond k-Anonymity and l-Diversity.* ICDE.
7. Machanavajjhala, A. et al. (2007). *l-Diversity: Privacy Beyond k-Anonymity.* ACM TKDD.
8. Narayanan, A. & Shmatikov, V. (2008). *Robust De-anonymization of Large Sparse Datasets.* IEEE S&P.
9. Regulation (EU) 2016/679 (GDPR), Articles 4(5), 25, 32, and Recital 26.
10. Samarati, P. & Sweeney, L. (1998). *Protecting Privacy when Disclosing Information: k-Anonymity and its Enforcement through Generalization and Suppression.*
11. Sweeney, L. (2002). *k-Anonymity: A Model for Protecting Privacy.* Int. J. of Uncertainty, Fuzziness and Knowledge-Based Systems.
12. U.S. Department of Health & Human Services. *45 CFR §164.514(b)* — HIPAA Safe Harbor de-identification standard.
13. University Medicine Greifswald. *gPAS — Generic Pseudonym Administration Service.* https://www.ths-greifswald.de/en/researchers-general-public/gpas/
14. HL7 International. *FHIR R4.* https://hl7.org/fhir/R4/ — and *FHIRPath Specification.* https://hl7.org/fhirpath/
