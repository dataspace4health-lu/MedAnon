# Security & Compliance

**System:** SPE FHIR BlackBox (MedAnon)  
**Version:** Phase 4  
**Last updated:** 2026-04-21

This document describes the security architecture of SPE FHIR BlackBox: how it authenticates callers, how it protects data in transit and at rest, what it logs, and which regulatory obligations each configuration profile satisfies.

---

## 1. Authentication & Authorization

### 1.1 API Key Mode

When `MEDANON_API_KEY` is set, every request to a protected endpoint must carry the header:

```
X-API-Key: <key>
```

The check uses `hmac.compare_digest()` (constant-time comparison — immune to timing attacks). A missing or mismatched key returns `HTTP 401`.

**Open endpoints** (no key required regardless of configuration):

| Path | Purpose |
|---|---|
| `/health` | Docker/K8s liveness probe |
| `/ready` | Readiness probe (checks Redis, gPAS, NLP) |
| `/metrics` | Prometheus metrics scraping |
| `/docs`, `/openapi.json`, `/redoc` | API documentation |
| `/.well-known/smart-configuration` | SMART on FHIR discovery |

**Open mode**: When `MEDANON_API_KEY` is not set, every caller is granted admin privileges. This is intentional for local development but **must not be used in production**. The anonymizer logs a warning at startup.

### 1.2 SMART on FHIR (Bearer Token)

Callers may instead send `Authorization: Bearer <token>`. The token is introspected via `SMART_INTROSPECTION_URL` (RFC 7662). On success, SMART scopes are mapped to MedAnon RBAC roles:

```
patient/*.read   → viewer
user/*.read      → analyst
system/*.write   → admin
```

If the introspection endpoint is unreachable, the call fails with `HTTP 503` (not 401) — MedAnon never grants access on introspection failure.

Fallback: when `SMART_INTROSPECTION_URL` is not set, MedAnon accepts the configured API key as a bearer token (allows clients that send the key in the Authorization header).

### 1.3 RBAC

Three roles form a strict hierarchy (`admin > analyst > viewer`):

| Role | Scope |
|---|---|
| `viewer` | Read-only: list configs, view AI status, read processing run history |
| `analyst` | All processing operations, job management, risk analysis, AI explain/compliance/detect, FHIR Patient export |
| `admin` | Everything above + config writes, round-trip/upload/bulk-export, FHIR Group export, AI config generation |

