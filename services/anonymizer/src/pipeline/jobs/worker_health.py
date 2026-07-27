"""Lightweight health server for the standalone worker process.

Serves ``/health``, ``/ready``, and ``/metrics`` on the Prometheus metrics
port (default 9091).  Replaces ``prometheus_client.start_http_server()`` so
that a single port handles both health probes and metrics exposition.

``/ready`` reports on the worker's **upstreams** (gPAS, Redis, the FHIR servers)
*and* on its own consume loop.  It used to report only the former, so Docker
called the container healthy for ten minutes while the loop raised on every
iteration and every claimed job was stranded in the Redis pending-entries list.
A worker that cannot consume is not ready, whatever its upstreams say.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

logger = logging.getLogger("medanon.worker_health")

# The consume loop and a dedicated background task both refresh this file (see
# ``jobs.worker._touch_heartbeat``). The background task ticks every 15 s, so a
# file older than this means the loop is wedged, the process is gone, or the
# output directory is not writable. Generous enough not to flap on a slow tick.
_HEARTBEAT_MAX_AGE_SEC = float(
    os.environ.get("MEDANON_WORKER_HEARTBEAT_MAX_AGE_SEC", "60")
)


def _heartbeat_check() -> str:
    """``ok`` | ``stale: …`` | ``missing: …`` for the worker's own consume loop."""
    path = Path(
        os.path.join(os.environ.get("MEDANON_OUTPUT_DIR", "/output"), "worker_healthy")
    )
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return f"missing: {path} was never written (is /output writable by this uid?)"
    except OSError as exc:
        return f"missing: {exc}"
    if age > _HEARTBEAT_MAX_AGE_SEC:
        return f"stale: last beat {age:.0f}s ago (max {_HEARTBEAT_MAX_AGE_SEC:.0f}s)"
    return "ok"


class _Handler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for health/ready/metrics."""

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._respond(200, b'{"status":"ok"}')
        elif self.path == "/ready":
            self._handle_ready()
        elif self.path == "/metrics":
            self._handle_metrics()
        else:
            self._respond(404, b"Not Found")

    def _handle_ready(self) -> None:
        from pipeline.health import CRITICAL_CHECKS, HealthCheckService

        # ``critical_only`` + a tight per-probe timeout keep the whole response
        # inside the healthcheck client's 3 s window (docker-compose.yml). Gating
        # on the advisory services *and* probing them was a double bug: a slow
        # ``trust_gate`` both failed the ``all(...)`` gate and pushed the probe
        # past 3 s, so the client disconnected (BrokenPipe) and the container
        # flapped to unhealthy while every job still ran.
        checks = HealthCheckService().check_readiness(timeout=2.0, critical_only=True)
        # The worker's own liveness, not just its upstreams'. Without this a
        # wedged consume loop reports healthy while the queue silently backs up.
        worker_loop = _heartbeat_check()
        checks["worker_loop"] = worker_loop

        # Gate on the critical upstreams (mirrors the API's /ready) plus the
        # consume loop.
        ready = worker_loop == "ok" and all(
            v == "ok" for k, v in checks.items() if k in CRITICAL_CHECKS
        )
        import json

        body = json.dumps({"ready": ready, "checks": checks}).encode()
        self._respond(200 if ready else 503, body)

    def _handle_metrics(self) -> None:
        try:
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

            # The FHIRPath caches expose *gauges*, refreshed by reading each
            # lru_cache's cache_info() at scrape time (the same thing the API's
            # /metrics does). Without this the medanon_fhirpath_cache_* series
            # are missing from the worker entirely  and the worker is the
            # process that runs every bulk export, where rule_evaluation is the
            # dominant stage once the gPAS/NLP caches are warm.
            try:
                from pipeline.rule_matcher import sample_cache_metrics

                sample_cache_metrics()
            except Exception:  # noqa: BLE001  never fail a scrape on observability
                logger.debug("fhirpath_cache_sample_failed", exc_info=True)

            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.end_headers()
            self.wfile.write(data)
        except ImportError:
            self._respond(501, b"prometheus_client not installed")

    def _respond(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        # Suppress default stderr logging for health check spam
        pass


def start_worker_health_server(port: int = 9091) -> HTTPServer | None:
    """Start the health server in a daemon thread.  Returns the HTTPServer."""
    if port <= 0:
        return None
    server = HTTPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("worker_health_server started port=%d", port)
    return server
