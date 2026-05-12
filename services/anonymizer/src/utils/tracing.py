"""OpenTelemetry tracing — opt-in distributed tracing for the anonymizer.

Activated when ``MEDANON_OTEL_ENABLED=true``. Otherwise this module is a no-op
so production deployments without an OTel collector pay zero overhead.

Environment variables (all standard OTLP):
    MEDANON_OTEL_ENABLED        — "true" to enable, anything else disables.
    OTEL_EXPORTER_OTLP_ENDPOINT — collector URL (default: http://otel-collector:4318).
    OTEL_SERVICE_NAME           — service name (default: medanon-anonymizer).
    OTEL_RESOURCE_ATTRIBUTES    — extra resource attributes (e.g. deployment.environment=prod).

Spans emitted automatically:
    - HTTP server spans for every FastAPI request (FastAPI instrumentation)
    - HTTP client spans for every gPAS / FHIR / NLP / analytics call (urllib3 instrumentation)
    - Log records correlated with active span via opentelemetry-instrumentation-logging

The setup is wrapped in a single ``try``: missing packages, network errors, or
collector outages must NEVER crash the anonymizer.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("medanon.tracing")


def setup_tracing(app) -> bool:
    """Initialize OpenTelemetry tracing on the FastAPI ``app``.

    Returns True if tracing was activated, False otherwise.
    Safe to call exactly once during application startup.
    """
    if os.environ.get("MEDANON_OTEL_ENABLED", "").lower() != "true":
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.logging import LoggingInstrumentor
        from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        logger.warning(
            "MEDANON_OTEL_ENABLED=true but opentelemetry packages are missing: %s. "
            "Tracing disabled.",
            exc,
        )
        return False

    try:
        service_name = os.environ.get("OTEL_SERVICE_NAME", "medanon-anonymizer")
        endpoint = os.environ.get(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"
        )
        # Allow either the bare collector base URL or a fully qualified traces URL.
        traces_endpoint = (
            endpoint
            if endpoint.endswith("/v1/traces")
            else endpoint.rstrip("/") + "/v1/traces"
        )

        resource = Resource.create({"service.name": service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint))
        )
        trace.set_tracer_provider(provider)

        # Auto-instrument FastAPI request handling.
        FastAPIInstrumentor.instrument_app(app)
        # Auto-instrument outgoing HTTP (gPAS / FHIR / NLP / analytics use urllib3).
        URLLib3Instrumentor().instrument()
        # Inject trace_id / span_id into every log record for correlation.
        LoggingInstrumentor().instrument(set_logging_format=False)

        logger.info(
            "OpenTelemetry tracing enabled (service=%s, endpoint=%s)",
            service_name,
            traces_endpoint,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — tracing must never crash the app
        logger.warning("OpenTelemetry setup failed; tracing disabled: %s", exc)
        return False
