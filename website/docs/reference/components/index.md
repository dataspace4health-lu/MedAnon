---
title: "Overview"
sidebar_position: 1
description: "Navigation map for the Component Catalog."
---

# Component Catalog

Technical reference for all services and modules. For architecture diagrams and design decisions see [Architecture](../../explanation/architecture.md). For data flow traces see [Data Flow](../../explanation/data-flow.md).

| Sub-page | What it covers |
|---|---|
| [Infrastructure](./infrastructure.md) | Docker services, networks, routing (nginx + Traefik), PostgreSQL databases, Redis, shared environment variables |
| [Pseudonymization Service](./pseudonymization.md) | gPAS TTP service, how pseudonymization works, circuit breaker, domain lifecycle, Python client modules |
| [De-identification Engine](./engine.md) | FastAPI REST API, 4-stage pipeline, action registry, async job system, scoring sub-system, AI agents, NLP integration, shared utilities |
| [Integrations & Storage](./integrations.md) | FHIR client, analytics microservice, NLP microservice, FHIR Subscriptions, SMART on FHIR, staging layer, PostgreSQL stores, domain types |
