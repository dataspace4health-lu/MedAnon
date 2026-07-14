"""Prometheus metrics for the HTTP surface (shared by the routers)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "trust_gate_requests_total",
    "Number of assess requests by outcome",
    ["endpoint", "outcome"],
)
LATENCY = Histogram(
    "trust_gate_request_seconds", "Latency of assess requests", ["endpoint"]
)
DECISIONS = Counter("trust_gate_decisions_total", "Passport decisions", ["decision"])
