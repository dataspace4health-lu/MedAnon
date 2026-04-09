MedAnon — Senior Architect Honest Assessment
Date: 2026-04-09 | Branch: Phase-3_ConfigProfile Verdict: Solid research/dev tool. Not production-ready for PHI. 23 issues found, 7 critical.

Part I: Technology Stack — Honest Verdicts
Every choice gets one of: KEEP (right choice), 

KEEP WITH FIXES (right choice, wrong execution), 

REPLACE (wrong choice).

1. Python 3.12 — KEEP

Criterion	Python 3.12	Go 1.22	Java 21	Node.js 20
FHIR tooling	fhirpathpy, fhir.resources	fhir-models (basic)	HAPI FHIR (gold standard)	fhirpath.js (reference)
NLP/PII detection	Presidio + spaCy (mature)	None	OpenNLP (limited)	compromise (basic)
Async web	FastAPI (native async)	net/http (goroutines)	Spring WebFlux	Express/Fastify
Memory/worker	~80 MB	~15 MB	~200 MB	~50 MB
Healthcare ecosystem	Strong	Weak	Strongest	Moderate
Why it's the right choice: Only runtime where Presidio + spaCy + FHIRPath + async web coexist natively. Go has no NLP. Java has HAPI FHIR but no Presidio. Node lacks both.

The honest downside: Python's GIL limits true CPU parallelism. Our thread pool model (32 threads) is a workaround, not a solution. Go's goroutines would be 10x more efficient for the I/O-heavy gPAS/FHIR transport layer.

If starting over: Python for pipeline + NLP, Go sidecar for HTTP transport. But the cost of that split doesn't justify it at our scale.

2. FastAPI + Pydantic v2 — KEEP
Criterion	FastAPI	Django REST	Flask	Litestar
Async native	Yes	Partial (4.1+)	No	Yes
Auto OpenAPI	Yes	Via drf-spectacular	Via flask-smorest	Yes
Validation	Pydantic v2 (Rust)	DRF serializers	Marshmallow	msgspec/attrs
Perf (req/s)	~12K	~3K	~4K	~15K
Why it's right: Pydantic v2's Rust core gives us near-Go validation speed. Auto OpenAPI docs. Native async. 45+ endpoints would be painful without it.

Honest downside: Litestar is 20% faster and has better dependency injection. But FastAPI's ecosystem (community, tutorials, hiring pool) is 10x larger.

3. Redis (Cache + Job Queue) — KEEP WITH FIXES
Criterion	Redis 7	PostgreSQL	RabbitMQ	Celery+Redis
Job queue	Streams (XREADGROUP)	SKIP LOCKED	Native AMQP	Mature, battle-tested
L2 cache	Built-in	Unorthodox	No	Via Redis
Ops complexity	Low (single binary)	Already have it	New dependency	+Celery overhead
Durability	AOF/RDB (configurable)	WAL (bulletproof)	Mirrored queues	Depends on broker
Why Redis is right for cache: L2 pseudonym cache with TTL. Fast. Simple.

Why Redis is WRONG for job queue — the honest truth:

volatile-lru eviction policy can delete in-progress job hashes. Job hashes have TTLs. Under memory pressure, Redis evicts them. The worker loses job state mid-execution. This is a data loss bug in production.
Status index sets (job:status:running) grow unbounded. Members are added but orphaned when their job hash is evicted. list_jobs returns phantom entries.
cancel() is not atomic. Read-then-write race between status check and update. Two concurrent cancels can corrupt state.
No authentication. Any container on processing-net can read/write Redis — including job data and cached pseudonyms.
What should change:

