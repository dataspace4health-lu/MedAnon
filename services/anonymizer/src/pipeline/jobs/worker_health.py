"""Lightweight health server for the standalone worker process.

Serves ``/health``, ``/ready``, and ``/metrics`` on the Prometheus metrics
port (default 9091).  Replaces ``prometheus_client.start_http_server()`` so
that a single port handles both health probes and metrics exposition.
"""

from __future__ import annotations

import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("medanon.worker_health")


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
        from api.services.health import HealthCheckService

        checks = HealthCheckService().check_readiness(timeout=3.0)
        ready = all(v == "ok" for v in checks.values())
        import json

        body = json.dumps({"ready": ready, "checks": checks}).encode()
        self._respond(200 if ready else 503, body)

    def _handle_metrics(self) -> None:
        try:
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

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
