---
title: "Technology & Stack"
sidebar_position: 7
description: "Every library, framework, and runtime used in MedAnon and why it was chosen."
---

# Technology & Stack

---

## Runtimes

| Technology | Version | Used by | Why |
|---|---|---|---|
| **Python** | 3.12 | anonymizer, worker, analytics, nlp, scoring | Latest stable; 3.13 causes `fhirpathpy` import failures (`typing.io` removed). |
| **Node.js** | 20 (LTS) | React SPA build (Vite) | Required by Vite 8; not present in runtime images. |
| **JVM / WildFly** | WildFly 38 | gPAS | gPAS is a Java EE application; WildFly is its supported runtime. |

---

## Backend Frameworks & Libraries

### API & Async

| Library | Version | Role |
|---|---|---|
| **FastAPI** | 0.115 | HTTP API framework, async, typed, OpenAPI auto-generation |
| **Uvicorn** | 0.30 | ASGI server, runs FastAPI in production |
| **Pydantic v2** | 2.x | Request/response models, config schema validation, rule schema validation |
| **slowapi** | 0.1 | Rate limiting middleware (wraps `limits` library) |

### FHIR & Clinical Data

| Library | Version | Role |
|---|---|---|
| **fhirpathpy** | latest | FHIRPath R4 expression evaluation, `match:` field selection in rules |
| **defusedxml** | 0.7 | Safe XML parsing (prevents XXE attacks) for FHIR XML input |
| **fhir.resources** |, | FHIR R4 Pydantic models (used selectively for validation) |

### NLP & PII Detection

| Library | Version | Role |
|---|---|---|
| **presidio-analyzer** | 2.x | NLP-based PII entity detection, runs in the `nlp` microservice |
| **presidio-anonymizer** | 2.x | Entity replacement and token substitution |
| **spaCy** | 3.x | NLP pipeline backbone; model `en_core_web_lg` (~750 MB) used by Presidio |

### Pseudonymization

| Technology | Version | Role |
|---|---|---|
| **gPAS** | 2025.2.0 | Trusted-third-party reversible pseudonymization (University Medicine Greifswald) |
| **WildFly** | 38 | Java EE application server hosting gPAS |
| **TTP-FHIR protocol** |, | FHIR R4 Parameters-based API for `$pseudonymize` / `$depseudonymize` |

### Databases & Storage

| Technology | Version | Role |
|---|---|---|
| **PostgreSQL** | 16 | App state (jobs, configs, staging, subscriptions, processing runs); gPAS pseudonym store; HAPI FHIR backing stores |
| **psycopg2** | 2.9 | PostgreSQL driver, `ThreadedConnectionPool` shared across all stores |
| **Redis** | 7 | Job queue (Streams + consumer groups) and L2 pseudonym + NLP detection cache |
| **redis-py** | 5.x | Redis client, used by job store, gPAS L2 cache, and NLP L2 cache |
| **SQLite** | bundled | Local dev fallback for job store (WAL mode, no external dependency) |

### Cryptography & Security

| Library | Version | Role |
|---|---|---|
| **cryptography** | 42+ | RSA encrypt/decrypt for the `encrypt` action |
| **PyJWT** | 2.x | JWT validation for OIDC/SMART auth |
| **PyJWKClient** | (PyJWT extra) | JWK Set fetching and key rotation for OIDC token validation |
| **hashlib** (stdlib) |, | SHA3-256 for plain `cryptohash`; HMAC-SHA3-256 for keyed hash |
| **secrets** (stdlib) |, | CSPRNG for `perturb` action and `bounded_random` |

### AI Integration

| Library | Version | Role |
|---|---|---|
| **litellm** | 1.x | LLM abstraction layer, routes to Ollama (local), OpenAI, Azure, or any OpenAI-compatible endpoint |
| **Ollama** | latest | Local LLM inference (`--profile ai`), keeps PHI on-premise |

### Observability

| Library | Version | Role |
|---|---|---|
| **prometheus-client** | 0.20 | Prometheus counters and histograms exposed on `/metrics` |
| **structlog** / standard `logging` |, | Structured log output |
| **OpenTelemetry** |, | Trace export to Jaeger (`--profile monitoring`) |

### Synthetic Data (analytics service)

| Library | Version | Role |
|---|---|---|
| **SDV (Synthetic Data Vault)** | 1.x | Advanced synthetic data generation (conditional, relational, time-series), optional, ~2 GB |
| **pandas** | 2.x | Tabular data manipulation in analytics and tabular de-identification |
| **pyarrow** | 15+ | Parquet I/O for tabular de-identification |

---

## Frontend

| Technology | Version | Role |
|---|---|---|
| **React** | 19 | SPA framework, concurrent mode, Suspense, lazy routing |
| **TypeScript** | 5.9 | Type safety across all client code |
| **Vite** | 8 | Build tool and dev server, fast HMR |
| **Tailwind CSS** | 4 | Utility-first CSS |
| **Shadcn/ui** | (New York) | Accessible component library built on Radix UI primitives |
| **Recharts** | 2.x | Chart components for analytics dashboard |
| **React Router** | 6 | Client-side routing (16 lazy-loaded routes) |
| **react-oidc-context** | 3.x | OIDC/Keycloak session management for the SPA |

---

## Infrastructure

| Technology | Version | Role |
|---|---|---|
| **Docker Compose** | v2 | Local development stack, 28 services, 7 optional profiles |
| **Traefik** | v3 | API gateway, Docker-provider service discovery, round-robin LB, sticky sessions for gPAS |
| **nginx** | 1.27 | UI reverse proxy, serves SPA, proxies `/api/`, `/fhir/`, `/fhir-target/` |
| **Helm** | 3.x | Kubernetes deployment, umbrella chart + 9 sub-charts |
| **K3s** |, | Lightweight Kubernetes target (override values in `helm/k3s-values.yaml`) |
| **Keycloak** | 26.1 | OIDC identity provider (`--profile auth`), realm `medanon`, Keycloak-to-Azure swap is env-only |
| **MinIO** | latest | S3-compatible object storage for job results (`--profile s3`) |
| **RabbitMQ** | 3.x | Macro-stage work streaming for DAG workflows; opt-in via `MEDANON_AMQP_URL` (external broker, no bundled compose service) |
| **Prometheus + Grafana + cAdvisor + Jaeger** | latest | Metrics, dashboards, container metrics, distributed tracing (`--profile monitoring`) |
| **HAPI FHIR** | 7.6.0 | Source and target FHIR R4 servers |

---

## Key Architectural Constraints

- **Python 3.12 in Docker, 3.13 locally**, `fhirpathpy` imports `typing.io` (removed in 3.13). The Docker image pins 3.12. Tests that exercise the FHIRPath path self-bootstrap a `typing.io` shim at module top so they pass under both versions.
- **NLP microservice is always-on**, the spaCy model makes it impractical to run in-process per anonymizer replica. The `nlp_scrub` action in any rule set requires the NLP service to be reachable.
- **gPAS is optional**, when `GPAS_URL` is unset, the engine auto-selects the `config.yaml` profile (HMAC hash + regex scrubbing). All gPAS-specific actions (`gpas_pseudonymize`, `gpas_depseudonymize`) are skipped gracefully.
- **SDV is isolated**, the SDV synthetic data library adds ~2 GB. It lives exclusively in the analytics microservice Dockerfile and never enters the anonymizer image.
