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
    ["operation", "status"],   # status: "ok" | "error"
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

# ── FHIR server client metrics ────────────────────────────────────────────────

FHIR_CALL_COUNT = Counter(
    "medanon_fhir_calls_total",
    "Total FHIR server HTTP calls",
    ["operation", "status"],   # status: "ok" | "error"
)

FHIR_LATENCY = Histogram(
    "medanon_fhir_duration_seconds",
    "FHIR server HTTP call latency in seconds",
    ["operation"],
)

# ── Async job queue metrics ───────────────────────────────────────────────────

JOB_QUEUE_DEPTH = Gauge(
    "medanon_job_queue_pending",
    "Number of pending messages in the Redis Streams job queue (medanon:job_stream).",
)

# ── Worker process metrics (exposed by dedicated worker container) ────────────

WORKER_JOBS_TOTAL = Counter(
    "medanon_worker_jobs_total",
    "Total jobs processed by the worker, by type and outcome",
    ["job_type", "status"],   # status: "done" | "error" | "cancelled"
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
