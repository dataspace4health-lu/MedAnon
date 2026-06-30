---
title: "Pseudonymization Service"
sidebar_position: 3
description: "gPAS TTP service, how pseudonymization works, circuit breaker, domain lifecycle, and Python client modules."
---

# Pseudonymization Service (gPAS)

gPAS (Generic Pseudonym Administration Service) is a trusted-third-party (TTP) service developed by the University Medicine Greifswald. It provides **audited, reversible pseudonymization** via the TTP-FHIR protocol, backed by PostgreSQL.

---

## Why gPAS Instead of Local Hashing

| Approach | Problem |
|---|---|
| Local SHA3-256 hash | Irreversible, adverse event investigation and follow-up linkage become impossible |
| HMAC hash (keyed) | Reversible only by whoever holds the key, no TTP separation |
| gPAS TTP | Reversible by an authorized third party, completely separate from the clinical and research teams |

The TTP model provides **organizational separation**: the clinical team does not have the pseudonym key, and the research team does not have the patient identifiers. Only the TTP operator can perform reverse lookups, and only under controlled procedures.

---

## How Pseudonymization Works

```
anonymizer (pseudonymize stage)
      │
      │  POST $pseudonymizeAllowCreate
      │  Parameters { [id1, id2, id3, ...] }
      ▼
gateway:8080 (Traefik round-robin, network alias gpas-lb)
      │
      ▼
gpas:8080 (WildFly 38)
      │  L1 cache → gpas-db query
      ▼
gpas-db:5432 (PostgreSQL 16)
      │
      ▼  Parameters { [psn1, psn2, psn3, ...] }
      │
anonymizer
      ├─ L1 cache hit  → skip gPAS call entirely
      ├─ L2 Redis hit  → skip gPAS call
      └─ write pseudonyms back to resource fields
```

A single batch HTTP call handles all values from up to 1,000 resources. If all values are L1/L2 cache hits, the gPAS HTTP call is skipped entirely.

---

## Circuit Breaker

Without a circuit breaker, a gPAS outage causes each processing request to wait for the full 30-second timeout before failing, exhausting the anonymizer thread pool.

```
CLOSED (normal operation)
    │  5 failures within 60s
    ▼
OPEN (fail-fast - no gPAS calls for 30s)
    │  30s elapsed
    ▼
HALF-OPEN (1 probe call)
    │  success → CLOSED
    │  failure → OPEN (timer resets)
```

| Variable | Default | Purpose |
|---|---|---|
| `GPAS_CB_FAILURE_THRESHOLD` | `5` | Failures within the window before opening |
| `GPAS_CB_RECOVERY_TIMEOUT_SEC` | `30` | How long the breaker stays open |
| `GPAS_CB_WINDOW_SEC` | `60` | Sliding window for counting failures |

A gPAS outage fails requests fast while keeping the anonymizer responsive for profiles that use only hashing or redaction.

---

## Domain Lifecycle

gPAS organizes pseudonyms into **domains**. Each domain has its own pseudonym namespace and generator algorithm.

1. **Create domain** via gPAS web UI (`http://localhost:8080/gpas-web/`) or `make init-domains`
2. Domain is stored in `gpas-db` and loaded into the JVM `domainLocks HashMap`
3. `$pseudonymizeAllowCreate`, creates a pseudonym if it does not exist, returns the existing one if it does
4. **Reverse lookup:** `$depseudonymize`, requires TTP admin credentials

:::danger Never insert domains directly into PostgreSQL
gPAS maintains a `domainLocks HashMap` in JVM memory that is populated only when domains are created through the API. Direct SQL inserts appear to work at the DB level but cause "domain not found" errors at runtime during pseudonymization.
:::

---

## Depseudonymization

The `gpas_depseudonymize` action reverses a pseudonym back to its original value. Requires TTP admin credentials. Used for adverse event investigation workflows.

```yaml
rules:
  - name: "reverse patient id for investigation"
    match: "Patient.id"
    action: "gpas_depseudonymize"
```

---

## Python Client Modules (`integrations/gpas/`)

| Module | Role |
|---|---|
| `client.py` | Public API: `gpas_pseudonymize_batch`, `gpas_depseudonymize_batch`. Checks L1 + L2 cache before calling transport. |
| `transport.py` | HTTP retry loop (2 retries, exponential backoff), URL resolution, cache helpers, domain listing. |
| `circuit_breaker.py` | Three-state circuit breaker. Singleton per process. |
| `protocol.py` | FHIR Parameters request builders and response parsers for `$pseudonymize` / `$depseudonymize`. |
| `adapter.py` | `GpasPseudonymizerAdapter`, implements `PseudonymizerPort`. Wraps `gpas_pseudonymize_batch`. |
| `canary.py` | Cache-coherence probe, verifies L2 Redis cache is consistent with gPAS state after a restart. |
