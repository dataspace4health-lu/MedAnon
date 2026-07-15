"""Prometheus metrics for the MedAnon anonymizer service.

All metrics are module-level singletons  import and use directly:

    from utils.metrics import REQUEST_COUNT, GPAS_CACHE_HITS

Counters/histograms are only registered once per process, so importing this
module multiple times is safe.
"""

from prometheus_client import Counter, Gauge, Histogram

# ── HTTP request metrics ──────────────────────────────────────────────────────

REQUEST_COUNT = Counter(
    "medanon_requests_total",
    "Total number of HTTP requests received",
    ["endpoint", "status_code"],
)

REQUEST_LATENCY = Histogram(
    "medanon_request_duration_seconds",
    "HTTP request latency in seconds",
    ["endpoint"],
)

# ── gPAS client metrics ───────────────────────────────────────────────────────

GPAS_CALL_COUNT = Counter(
    "medanon_gpas_calls_total",
    "Total gPAS HTTP calls",
    ["operation", "status"],  # status: "ok" | "error"
)

GPAS_LATENCY = Histogram(
    "medanon_gpas_duration_seconds",
    "gPAS HTTP call latency in seconds",
    ["operation"],
)

GPAS_CACHE_HITS = Counter(
    "medanon_gpas_cache_hits_total",
    "gPAS pseudonym cache hits",
)

GPAS_CACHE_MISSES = Counter(
    "medanon_gpas_cache_misses_total",
    "gPAS pseudonym cache misses",
)

# Retained for Grafana dashboard compatibility. No longer incremented  the
# pipeline relies on the gPAS client's deterministic L1/L2 cache for
# cross-chunk dedup instead of an in-process seen-values set.
SEEN_VALUES_CAP_REACHED = Counter(
    "medanon_seen_values_cap_reached_total",
    "Times the in-process seen-values dedup set reached its cap (deprecated  always 0)",
)

# ── Rule conflicts ───────────────────────────────────────────────────────────
# Incremented when two config rules target the same resource path with a
# different action (e.g. a `redact` rule and a `mask` rule both matching
# Patient.telecom.value).  Surfaced statically by GET /v1/configs/{name}/conflicts
# and at runtime by the action dispatcher dedup loop.
RULE_CONFLICT_TOTAL = Counter(
    "medanon_rule_conflict_total",
    "Config rules in conflict (same path, different action)",
    ["path", "action_a", "action_b"],
)

# ── k-anonymity suppression (E1.4) ──────────────────────────────────────────
# Incremented once per suppressed resource in the privacy/apply.py path.
# ``resource_type`` lets operators see whether Patients or their linked
# clinical resources are being suppressed (linked suppression fires separately).
RESOURCES_SUPPRESSED = Counter(
    "medanon_resources_suppressed_total",
    "Resources dropped by k-anonymity suppression before de-identification",
    ["resource_type", "reason"],
)


# ── Action fallback (E1.1) ───────────────────────────────────────────────────
# Incremented when an action handler raises a recoverable error and the
# dispatcher falls back to redaction in "skip" mode.  ``reason`` is the
# exception type name so operators can see *why* an action failed instead of
# the failure being silently masked by a successful-looking redaction.
ACTION_FALLBACK = Counter(
    "medanon_action_fallback_total",
    "Times an action failed and the dispatcher fell back to redaction",
    ["action", "reason"],
)


# ── NLP detection cache ──────────────────────────────────────────────────────

NLP_CACHE_HITS = Counter(
    "medanon_nlp_cache_hits_total",
    "NLP detection cache hits (cross-run reuse)",
)

NLP_CACHE_MISSES = Counter(
    "medanon_nlp_cache_misses_total",
    "NLP detection cache misses (forwarded to NLP service)",
)

# ── Error PHI scrubbing (H1.3) ───────────────────────────────────────────────
# Incremented whenever _sanitize_error actually removed a PHI-looking token
# (UUID/date/email/phone/SSN/MRN) from an exception message before it was
# persisted to the job error field or audit log.  A rising rate flags action
# handlers that interpolate raw values into exception messages.
ERROR_PHI_SCRUBBED = Counter(
    "medanon_error_phi_scrubbed_total",
    "Exception messages from which PHI-looking tokens were scrubbed",
)

# ── Async audit queue (F.18) ─────────────────────────────────────────────────
# Incremented when the bounded async-audit queue is full and an event's
# off-hot-path sinks (Redis Stream / S3) are dropped. The authoritative stdout
# JSON line is still written synchronously, so this counts degraded-durability
# events, not lost records.
AUDIT_DROPPED = Counter(
    "medanon_audit_dropped_total",
    "Audit events whose async (Redis/S3) sinks were dropped due to a full queue",
)

