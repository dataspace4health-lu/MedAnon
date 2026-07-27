# Introduction  Health Data Preparation & Privacy Toolkit

## Who this documentation is for

This documentation targets three audiences:

- **Architects**  understand the system design, integration patterns, and the rationale behind key decisions
- **Developers**  onboard quickly, understand module boundaries, run tests, extend the pipeline
- **Operations teams**  deploy, monitor, back up, and troubleshoot the system in production

New team members should read [Section 0 → 1 → 2.3](INDEX.md) in order before touching any configuration or code.

---

## What the system does

**SPE FHIR BlackBox** (branded as **MedAnon**) is a healthcare data de-identification and pseudonymization engine. It transforms identified patient records  clinical FHIR R4 resources, HL7 v2 messages, and DICOM files  into privacy-safe equivalents that can be shared with research institutions, analytics platforms, or data space connectors without exposing patient identity.

The system does three things well:

1. **Rule-driven de-identification**  A YAML configuration file maps FHIRPath expressions to de-identification actions (redact, hash, pseudonymize, generalize, encrypt, NLP scrub). Any team member can read and audit the rules. Seven built-in profiles cover the main regulatory scenarios out of the box.

2. **Reversible pseudonymization**  Via gPAS (Generic Pseudonym Administration Service), a trusted third-party (TTP) service, patient IDs can be pseudonymized in a way that is reversible under controlled conditions. This supports adverse event investigation and longitudinal study linkage.

3. **Compliance-ready output**  Profiles for GDPR, HIPAA Safe Harbor, IRB research, and structural preservation. Every processed resource can be scored for privacy risk, data utility, and FHIR structural quality.

---

## Why it was built

Clinical data sharing is constrained by law and ethics. GDPR (EU), HIPAA (US), and national frameworks require that patient identifiers be removed before data leaves the clinical environment. Common alternative approaches have trade-offs:

| Approach | Trade-off |
|---|---|
| Manual de-identification | Slow, error-prone, not reproducible |
| Commercial products | Expensive licenses, opaque rules, vendor lock-in |
| Simple hashing | Irreversible  prevents follow-up linkage for adverse events |
| NLP-only tools | No FHIR structure awareness, no compliance profiles |

SPE FHIR BlackBox addresses all four trade-offs: it is open, auditable, reversible where needed, FHIR-native, and covers the main regulatory frameworks.

---

## Key use cases

| Use case | Description | Relevant config profile |
|---|---|---|
| **Research data export** | Bulk export of a patient cohort to a research data warehouse | `config_research_pseudonymous.yaml` |
| **GDPR-compliant sharing** | Share data with EU research partners | `config_gdpr_eu.yaml` |
| **HIPAA Safe Harbor** | Share data with US partners | `config_hipaa_safe_harbor.yaml` |
| **Data space integration** | Feed de-identified FHIR into IDSA / FIWARE connectors | `config_gpas.yaml` |
| **Real-time API pipeline** | Per-request de-identification for streaming pipelines | Any profile, `POST /process` |
| **Pseudonymization hub** | Centralized TTP pseudonymization for multi-site studies | `config_gpas.yaml` with gPAS |
| **NLP narrative scrubbing** | Remove patient names and identifiers from clinical notes | `config_value_masking.yaml` |
| **AI-assisted config authoring** | Generate YAML rules from natural-language description | `/v1/ai/generate-config` |
| **Compliance auditing** | Score de-identified output for regulatory gaps | `/v1/jobs/{id}/score` |

---

## System at a glance

```
Identified FHIR data                       De-identified FHIR data
(source FHIR server)                       (target FHIR server / NDJSON)
        │                                            ▲
        │                                            │
        ▼                                            │
┌─────────────────────────────────────────────────────────────────┐
│                  SPE FHIR BlackBox (MedAnon)                    │
│                                                                  │
│  ┌─────────────┐   ┌────────────┐   ┌──────────────────────┐   │
│  │  REST API   │   │  4-stage   │   │  Config profiles      │   │
│  │  (FastAPI)  │──▶│  pipeline  │──▶│  (7 built-in YAML)   │   │
│  │             │   │            │   │                       │   │
│  │  React UI   │   │  + AI      │   │  + user-defined       │   │
│  │  (nginx SPA)│   │  agents    │   │    profiles           │   │
│  └─────────────┘   └─────┬──────┘   └──────────────────────┘   │
│                          │                                       │
│         ┌────────────────┼─────────────────┐                    │
│         ▼                ▼                 ▼                    │
│  ┌────────────┐  ┌────────────┐  ┌──────────────┐              │
│  │ gPAS TTP   │  │ NLP micro  │  │  Scoring     │              │
│  │ (reversible│  │ (Presidio  │  │  (privacy ×  │              │
│  │  pseudonym)│  │  + spaCy)  │  │  utility ×   │              │
│  └────────────┘  └────────────┘  │  quality)    │              │
│                                   └──────────────┘              │
└─────────────────────────────────────────────────────────────────┘
```