Role is resolved from `ENDPOINT_ROLES` (exact match) then `ENDPOINT_ROLE_PREFIXES` (prefix match). Source: [api/auth.py](../services/anonymizer/src/api/auth.py#L50-L104).

Key enforcement points:

| Endpoint | Required role | Note |
|---|---|---|
| `POST /v1/process/and-upload` | admin | Writes to target FHIR server |
| `POST /v1/process/round-trip` | admin | Source read + target write |
| `POST /v1/jobs/bulk-export` | admin | Bulk export to filesystem/S3 |
| `POST /v1/ai/generate-config` | admin | AI writes config profile |
| `DELETE /v1/processing-runs` | analyst | Known issue: should require admin — pending fix |
| `POST /v1/configs` | admin | Enforced in the router even though prefix returns viewer |

---

## 2. Encryption in Transit

### 2.1 Network Segmentation

Docker Compose uses two isolated bridge networks:

```
┌──────────────────────────────────────────────────────────────┐
│  processing-net  (all services)                              │
│                                                              │
│  anonymizer ── worker ── redis ── app-db ── analytics ──    │
│  gateway (Traefik, aliases: gpas-lb, nlp-lb) ── nlp ──      │
│  gpas ── gpas-db ── fhir-target ── hapi-target-db ── ui ──  │
│  ollama (opt-in) ── minio (opt-in)                           │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│  source-net  (isolated — NEVER reachable from the internet)  │
│                                                              │
│  fhir-server ── hapi-db ── anonymizer ── worker             │
│                                                              │
│  (only anonymizer and worker bridge both networks)           │
└──────────────────────────────────────────────────────────────┘
```

The source FHIR server (`fhir-server`) has **no host port**. It is only reachable from within the `source-net`. External systems can never query identified patient records directly.

### 2.2 TLS at the Perimeter

All Docker Compose inter-service traffic is on private bridge networks (untrusted LAN never sees it). TLS termination is at the perimeter:

- **Production (Kubernetes)**: TLS terminates at the Ingress controller (nginx-ingress or cloud LB). The Helm chart supports `ingress.tls` configuration.
- **Docker Compose production**: Place a TLS-terminating reverse proxy (e.g. nginx, Traefik) in front of `UI_PORT` (8501) and `ANONYMIZER_PORT` (8000). See the production hardening section in [DEPLOYMENT.md](DEPLOYMENT.md).

### 2.3 Redis Security

Redis is password-protected in all non-development environments. The connection URL format:

```
MEDANON_REDIS_URL=redis://:password@redis:6379/0
```

The anonymizer, worker, and rate-limiter all authenticate via this URL. Rate-limit counters are stored on Redis DB 2 (`/2`), audit stream on the default DB.

---

## 3. Encryption at Rest

### 3.1 HMAC Pseudonymization (Primary)

The `cryptohash` action applies HMAC-SHA3-256 keyed with `MEDANON_HASH_KEY`. This is the default pseudonymization mechanism for the GDPR and HIPAA profiles.

```yaml
rules:
  - name: "pseudonymize patient ID"
    match: "Patient.id"
    action: "cryptohash"
```

Without the key, the pseudonym cannot be reversed — it is a one-way commitment. The key **must** be set in production:

```bash
# Generate a cryptographically secure key (32 bytes → 64 hex chars)
python3 -c "import secrets; print(secrets.token_hex(32))"
```

`MEDANON_HASH_ALLOW_PLAIN=true` bypasses the HMAC requirement (plain SHA3-256). This is **only safe for local development and tests** — never set in production.

### 3.2 RSA Field Encryption

The `encrypt` action applies RSA-OAEP (SHA-256 padding) with a 2048-bit minimum key size. Used for fields that must be reversible without a TTP (unlike gPAS).

Key path validation prevents path-traversal attacks:
- `MEDANON_KEY_ALLOWED_DIRS` constrains which directories may be read for key material
- `os.path.realpath()` resolves symlinks before checking the allowlist
- The RSA key is loaded once and cached keyed on `(path, mtime)` — re-read on rotation

Key paths: `MEDANON_RSA_PUBLIC_KEY` / `MEDANON_RSA_PRIVATE_KEY` (inside the container). Private keys are in `.gitignore` (`services/anonymizer/keys/id_rsa`, `*.pem`, `*.key`) and must never be committed to source control.

### 3.3 gPAS Pseudonymization

gPAS stores pseudonym mappings in its PostgreSQL database. The mapping is:

```
original value  →  gPAS pseudonym (e.g. psn-R7K2M9P4)
```

gPAS pseudonyms are reversible **only by a TTP admin** with access to the gPAS server. The Python client never holds the mapping table — it calls gPAS via HTTP to resolve values in both directions.

gPAS PostgreSQL volumes (`gpas-postgres`) are backed by a Docker named volume. For production: encrypt the host filesystem or use cloud-managed encrypted storage (e.g. AWS EBS with KMS).

### 3.4 PostgreSQL Application Database

`app-db` stores jobs, configs, FHIR subscriptions, and staging data — **no PHI**. Job results (NDJSON) are stored in a Docker volume mounted at `/output` (or S3 when `MEDANON_RESULT_STORAGE=s3`). These results are de-identified outputs; they should not contain original PHI if the pipeline ran correctly.

Result files are auto-deleted after `MEDANON_RESULT_TTL_SEC` seconds (default: no TTL, disabled by default). Enable in production:

```bash
MEDANON_RESULT_TTL_SEC=86400   # 24 hours
```

---

## 4. Input Safety

### 4.1 XML Safety (defusedxml)

All XML input is parsed via `defusedxml`, which blocks:

- Billion laughs (XML bomb)
- Quadratic blowup
- External entity injection (XXE)
- External DTD loading

### 4.2 Request Size Limit

The maximum accepted request body is controlled by `MEDANON_MAX_BODY_BYTES` (default: 10 MB). Requests exceeding this limit are rejected before the body is read.

```
MEDANON_MAX_BODY_BYTES=10485760  # 10 MB
```

### 4.3 SSRF Protection

Any user-supplied URL (e.g. `server_url` in `/process/from-server`) is validated against a private-network blocklist **before the connection is attempted**:

```
Blocked networks:
10.0.0.0/8       172.16.0.0/12    192.168.0.0/16
127.0.0.0/8      169.254.0.0/16   0.0.0.0/8
::1/128           fc00::/7         ::ffff:0:0/96
```

DNS-based SSRF is also blocked: hostnames are resolved at validation time, and any address that falls in the above ranges causes HTTP 422. IPv4-mapped IPv6 addresses (`::ffff:x.x.x.x`) are unmapped before checking.

Environment variables set by administrators are **not** SSRF-checked (they are trusted configuration). Only user-supplied request body values go through validation.

Source: [api/deps.py](../services/anonymizer/src/api/deps.py#L77-L166).

### 4.4 Rate Limiting

slowapi rate limiting is enabled by default (`MEDANON_RATE_LIMIT_ENABLED=true`). When `MEDANON_REDIS_URL` is set, counters are shared across replicas via Redis DB 2. When Redis is unavailable, counters fall back to per-process memory (each replica has its own limit).

Configured limits per endpoint are declared in `api/main.py`. Exceeded limits return HTTP 429.

---

## 5. Container Hardening

All Docker Compose services use defense-in-depth hardening:

| Security control | Services |
|---|---|
| `read_only: true` | anonymizer, worker |
| `tmpfs` for writable paths | anonymizer (`/tmp`), worker (`/tmp`, `/output`) |
| `cap_drop: ALL` | anonymizer, worker, redis, app-db, nlp, analytics, ollama |
| `security_opt: no-new-privileges: true` | anonymizer, worker, redis, nlp, analytics, ollama, app-db |
| Non-root user | All Python services (UID 1000) |
| No host port for source FHIR | `fhir-server` — only reachable from `source-net` |

**Exception notes:**
- `app-db` (PostgreSQL): `cap_drop: ALL` omitted — PostgreSQL requires `CAP_CHOWN` and `CAP_SETUID` during startup. `no-new-privileges` is applied.
- `ollama`: `read_only` omitted — Ollama writes model files to its volume at runtime.

Memory limits (prevents OOM from impacting other containers):

| Service | Memory limit |
|---|---|
| anonymizer | 3 GB |
| worker | 2 GB |
| gPAS (WildFly) | 2.5 GB (`-Xms128M -Xmx1536M` + G1GC) |
| gpas-postgres | 2 GB |
| nlp (per replica) | 2 GB |

---

## 6. Audit Logging

### 6.1 What Is Logged

Every API request is recorded. The audit entry schema:

```json
{
  "ts": "2026-04-21T14:30:00Z",
  "event": "process",
  "method": "POST",
  "path": "/v1/process",
  "status": 200,
  "actor": "api-key-user",
  "auth_method": "api-key",
  "request_id": "req-abc123",
  "outcome": "success"
}
```

Event types: `process`, `job.create`, `job.complete`, `job.fail`, `config.change`, `auth.login`, `auth.deny`.

**What is NOT logged:** PHI, request/response bodies, raw API keys, bearer tokens. The audit log is safe to forward to a SIEM without PHI scrubbing.

### 6.2 Audit Log Destinations

Three concurrent destinations (all enabled simultaneously):

| Destination | When active | Notes |
|---|---|---|
| Structured JSON to stdout | Always | Captured by Docker/K8s log driver; forward to ELK, Splunk, Loki, etc. |
| Redis Stream `medanon:audit` | When `MEDANON_REDIS_URL` is set | Append-only; capped at `MEDANON_AUDIT_STREAM_MAXLEN` entries (default 50 000); queryable via `GET /v1/audit` (admin only) |
| Rotating file on disk | When `MEDANON_AUDIT_LOG_FILE` is set | 10 MB per file, 5 backups (50 MB total); configure with `MEDANON_AUDIT_LOG_MAX_BYTES` + `MEDANON_AUDIT_LOG_BACKUP_COUNT` |

Source: [utils/audit.py](../services/anonymizer/src/utils/audit.py).

### 6.3 Transformation Manifest (GDPR Art. 30)

When `MEDANON_MANIFEST_ENABLED=true`, each output FHIR resource carries a `meta.tag` entry recording every transformation applied:

```json
{
  "meta": {
    "tag": [{
      "system": "https://medanon.io/manifest",
      "code": "de-identified",
      "display": "Patient.id:gpas_pseudonymize | Patient.name:redact | Patient.birthDate:generalize(date_year)"
    }]
  }
}
```

This enables GDPR Art. 30 (Records of Processing Activities) compliance at the resource level. The manifest tag is stripped before uploading to the target FHIR server.

---

## 7. Data Categories & Processing Purposes

### 7.1 Data Categories Processed

SPE FHIR BlackBox processes **special category personal data** under GDPR Art. 9: health data in the form of FHIR R4 resources (Patient, Observation, Condition, MedicationRequest, etc.).

The system never creates derived profiles, enriches records, or retains identified data after processing. It is a **pass-through transformation engine** — data enters, is transformed, and exits.

### 7.2 Legitimate Processing Basis

| Use case | Legal basis |
|---|---|
| GDPR pseudonymization for secondary use | Art. 89(1) — research with appropriate safeguards |
| HIPAA Safe Harbor de-identification | 45 CFR §164.514(b) |
| IRB-approved research | Consent + Art. 9(2)(j) research purpose |
| Reversible pseudonymization (gPAS) | Consent or contractual necessity; TTP access controls ensure only authorized re-linkage |

### 7.3 Retention Periods

| Data | Default retention | Control |
|---|---|---|
| De-identified job results (NDJSON files) | Indefinite (no default TTL) | `MEDANON_RESULT_TTL_SEC` |
| Staged resources (two-phase bulk) | Indefinite | `MEDANON_STAGING_RETENTION_DAYS` |
| Processing run metadata (scoring) | Indefinite | Manual purge via `DELETE /v1/processing-runs` |
| Audit log (Redis Stream) | Last 50 000 events | `MEDANON_AUDIT_STREAM_MAXLEN` |
| gPAS pseudonym mappings | Indefinite (TTP-controlled) | gPAS domain lifecycle admin |

**Recommendation:** Set `MEDANON_RESULT_TTL_SEC=86400` (24 hours) in production unless downstream processes need longer access windows.

---

## 8. GDPR Compliance Mapping

| GDPR Article | Requirement | Implementation |
|---|---|---|
| Art. 4(5) | Pseudonymization | HMAC-SHA3-256 (`cryptohash`) or gPAS TTP pseudonyms per output profile |
| Art. 25 | Data protection by design | Minimum-data processing; rule-driven transformation; source FHIR server network-isolated |
| Art. 30 | Records of Processing Activities | Transformation manifest in `meta.tag` when `MEDANON_MANIFEST_ENABLED=true` |
| Art. 32 | Security of processing | Network segmentation, container hardening, encryption in transit (TLS at perimeter), HMAC for pseudonyms, audit logging |
| Art. 89(1) | Research safeguards | Pseudonymization + technical controls; access requires `analyst` or `admin` role |

### Profile-level GDPR coverage

| Profile | Art. 4(5) | Art. 25 | Art. 30 | Art. 32 |
|---|---|---|---|---|
| `config_gdpr_eu.yaml` | ✓ HMAC pseudonym | ✓ Name/contact fully redacted | ✓ Manifest | ✓ Audit + TLS |
| `config_gpas.yaml` | ✓ gPAS TTP pseudonym | ✓ Dates generalized | ✓ Manifest | ✓ Audit + TLS |
| `config_research_pseudonymous.yaml` | ✓ HMAC | ✓ Dates to year-month | ✓ Manifest | ✓ Audit + TLS |
| `config_hipaa_safe_harbor.yaml` | Partial (HMAC but primary goal is HIPAA) | ✓ 18 PHI categories removed | ✓ Manifest | ✓ Audit + TLS |
| `config_structure_preserving.yaml` | ✓ gPAS (IDs), `[REDACTED]` (PII) | Partial (structure retained by design) | ✓ Manifest | ✓ Audit + TLS |
| `config_value_masking.yaml` | ✓ Entity-conditional NLP | ✓ Field-level masking | ✓ Manifest | ✓ Audit + TLS |
| `config.yaml` (minimal) | Partial (HMAC) | Partial (regex-only scrubbing) | ✓ Manifest | ✓ Audit + TLS |

---

## 9. HIPAA Compliance

The `config_hipaa_safe_harbor.yaml` profile implements HIPAA Safe Harbor (45 CFR §164.514(b)) by removing or generalizing all 18 PHI categories:

| # | PHI Category | FHIR Field(s) | Action |
|---|---|---|---|
| 1 | Names | `Patient.name`, `*.text` | `redact` + NLP scrub |
| 2 | Geographic data (sub-state) | `Patient.address.city`, `.line` | `redact` |
| 3 | Dates (except year) | `*.birthDate`, `*.effectiveDateTime`, `*.date` | `generalize(date_year)` |
| 4 | Phone numbers | `Patient.telecom[phone]` | `redact` |
| 5 | Fax numbers | `Patient.telecom[fax]` | `redact` |
| 6 | Email addresses | `Patient.telecom[email]` | `redact` |
| 7 | Social security numbers | `*.identifier[ssn]` | `redact` |
| 8 | Medical record numbers | `*.identifier[mrn]` | `redact` |
| 9 | Health plan beneficiary numbers | `*.identifier[health-plan]` | `redact` |
| 10 | Account numbers | `*.identifier[account]` | `redact` |
| 11 | Certificate/license numbers | `*.identifier[license]` | `redact` |
| 12 | Vehicle identifiers | `*.identifier[vehicle]` | `redact` |
| 13 | Device identifiers | `Device.identifier` | `redact` |
| 14 | Web URLs | `*.identifier[uri]`, free text | `redact` + NLP scrub |
| 15 | IP addresses | Free text | NLP scrub |
| 16 | Biometric identifiers | Free text | NLP scrub |
| 17 | Full-face photographs | DICOM endpoint handles separately |
| 18 | Any other unique identifying number | `Patient.id` | `redact` |

Postal codes: generalized to 3-digit prefix (`generalize(zip_prefix 3)`).

**Note:** MedAnon covers categories 1–16 and 18 automatically. DICOM de-identification (category 17) is handled by the `/v1/process/dicom` endpoint.

---

## 10. Known Open Security Issues

The following issues are tracked in the architecture review ([docs/internal/ARCHITECTURE_REVIEW_2026_04_21.md](internal/ARCHITECTURE_REVIEW_2026_04_21.md)) and pending remediation:

| Issue | Severity | Description |
|---|---|---|
| PHI to external LLM | Critical | `pii_detector.py` has no code-level enforcement that `MEDANON_AI_PII_PROVIDER` is a local model. PHI could reach an external API if misconfigured. |
| Prompt injection in config generator | Critical | User-supplied config descriptions are interpolated directly into LLM messages in `config_generator.py`. |
| AI response cache unbounded | High | `LLMProvider._cache` has no eviction policy — memory grows without limit in long-running instances. |
| SSRF in AI proxy methods | High | `explain_config` and `advise_compliance` use `urllib.request` without the private-network guard applied to user-facing endpoints. |
| SSE streaming bypasses proxy | High | SSE path in `agents.py` router bypasses `AgentService` proxy logic and `AI_SERVICE_URL`. |
| FHIRPath eval errors swallowed | Medium | Errors in `rule_matcher.py` are caught silently even in `processing_errors: raise` mode. |
| Job poison retry loop | Medium | Failed jobs retry forever on crash recovery — no per-job retry limit. |
| `DELETE /v1/processing-runs` under-privileged | Low | Requires only `analyst` — should require `admin`. |

**Immediate mitigations while fixes are in progress:**

1. If using AI features (`MEDANON_AI_ENABLED=true`): ensure `MEDANON_AI_PII_PROVIDER` always points to the local Ollama instance. Never set it to an external API endpoint.
2. Do not expose the AI config-generation endpoint to untrusted users — it is `admin`-only by design.
3. Monitor `LLMProvider` memory usage in long-running containers; restart weekly if AI features are heavily used.
