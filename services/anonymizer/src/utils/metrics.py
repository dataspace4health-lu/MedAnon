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