# ── NLP L2 (Redis) detection cache (E5.6) ────────────────────────────────────
# The NLP microservice keeps an optional Redis-backed L2 cache for detection
# results (NLP_REDIS_URL).  These mirror the service-side counters so the
# anonymizer's /metrics scrape reports L2 effectiveness alongside L1.
NLP_L2_CACHE_HITS = Counter(
    "medanon_nlp_l2_cache_hits_total",
    "NLP L2 (Redis) detection cache hits",
)

NLP_L2_CACHE_MISSES = Counter(
    "medanon_nlp_l2_cache_misses_total",
    "NLP L2 (Redis) detection cache misses",
)

# ── Per-stage pipeline latency ───────────────────────────────────────────────
# Stage labels:
#   "rule_evaluation"   FHIRPath evaluation + de-identification rule dispatch (parallel per resource)
#   "phi_detection"     NLP batch PHI detection + text replacement
#   "pseudonymization"  gPAS batch identifier pseudonymization (HTTP lookup)
#   "resource_assembly" pseudonym write-back + reference rewriting (parallel per resource)
# Note: phi_detection and pseudonymization run concurrently  their wall-clock
# durations overlap; summing them does not yield the critical path.

PIPELINE_STAGE_LATENCY = Histogram(
    "medanon_pipeline_stage_duration_seconds",
    "Wall-clock seconds spent in each pipeline stage per chunk",
    ["stage"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# ── FHIR server client metrics ────────────────────────────────────────────────

FHIR_CALL_COUNT = Counter(
    "medanon_fhir_calls_total",
    "Total FHIR server HTTP calls",
    [
        "operation",
        "status",
        "role",
    ],  # status: "ok" | "error"; role: "source" | "target"
)

FHIR_LATENCY = Histogram(
    "medanon_fhir_duration_seconds",
    "FHIR server HTTP call latency in seconds",
    ["operation", "role"],
)

# ── Async job queue metrics ───────────────────────────────────────────────────

JOB_QUEUE_DEPTH = Gauge(
    "medanon_job_queue_pending",
    "Number of pending messages in the Redis Streams job queue (medanon:job_stream).",
)

# ── Circuit breaker observability ─────────────────────────────────────────────

# Numeric encoding lets dashboards alert on "any breaker != 0 for > 1m".
#   0 = closed, 1 = half_open, 2 = open
CIRCUIT_BREAKER_STATE = Gauge(
    "medanon_circuit_breaker_state",
    "Circuit breaker state per integration (0=closed, 1=half_open, 2=open).",
    ["name"],
)

CIRCUIT_BREAKER_TRIPS = Counter(
    "medanon_circuit_breaker_trips_total",
    "Total CLOSED→OPEN transitions per circuit breaker.",
    ["name"],
)

# ── FHIRPath cache observability ─────────────────────────────────────────────

# Compile / classification / where-plan / candidate-expansion caches in
# ``pipeline/rule_matcher``.  Sampled on demand from ``cache_info()`` rather
# than instrumented per-call (zero hot-path overhead).
FHIRPATH_CACHE_HITS = Gauge(
    "medanon_fhirpath_cache_hits",
    "FHIRPath LRU cache hit count (sampled from functools.lru_cache.cache_info).",
    ["cache"],
)
FHIRPATH_CACHE_MISSES = Gauge(
    "medanon_fhirpath_cache_misses",
    "FHIRPath LRU cache miss count.",
    ["cache"],
)
FHIRPATH_CACHE_SIZE = Gauge(
    "medanon_fhirpath_cache_size",
    "FHIRPath LRU cache current entry count.",
    ["cache"],
)
FHIRPATH_CACHE_MAXSIZE = Gauge(
    "medanon_fhirpath_cache_maxsize",
    "FHIRPath LRU cache configured maxsize.",
    ["cache"],
)

# ── Outbound HTTP retries (proxy_request) ────────────────────────────────────

# Counts retry attempts grouped by upstream service and outcome so dashboards
# can flag flapping integrations long before the circuit breaker trips.
PROXY_RETRIES = Counter(
    "medanon_proxy_retries_total",
    "Total HTTP retry attempts emitted by integrations/http_client.",
    ["upstream", "reason"],  # reason: "http_5xx" | "http_429" | "connection"
)

# ── Per-upstream bulkheads (utils/bulkhead) ──────────────────────────────────

BULKHEAD_ACQUIRED = Counter(
    "medanon_bulkhead_acquired_total",
    "Bulkhead slots successfully acquired per upstream.",
    ["upstream"],
)
BULKHEAD_REJECTED = Counter(
    "medanon_bulkhead_rejected_total",
    "Bulkhead acquisition rejections (saturation) per upstream.",
    ["upstream"],
)

# ── Build / version info ──────────────────────────────────────────────────────

# Set once at startup with version + git_sha labels; value is always 1.
# Lets ops correlate metric anomalies with deployments.
BUILD_INFO = Gauge(
    "medanon_build_info",
    "Anonymizer service build metadata (always 1; labels carry the data).",
    ["version", "git_sha"],
)

# ── Worker process metrics (exposed by dedicated worker container) ────────────

WORKER_JOBS_TOTAL = Counter(
    "medanon_worker_jobs_total",
    "Total jobs processed by the worker, by type and outcome",
    ["job_type", "status"],  # status: "done" | "error" | "cancelled"
)

WORKER_JOB_DURATION = Histogram(
    "medanon_worker_job_duration_seconds",
    "End-to-end job execution time in seconds",
    ["job_type"],
    buckets=(5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)

WORKER_ACTIVE_JOBS = Gauge(
    "medanon_worker_active_jobs",
    "Number of jobs currently executing in the worker",
)

# ── Scoring engine metrics ───────────────────────────────────────────────────
# The SCORE_* handles moved to medanon-core scoring._metrics (the scoring engine
# is now a shared package and owns its own optional metrics). Imported there,
# they still register with the default Prometheus registry and appear on /metrics.

# ── Output-barrier / QC quality observability ────────────────────────────────
# De-identification *quality* signals, distinct from latency/throughput. Before
# these, gate blocks and quarantines were observable only by grepping
# log.warning  you could not alert on or graph "leak rate spiked".

# Every output-gate verdict. ``gate``: "raw_pii" (always-on raw-resource scan)
# | "score_summary" (opt-in scoring gate). ``decision``: "pass" | "block".
GATE_DECISIONS = Counter(
    "medanon_gate_decisions_total",
    "Output-barrier verdicts by gate and decision",
    ["gate", "decision"],
)

# Residual personal identifiers detected in de-identified output, by category
# and severity. A non-zero rate means PHI is slipping past the rules into the
# gate  a precision/recall signal for the de-identification config itself.
PHI_LEAK_DETECTED = Counter(
    "medanon_phi_leak_detected_total",
    "Residual personal-identifier detections in output, by type and severity",
    ["type", "severity"],
)

# Resources quarantined (emitted as a __quarantined marker, never unprocessed).
# ``stage``: where the failure was caught; ``reason``: exception type name.
QUARANTINE_TOTAL = Counter(
    "medanon_quarantine_total",
    "Resources quarantined after a processing failure (never emitted unprocessed)",
    ["stage", "reason"],
)

# NLP fail-closed events  when PHI detection could not run and the pipeline
# redacted instead of leaking. ``reason``: "adapter_unavailable" (no NLP
# configured) | "unavailable_token" (remote call failed → [NLP_UNAVAILABLE]).
# A rising rate means PHI-bearing text is being mass-redacted, not scrubbed.
NLP_FALLBACK_TOTAL = Counter(
    "medanon_nlp_fallback_total",
    "NLP fail-closed redaction events by reason",
    ["reason"],
)

# ── RabbitMQ macro-stage streaming (opt-in: MEDANON_AMQP_URL) ─────────────────
AMQP_PUBLISHED = Counter(
    "medanon_amqp_published_total",
    "Stage messages published to RabbitMQ",
    ["stage"],
)
AMQP_CONSUMED = Counter(
    "medanon_amqp_consumed_total",
    "Stage messages consumed from RabbitMQ, by outcome",
    ["stage", "outcome"],  # outcome: done | skip | retry | dead
)
AMQP_REDELIVERED = Counter(
    "medanon_amqp_redelivered_total",
    "Stage messages redelivered (retry queue → main)",
    ["stage"],
)
AMQP_PUBLISH_FAILURES = Counter(
    "medanon_amqp_publish_failures_total",
    "Publish attempts that failed confirm/timed out",
)
AMQP_DLQ = Counter(
    "medanon_amqp_dlq_total",
    "Stage messages routed to a dead-letter queue",
    ["stage"],
)