---

## Technology stack summary

| Layer | Technology | Purpose |
|---|---|---|
| **API** | Python 3.12, FastAPI, uvicorn | REST API with Pydantic validation, OpenAPI docs |
| **Pipeline** | Custom Python, FHIRPath (fhirpathpy) | 4-stage de-identification orchestrator |
| **NLP** | Microsoft Presidio + spaCy `en_core_web_lg` | Named-entity recognition for clinical narrative PHI |
| **Pseudonymization** | gPAS (University Medicine Greifswald), WildFly 38 | Reversible TTP pseudonymization |
| **Databases** | PostgreSQL 16 (×4 instances), Redis 7 | App state, HAPI, gPAS, job queue, cache |
| **FHIR** | HAPI FHIR R4 v7.6.0, fhirpathpy | Source + target FHIR servers, FHIRPath evaluation |
| **AI agents** | litellm (OpenAI / Ollama), Presidio | Config generation, PII detection, compliance analysis |
| **Frontend** | React 19, TypeScript 5.9, Vite 8, Tailwind CSS 4, Shadcn/ui | 24-page SPA with job management, analytics dashboard, and visual config builder |
| **Infrastructure** | Docker Compose (15 always-on services + 7 opt-in profiles), Helm (9 sub-charts) | Local + Kubernetes deployment |
| **Observability** | Prometheus, structured JSON audit log, Prometheus Pushgateway | Metrics, audit trail, alerting |

---

## Repository structure

```
privacy-toolkit/
├── services/
│   ├── anonymizer/          Main de-identification service (FastAPI + pipeline)
│   │   ├── src/             Python source (PYTHONPATH root inside container)
│   │   ├── config/          7 bundled YAML de-identification profiles
│   │   ├── sql/             PostgreSQL init schema (medanon schema)
│   │   └── tests/           pytest suite (600+ tests)
│   ├── analytics/           Analytics microservice (risk + synthetic data)
│   └── nlp/                 NLP microservice (Presidio + spaCy, ~800 MB image)
├── client/                  React 19 SPA (24 pages, Vite, Tailwind, Shadcn)
├── helm/                    Kubernetes Helm charts (umbrella + 9 sub-charts)
├── docs/                    All project documentation (this directory)
└── scripts/                 Utility scripts (patient import, verify, batch)
```

---

## Development setup (quick start)

```bash
# 1. Clone and configure
git clone <repo>
cd privacy-toolkit
cp .env.example .env     # edit and fill in secrets

# 2. Start the full stack
make up                  # Docker Compose with preflight checks
docker compose ps        # wait until all show "healthy" (~90s for gPAS)

# 3. Initialize gPAS domain
make init-domains

# 4. Verify
curl http://localhost:8000/health    # {"status":"ok"}
open http://localhost:8501           # React UI
```

For local Python development (without Docker):

```bash
make setup               # create venv, install anonymizer deps
cd services/anonymizer
python3 -m pytest tests/ -q   # run test suite
make lint                # ruff linting
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full deployment guide including Kubernetes.

---

## Documentation roadmap

| Section | File | When to read |
|---|---|---|
| **Introduction** (this file) | `introduction.md` | First |
| **Architecture** | `architecture.md` | Before writing any code |
| **Component Catalog** | `components.md` | Before touching a specific module |
| **Deployment** | `DEPLOYMENT.md` | Before running in production |
| **Policies** | `policies.md` | Before choosing a de-identification profile |
| **Scoring System** | `scoring-system.md` | Before interpreting scores |
| **User Manual** | `user-manual.md` | Before using the UI or REST API |
| **Result Example** | `result-example.md` | To understand what the output looks like |
| **Runbook** | `RUNBOOK.md` | When operating in production |
