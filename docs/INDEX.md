# Health Data Preparation & Privacy Toolkit — Documentation

**System:** SPE FHIR BlackBox (MedAnon)  
**Version:** Phase 4 (AI agents + Kubernetes)  
**Last updated:** 2026-04-21

This index is the single entry point for all project documentation. Each section links to the relevant file. New team members should read sections 0 → 1 → 2.3 in order.

---

## 0. Introduction

**File:** [introduction.md](introduction.md)

- What the system does and why it was built
- Key use cases
- Audience and scope
- Technology stack summary

---

## 1. Architecture

**Core architecture and design decisions:**

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | System topology, 4-pass pipeline, async job queue, gPAS, config profiles, auth |
| [data-flow.md](data-flow.md) | Network layout, single-resource flow trace, bulk export flow, NLP batch flow, gPAS cache detail |
| [architecture-overview.md](architecture-overview.md) | 5-layer architecture diagram, service layer, pipeline internals, NLP microservice, scoring |

---

## 2. Component Catalog

**File:** [components.md](components.md)

The component catalog is organized into four subsections matching the deployment topology:

### 2.1 Infrastructure & Shared Configuration

Docker services, nginx load balancers, PostgreSQL databases, Redis, environment variables, secrets.  
→ See [components.md § Infrastructure](components.md#21-infrastructure--shared-configuration)

### 2.2 Pseudonymization Service (gPAS + PostgreSQL)

gPAS TTP service, circuit breaker, pseudonym cache (L1 LRU + L2 Redis), domain lifecycle, depseudonymization.  
→ See [components.md § Pseudonymization](components.md#22-pseudonymization-service-gpas--postgresql)

### 2.3 De-identification Engine

The anonymizer: FastAPI entry point, 4-pass pipeline modules, all actions, NLP integration, scoring system, AI agents, async job system.  
→ See [components.md § De-identification Engine](components.md#23-de-identification-engine)

### 2.4 Data Consumers

FHIR client (reader, writer, bulk), analytics microservice, FHIR subscriptions, SMART on FHIR, data connectors.  
→ See [components.md § Data Consumers](components.md#24-data-consumers)

---

## 3. Deployment

### 3.1 Deployment Guide

**File:** [DEPLOYMENT.md](DEPLOYMENT.md)

- Prerequisites and environment setup
- Docker Compose: step-by-step startup, gPAS domain initialization
- Opt-in profiles (HA, S3, AI/Ollama)
- Kubernetes / Helm chart structure and installation
- Resource limits, TLS, production hardening

### 3.2 Connector Integration

**File:** [connector-integration.md](connector-integration.md)

How to connect SPE FHIR BlackBox to IDSA / FIWARE dataspace connectors and EDC.

### 3.3 De-identification Scoring System

**File:** [scoring-system.md](scoring-system.md)

- Constraint-based multiplicative model (privacy × utility × quality)
- Privacy gate: attacker model, HIPAA identifier check, text risk
- Utility scorer: field retention, date precision, clinical code coverage
- Quality scorer: FHIR structural validity, reference integrity
- API reference: `/v1/score`, `/v1/jobs/{id}/score`, `/v1/jobs/{id}/score/report`

### 3.4 Security & Compliance

**File:** [security.md](security.md)

- Authentication: API key (HMAC constant-time), SMART on FHIR bearer tokens
- RBAC: viewer / analyst / admin role hierarchy and per-endpoint mapping
- Encryption in transit: Docker network segmentation, TLS at the perimeter, Redis password
- Encryption at rest: HMAC-SHA3-256 pseudonyms, RSA field encryption, gPAS TTP mappings
- Input safety: defusedxml, request size limit, SSRF protection, rate limiting
- Container hardening: `read_only`, `cap_drop: ALL`, `no-new-privileges`, memory limits
- Audit logging: structured JSON, Redis Stream, rotating file — never logs PHI
- GDPR compliance mapping: Art. 4(5), 25, 30, 32, 89 — per profile
- HIPAA Safe Harbor: all 18 PHI categories with FHIR field mapping
- Known open security issues (Phase 4 AI agents)

### 3.5 Policies

**File:** [policies.md](policies.md)

- Profile comparison table (all 7 profiles)
- When to use each profile: GDPR, HIPAA Safe Harbor, IRB Research, Structure Preserving, Value Masking
- Compliance mapping: what each profile satisfies and what it does not
- Custom profile authoring guide

### 3.6 API Reference

**File:** [api-reference.md](api-reference.md)

- Authentication header and open endpoints
- Error format (RFC 7807) and status codes
- Rate limits per endpoint group
- Config profile selection (`?config_profile=`)
- All endpoint groups: health, de-identification, async jobs, scoring, configs, analytics, processing-runs, AI agents, FHIR operations, audit
- Request/response examples for each group
- Quick-start curl examples: de-identify a patient, run a bulk export, score a resource

### 3.7 Connector Integration

**File:** [connector-integration.md](connector-integration.md)

- Pattern A: Pre-built NDJSON asset with EDC contract definition and usage policy
- Pattern B: Server-to-server round-trip
- Pattern C: EDC HTTP Data Plane proxy (inline de-identification during transfer)
- Pattern D: FIWARE/NGSI-LD middleware (FHIR → de-identify → NGSI-LD entity → Orion-LD)
- Authentication summary across all interfaces
- Operational notes (streaming, reference integrity, SSRF, large datasets)

### 3.8 Solution Overview

**File:** [internal/architecture-presentation.md](internal/architecture-presentation.md) *(internal — not tracked by git)*

Executive-level overview: problem statement, solution summary, competitive positioning, phased roadmap.

### 3.9 User Manual

**File:** [user-manual.md](user-manual.md)

- Web UI walkthrough (all 13 pages)
- REST API quick-start: de-identify a patient, run a bulk export, check risk score
- CLI batch processing
- Troubleshooting common errors

### 3.10 Result Example

**File:** [result-example.md](result-example.md)

Side-by-side examples of identified FHIR input vs de-identified output for each config profile. Annotated to explain every transformation applied.

---

## Additional references

| Document | Description |
|---|---|
| [TECHNICAL_DOCUMENTATION.md](TECHNICAL_DOCUMENTATION.md) | Comprehensive single-document reference: all 9 areas in one file (for onboarding or printing) |
| [RUNBOOK.md](RUNBOOK.md) | Operations runbook: monitoring, backup, secret rotation, common troubleshooting |

## Internal references *(docs/internal/ — not tracked by git)*

These files exist locally but are excluded from the repository via `.gitignore`. They contain security audits, competitive analysis, planning documents, and detailed architecture reviews that are not intended for public distribution.

| Document | Description |
|---|---|
| [internal/architecture-overview.md](internal/architecture-overview.md) | 5-layer architecture diagram with service layer, pipeline internals, NLP microservice detail |
| [internal/architecture-presentation.md](internal/architecture-presentation.md) | Executive-level overview: problem statement, competitive positioning, phased roadmap |
| [internal/auth-security-roadmap.md](internal/auth-security-roadmap.md) | Security audit findings and phased remediation plan |
| [internal/backend-technology-reference.md](internal/backend-technology-reference.md) | Technology stack quick reference: all libraries, versions, and rationale |
| [internal/BACKEND_REVIEW_2026_04_20.md](internal/BACKEND_REVIEW_2026_04_20.md) | Full backend code review: 8 critical, 18 high, 32 medium findings |
| [internal/ARCHITECTURE_REVIEW_2026_04_21.md](internal/ARCHITECTURE_REVIEW_2026_04_21.md) | Deep microservice architecture review: 12 critical, 31 high findings |
| [internal/COMPETITIVE_ANALYSIS.md](internal/COMPETITIVE_ANALYSIS.md) | Competitive analysis vs 7 market solutions |
| [internal/PERFORMANCE_ANALYSIS.md](internal/PERFORMANCE_ANALYSIS.md) | Performance benchmarks and tuning analysis |
| [internal/PRODUCT_BACKLOG.md](internal/PRODUCT_BACKLOG.md) | Product backlog and feature roadmap |
| [internal/PlanF.MD](internal/PlanF.MD) | Architecture evolution plan (database migration, AI integration) |
| [internal/AI.md](internal/AI.md) | AI agent architecture evolution notes |
| [internal/EVIDENCE_REPORT_TEMPLATE.md](internal/EVIDENCE_REPORT_TEMPLATE.md) | Fillable audit evidence report template |
