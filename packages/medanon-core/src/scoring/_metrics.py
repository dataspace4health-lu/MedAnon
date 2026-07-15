"""Optional Prometheus metrics for the scoring engine.

The engine is a leaf package, so it owns its metric handles here (optionally
backed by ``prometheus_client``) rather than importing them from the anonymizer's
``utils.metrics``. When ``prometheus_client`` is absent - e.g. in a slim consumer
that did not install the ``metrics`` extra - the handles are no-op stubs and
scoring runs unchanged. ``_HAS_METRICS`` reports which path is active.
"""

from __future__ import annotations

try:
    from prometheus_client import Counter, Histogram

    _HAS_METRICS = True
except ImportError:  # prometheus_client not installed in this consumer
    _HAS_METRICS = False


if _HAS_METRICS:
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
else:

    class _NoOpMetric:
        """Stand-in exposing the Counter/Histogram surface the engine calls."""

        def labels(self, *args, **kwargs) -> "_NoOpMetric":
            return self

        def observe(self, *args, **kwargs) -> None:
            pass

        def inc(self, *args, **kwargs) -> None:
            pass

    SCORE_COMPOSITE = _NoOpMetric()
    SCORE_PRIVACY_RISK = _NoOpMetric()
    SCORE_DECISIONS = _NoOpMetric()
    SCORE_DURATION = _NoOpMetric()
