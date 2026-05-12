"""Prometheus metrics for the MedAnon anonymizer service.

All metrics are module-level singletons — import and use directly:

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

# Cross-chunk in-process dedup capacity exhausted.  When this counter is
# non-zero the pipeline fell back to relying on L1/L2 cache only for dedup,
# which still preserves correctness but increases gPAS round-trips.
SEEN_VALUES_CAP_REACHED = Counter(
    "medanon_seen_values_cap_reached_total",
    "Times the in-process seen-values dedup set has reached its cap",
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

# ── Per-stage pipeline latency (separates fetch / nlp / gpas / write) ────────

PIPELINE_STAGE_LATENCY = Histogram(
    "medanon_pipeline_stage_duration_seconds",
    "Wall-clock seconds spent in each pipeline stage per chunk",
    ["stage"],  # stage: "fetch" | "actions" | "nlp" | "gpas" | "post" | "write"
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

# ── FHIR server client metrics ────────────────────────────────────────────────

FHIR_CALL_COUNT = Counter(
    "medanon_fhir_calls_total",
    "Total FHIR server HTTP calls",
    ["operation", "status", "role"],  # status: "ok" | "error"; role: "source" | "target"
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

SCORE_COMPOSITE = Histogram(
    "medanon_score_composite",
    "Composite de-identification score (0-100)",
    ["resource_type", "decision"],
    buckets=(0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100),
)

SCORE_PRIVACY_RISK = Histogram(
    "medanon_score_privacy_risk",
    "Privacy risk score (0.0-1.0, lower is better)",
    ["resource_type"],
    buckets=(0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.5, 0.75, 1.0),
)

SCORE_DECISIONS = Counter(
    "medanon_score_decisions_total",
    "Total scoring decisions by outcome",
    ["decision"],
)

SCORE_DURATION = Histogram(
    "medanon_score_duration_seconds",
    "Time spent scoring a single resource",
    ["resource_type"],
    buckets=(0.0001, 0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0),
)
