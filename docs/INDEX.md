# Health Data Preparation & Privacy Toolkit — Documentation

**System:** SPE FHIR BlackBox (MedAnon)  
**Version:** Phase 5 (use-case validation, AI agents + Kubernetes)  
**Last updated:** 2026-06-05

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
| [architecture.md](architecture.md) | System topology, 4-stage pipeline, async job queue, gPAS, config profiles, auth |
| [data-flow.md](data-flow.md) | Network layout, single-resource flow trace, bulk export flow, NLP batch flow, gPAS cache detail |
| [components.md](components.md) | Component catalog: services, modules, integrations, API endpoints |

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

The anonymizer: FastAPI entry point, 4-stage pipeline modules, all actions, NLP integration, scoring system, AI agents, async job system.  
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

- Profile comparison table (all 7 selectable built-in profiles)
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

**File:** [introduction.md](introduction.md)

Problem statement, solution summary, and capability overview.

### 3.9 User Manual

**File:** [user-manual.md](user-manual.md)

- Web UI walkthrough (all 16 pages)
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

These files exist locally but are excluded from the repository via `.gitignore`. They contain planning documents and architecture reviews not intended for public distribution. See [internal/INDEX.md](internal/INDEX.md) for the full index; superseded snapshots are under `internal/_archive/`.

| Document | Description |
|---|---|
| [internal/CURRENT_STATE.md](internal/CURRENT_STATE.md) | 1-page orientation: what works in production today |
| [internal/BACKEND_FILE_MAP_2026_06_04.md](internal/BACKEND_FILE_MAP_2026_06_04.md) | Authoritative file-by-file module map + 4-stage pipeline |
| [internal/REVIEW_STATUS.md](internal/REVIEW_STATUS.md) | Single-source-of-truth ledger for every review finding |
| [internal/MASTER_TASK_PLAN_2026_06_08.md](internal/MASTER_TASK_PLAN_2026_06_08.md) | Consolidated forward roadmap (platform + engine-quality tracks) |
| [internal/keycloak-auth-plan.md](internal/keycloak-auth-plan.md) | Keycloak/OIDC integration design (auth — NOT STARTED) |