Redis for cache: KEEP (it's good at this)
Redis for job queue: REPLACE with PostgreSQL SKIP LOCKED or Celery. We already have PostgreSQL (for HAPI). Using it for the job queue eliminates the eviction/durability risk, gives us transactional status updates, and removes the orphan index problem. Alternatively, adopt Celery for a battle-tested task queue with retry policies, dead-letter queues, and monitoring (Flower).
4. SQLite Job Store (Fallback) — REPLACE
Current: Fallback when Redis is unavailable. New connection per operation. Non-atomic dequeue (select-then-update race). No connection pooling.

Verdict: REPLACE. SQLite is not suitable for concurrent job processing:

next_pending() + update(status=running) is not atomic — two workers grab the same job
New sqlite3.connect() on every operation (no pooling)
Multi-container mode over shared Docker volume is unreliable
Schema migration swallows errors with bare except Exception: pass
Replace with: PostgreSQL (already in the stack for HAPI). Single UPDATE ... WHERE status='pending' RETURNING * is atomic dequeue. Connection pooling via asyncpg. Works across multiple containers natively.

5. urllib3 + Thread Pool HTTP — REPLACE
Criterion	urllib3 + threads	httpx (async)	aiohttp	requests
Async native	No (needs thread pool)	Yes	Yes	No
Connection pooling	Yes	Yes	Yes	Per-session
HTTP/2	No	Yes	No	No
Thread consumption	1 per request	0	0	1 per request
Current problem: All HTTP calls (gPAS, FHIR, analytics, NLP) go through urllib3 in a shared 32-thread pool. I/O-bound gPAS calls (up to 30s timeout each) and CPU-bound FHIRPath evaluation share the same pool. Under concurrent load, gPAS calls starve FHIRPath work.

time.sleep() in gPAS retry backoff blocks a thread for up to 2.4 seconds of pure sleeping. Multiply by sub-batches across concurrent jobs = thread pool exhaustion.

Replace with: httpx async client for all HTTP integrations. Eliminates thread pool for I/O. Supports HTTP/2. Uses asyncio.sleep() for backoff (non-blocking). Keep the thread pool only for CPU-bound FHIRPath evaluation.

6. Single API Key Auth — REPLACE
Current: One API key in MEDANON_API_KEY env var. All key holders get admin role. Key stored in browser localStorage (XSS-exfiltrable). No rotation without restart. Open mode gives admin to everyone with no warning.

This is not production-grade for a PHI system. Period.

Criterion	Current (single key)	Keycloak/OIDC	OAuth2 + JWT	API key per user
Per-user identity	No	Yes	Yes	Partial
MFA	No	Yes	Depends on IdP	No
Audit trail	"api-key-user"	Real user ID	Real user ID	Key ID
Key rotation	Container restart	Token refresh	Token refresh	Hot reload
HIPAA compliant	No	Yes	Yes	Partial
Replace with: Keycloak (or any OIDC provider). Per-user identity for audit trails. Token-based auth with refresh. MFA support. Role mapping from IdP claims. Short-lived JWTs eliminate the localStorage credential exposure.

At minimum (quick fix): Support multiple API keys with different roles via a JSON config map. Add brute-force protection.

7. Nginx (UI Proxy) — KEEP WITH FIXES
Good: X-Frame-Options, X-Content-Type-Options, Referrer-Policy headers present.

Critical gaps:

No Content-Security-Policy (CSP). XSS can exfiltrate PHI. This is the #1 missing header.
No HSTS. Required for production TLS.
/fhir/ proxy gives unauthenticated browser access to source FHIR server containing identified PHI. Any browser user can query patient data directly.
Static asset Cache-Control headers override security headers (nginx add_header inheritance quirk).
No rate limiting at nginx level.
8. FHIRPath Hybrid Evaluator — KEEP
Good design decision. Native evaluator handles 96% of rules (simple dot-paths, wildcards, .where() filters). fhirpathpy handles the remaining 4% (complex expressions). This avoids the heavyweight antlr4 parser for common cases.

Not over-engineered. Each evaluator type is 20-40 lines. The classification is clean.

9. Presidio + spaCy (NLP/PII) — KEEP WITH FIXES
Right choice. On-premise NLP avoids sending PHI to cloud APIs. Presidio is the only mature open-source PII detection library. spaCy en_core_web_lg provides 86% F1 on clinical text.

The fix needed: The NLP skip fail mode (MEDANON_NLP_FAIL_MODE=skip) silently returns unscrubbed text. This is a configurable PHI leak. Remove skip mode entirely — redact should be the only fallback.

10. Docker Compose (Dev/Staging) — KEEP WITH FIXES
Good (above average): cap_drop: ALL, no-new-privileges, read_only rootfs, two-network segmentation, resource limits, health checks on all services.

Bad:

gPAS, HAPI, and database containers run as root with no capability restrictions
Source FHIR server (identified PHI) exposed on host port 8081
Redis has no authentication
All secrets in .env file (visible in docker inspect, /proc/1/environ)
No TLS between services — PHI transits unencrypted on Docker bridge network
MinIO default credentials (minioadmin/minioadmin)
11. Helm Charts (Kubernetes) — KEEP WITH FIXES
Good: SecurityContext, NetworkPolicy, ConfigMap checksums, Prometheus annotations, probes.

Missing entirely:

No HorizontalPodAutoscaler (stuck at replicas: 1)
No PodDisruptionBudget (cluster upgrade = total outage)
No Ingress template (values reference routing but no template renders it)
No dedicated worker Deployment (relies on embedded worker in anonymizer pod)
tag: latest as default (should be pinned version)
Secrets in plain Kubernetes Secrets (not Sealed Secrets or Vault)
12. CI/CD Pipeline — INADEQUATE, NEEDS MAJOR WORK
What exists: ruff lint, partial unit tests, Docker build (no push), helm lint.

What's missing for a PHI system:

No SAST (Bandit, Semgrep, CodeQL)
No dependency vulnerability scanning (Trivy, Snyk, Dependabot)
No container image scanning
No secrets scanning (gitleaks, trufflehog)
No integration tests in CI
No client-side CI (no TypeScript check, no ESLint, no React tests)
No code coverage enforcement
No end-to-end tests
No deploy pipeline
13. Observability — INADEQUATE
What exists: Prometheus counters/histograms for HTTP, gPAS, FHIR. Audit log to rotating file. Request ID propagation.

What's missing:

No distributed tracing (OpenTelemetry/Jaeger) — can't trace a request across anonymizer/worker/gPAS/FHIR
No structured application logging (only audit log is JSON; app logs are human-readable)
No metrics for: NLP calls, Redis operations, pipeline throughput, error rates by type
No alerting rules or Grafana dashboards
No SLO/SLI definitions
Audit log is a local mutable file — non-compliant with HIPAA (must be immutable, centrally stored, retained 6 years)
14. Other Technologies — Quick Verdicts
Technology	Role	Verdict
PyYAML + safe_load	Config parsing	KEEP — but env-var interpolation needs sandboxing
defusedxml	XML parsing	KEEP — prevents XXE attacks
orjson	JSON hot paths	KEEP — 3-10x faster than stdlib json
PyCryptodome (RSA-OAEP)	Encrypt/decrypt action	KEEP — industry standard
HMAC-SHA3-256	Cryptohash action	KEEP WITH FIX — JSON serialization not canonical (sort_keys=True needed)
slowapi	Rate limiting	KEEP — but add nginx-level rate limiting too
Docker multi-stage build	Image optimization	KEEP — clean 4-stage design
ruff	Linting/formatting	KEEP — fastest Python linter
pytest	Testing	KEEP — but coverage and concurrency tests missing
Part II: Quality Scoring System — Honest Assessment
The Implementation
Files: client/src/lib/qualityScore.ts, client/src/lib/piiDetection.ts

How it works:

Score = (PII Coverage × 0.60) + (Action Strength × 0.25) + (Error Rate × 0.15)

PII Coverage:  What % of HIPAA-sensitive fields were transformed
Action Strength: How strong the transformations were (redact=1.0, generalize=0.7, scrub_text=0.5)
Error Rate:    What % of resources processed without errors

Grade: A≥90 | B≥75 | C≥60 | D≥40 | F<40
Three-layer PII detection:

Manifest tags (if MEDANON_MANIFEST_ENABLED=true) — most reliable
Pattern matching (regex for [REDACTED], hex hashes, [[TOKEN_N]], year-only dates)
Field diffing (compare original vs. de-identified when both available)
What's Wrong With It — 4 Critical Flaws
Flaw 1: Empty Data = Grade A (CRITICAL)
const coverage = sensitivePresent > 0
  ? sensitiveTreated / sensitivePresent
  : 1.0; // ← THIS LINE
When no sensitive fields are detected (empty field summary, manifest disabled, no originals for diffing), sensitivePresent = 0 and coverage defaults to 100%. A completely unprocessed dataset gets Grade A.

Impact: Operator sees "Grade A: 100%" on data that was never de-identified. Releases PHI thinking it's clean.

Fix: When sensitivePresent = 0, set coverage to 0.0 and display "Unable to assess — insufficient data".

Flaw 2: Error Rate Has Almost No Impact (HIGH)
const raw = coverage * 0.60 + strength * 0.25 + errorRate * 0.15;
With 15% weight, error rate barely affects the score. A dataset where 50% of resources failed (and thus contain unscrubbed PHI):

Score = 1.0 × 0.60 + 1.0 × 0.25 + 0.5 × 0.15 = 0.925 → Grade A
Half the resources failed processing, system reports Grade A.

Fix: Error rate should be a gate, not a weighted component. Any error rate >5% caps the grade at C. Any error rate >20% caps at F.

Flaw 3: Incomplete HIPAA Coverage (HIGH)
KNOWN_SENSITIVE covers 5 resource types: Patient, Practitioner, Organization, RelatedPerson, Person, plus * wildcard for id and text.

Missing from HIPAA Safe Harbor 18 identifiers:

Device.identifier (device serial numbers)
AuditEvent.agent.network (IP addresses)
Endpoint.address (URLs)
Account.identifier (account numbers)
Coverage.identifier (health plan numbers)
Media resources (biometric data)
A dataset with un-scrubbed device serial numbers or network addresses gets a passing grade.

Flaw 4: Heuristic False Positives Inflate Scores (MEDIUM)
The regex classifiers have false positive risks:

\d{4} (year) also matches 4-digit lab results, zip codes, room numbers
\d{4}-\d{2} (year-month) matches any string in that format
64-char hex matches legitimate hash-like identifiers
These inflate the strength component, making grades higher than warranted.

Part III: Critical Vulnerabilities Found
Tier 0 — Fix Before Any Production Use
#	Issue	File	Impact
1	Source FHIR server (identified PHI) exposed via nginx /fhir/ proxy with no auth	client/nginx.conf	Any browser user queries identified patient data directly
2	Quality score reports Grade A on empty/unprocessed data	qualityScore.ts:154	PHI released under false sense of de-identification
3	NLP skip mode silently returns unscrubbed free text	deidentify.py:102	Configurable PHI leak via env var
4	API key stored in browser localStorage	client/src/api/client.ts:17	XSS exfiltrates auth credential
5	Redis has no authentication	docker-compose.yml:565	Any container reads pseudonym cache + job data
6	FHIRPath eval errors silently skip rules in "skip" mode	action_dispatcher.py:96	Broken FHIRPath expression = PHI pass-through, only a warning log
7	scrub_text.py global token state holds PHI indefinitely in reverse map	scrub_text.py:227	Process memory dump exposes all previously processed PHI
Tier 1 — Fix Before Staging
#	Issue	File	Impact
8	No Content-Security-Policy header	nginx.conf	XSS vector for PHI exfiltration
9	Single shared admin API key, no per-user identity	auth.py	HIPAA audit trail says "api-key-user" for everyone
10	volatile-lru evicts in-progress job hashes	docker-compose.yml	Job state lost mid-execution under memory pressure
11	Thread pool shared between I/O and CPU work	thread_pool.py, processor.py	Thread exhaustion under concurrent load
12	HMAC JSON serialization not canonical	cryptohash.py:17	Same value hashes differently across systems
13	SSRF: pagination allows origin-hopping redirects	_transport.py:212	Compromised FHIR server reaches internal services
14	Audit log is a local mutable file	main.py	Non-compliant with HIPAA audit requirements
15	Error rate barely affects quality score (15% weight)	qualityScore.ts:168	50% failures still get Grade A
16	gPAS/HAPI/DB containers run as root	docker-compose.yml	Container escape risk
Tier 2 — Fix Before Production
#	Issue	File	Impact
17	No encryption at rest or in transit between services	Infrastructure	PHI exposed on disk and Docker bridge
18	No SAST/dependency scanning in CI	.github/workflows/	Vulnerable code ships undetected
19	No distributed tracing	Missing	Can't debug production issues across services
20	No HPA/PDB in Helm charts	helm/	Zero availability guarantees on K8s
21	No backup/DR strategy	Infrastructure	Data loss risk
22	date.today() in generalize leaks processing date	generalize.py:61	Re-identification signal
23	Config env-var interpolation not sandboxed	config/loader.py:67	User configs can exfiltrate MEDANON_HASH_KEY
Part IV: What To Do Next — Prioritized Roadmap
Phase 1: Critical Security (1-2 weeks)
Fix quality score empty-data default — change 1.0 to 0.0, add "Unable to assess" UI state
Remove NLP skip mode — redact is the only safe fallback
Add Redis authentication — --requirepass in compose, update all clients
Add CSP header to nginx — script-src 'self'; style-src 'self' 'unsafe-inline'
Add auth to nginx FHIR proxies — require API key for /fhir/ and /fhir-target/
Clear scrub_text global token state after each request (call reset_global_token_state())
Make error rate a gate in quality score — >5% caps at C, >20% caps at F
Fix FHIRPath silent skip — use fallback redaction instead of skip on eval errors
Phase 2: Auth + Observability (2-3 weeks)
Replace localStorage API key with httpOnly cookie or session token
Support multiple API keys with roles (interim before IdP)
Add SAST to CI (Bandit + Semgrep) and dependency scanning (Trivy)
Add structured JSON logging for all application logs
Add OpenTelemetry tracing for cross-service request tracking
Fix HMAC canonical serialization — json.dumps(sort_keys=True, separators=(',',':'))
Bind source FHIR host port to 127.0.0.1 only — 127.0.0.1:${HAPI_PORT:-8081}:8080
Phase 3: Infrastructure Hardening (3-4 weeks)
Replace Redis job queue with PostgreSQL SKIP LOCKED (or adopt Celery)
Replace urllib3 thread pool HTTP with httpx async for gPAS/FHIR/analytics
Add mTLS between services (or at minimum TLS for FHIR + gPAS)
Implement centralized immutable audit logging (ELK or CloudWatch)
Add HPA, PDB, and Ingress templates to Helm charts
Add worker Deployment to Helm chart (separate from anonymizer pod)
Integrate Keycloak (or OIDC provider) for per-user identity
Phase 4: Production Readiness (4-6 weeks)
Database encryption at rest (LUKS volumes or cloud-managed encryption)
Backup automation (pg_dump cron, Redis RDB snapshots, S3 lifecycle)
End-to-end test suite (Playwright for UI, integration tests in CI with Docker)
Expand KNOWN_SENSITIVE to all HIPAA Safe Harbor categories across all FHIR resource types
Add reference_date param to generalize (deterministic age brackets)
Sandbox config env-var interpolation (allowlist of interpolatable variables)
Config snapshot at job submission (prevent mid-job config mutation)
Part V: Technology Summary Card
Layer	Current	Verdict	Target
Language	Python 3.12	KEEP	Python 3.12
Web Framework	FastAPI + Pydantic v2	KEEP	FastAPI + Pydantic v2
HTTP Transport	urllib3 + ThreadPool	REPLACE	httpx (async)
Job Queue	Redis Streams	REPLACE	PostgreSQL SKIP LOCKED or Celery
Job Store Fallback	SQLite	REPLACE	PostgreSQL
Cache L2	Redis	KEEP	Redis (with auth)
Auth	Single API key	REPLACE	Keycloak/OIDC
NLP/PII	Presidio + spaCy	KEEP (fix fail mode)	Presidio + spaCy
FHIRPath	Hybrid evaluator	KEEP	Hybrid evaluator
Config	PyYAML + env interpolation	KEEP (sandbox it)	PyYAML + allowlist
Observability	Prometheus only	UPGRADE	Prometheus + OpenTelemetry + JSON logs
CI/CD	Lint + partial tests	UPGRADE	+ SAST + Trivy + integration tests + E2E
Audit	Local file	REPLACE	Centralized immutable store
Infra (Docker)	Compose + partial hardening	KEEP (fix gaps)	+ Redis auth + TLS + non-root DBs
Infra (K8s)	Helm (minimal)	KEEP (add HPA/PDB)	+ HPA + PDB + Ingress + worker
Bottom line: The pipeline architecture (rule matching, action dispatch, gPAS integration, hybrid FHIRPath) is well-designed and the right approach. The problems are all in the surrounding infrastructure: auth, secrets, observability, CI/CD, and the quality scoring system that gives false confidence. Fix the 7 Tier-0 issues and you have a credible staging system. Complete Phases 1-3 and you have a production-grade healthcare platform.