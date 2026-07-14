"""Health check service  probes upstream dependencies."""

import logging
import os
import urllib.error
import urllib.request as _ureq
from concurrent.futures import TimeoutError as _FuturesTimeoutError, as_completed

from utils.thread_pool import get_executor

logger = logging.getLogger("medanon")

# Checks that gate the ``/ready`` boolean (and therefore the k8s readinessProbe
# in helm/charts/anonymizer). A failure here means this process cannot serve
# de-identification traffic correctly, so pulling it from the load balancer is
# the right response.
#
# Everything NOT listed here is advisory: it is probed and reported in
# ``checks`` for observability, but it must never flip ``/ready`` to 503.
# The analytics/scoring/trust-gate/AI services are optional strangler-fig
# extractions that degrade gracefully, so gating readiness on them would let a
# non-essential dependency evict the core engine from service.
CRITICAL_CHECKS = frozenset({"gpas", "fhir", "fhir_target", "redis", "nlp", "postgres"})


class HealthCheckService:
    """Probes configured upstream services for readiness.

    Critical upstreams (gPAS, FHIR, Redis, NLP, Postgres) gate readiness;
    optional microservices (analytics, scoring, Trust Gate, AI) are reported
    but advisory. See :data:`CRITICAL_CHECKS`.
    """

    def check_readiness(
        self, timeout: float | None = None, critical_only: bool = False
    ) -> dict[str, str]:
        """Probe all configured upstream services **in parallel**.

        Returns a dict of ``{service_name: "ok"|"error"|"timeout"}`` for each
        configured upstream.  Empty dict when no upstreams are configured.

        Worst-case latency is bounded by the single slowest probe (~timeout)
        rather than the sum of all probes.

        ``critical_only=True`` skips the advisory microservice probes entirely.
        The worker's health server gates only on :data:`CRITICAL_CHECKS`, so
        probing a slow-to-time-out ``trust_gate`` there just inflated ``/ready``
        latency past the healthcheck client's own timeout, flapping the
        container to unhealthy while every job still ran.
        """
        if timeout is None:
            timeout = float(os.environ.get("MEDANON_READY_TIMEOUT", "5.0"))

        probes: dict[str, tuple] = {}

        gpas_url = os.environ.get("GPAS_URL", "")
        if gpas_url:
            probes["gpas"] = (self._probe_gpas, (gpas_url, timeout))

        fhir_url = os.environ.get("FHIR_SOURCE_URL", "")
        if fhir_url:
            probes["fhir"] = (self._probe_fhir, (fhir_url, timeout))

        fhir_target_url = os.environ.get("FHIR_TARGET_URL", "")
        if fhir_target_url:
            probes["fhir_target"] = (self._probe_fhir, (fhir_target_url, timeout))

        redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
        if redis_url:
            probes["redis"] = (self._probe_redis, (redis_url, timeout))

        nlp_url = os.environ.get("NLP_SERVICE_URL", "").strip()
        if nlp_url:
            probes["nlp"] = (self._probe_health, (nlp_url, timeout))

        app_db_url = os.environ.get("MEDANON_APP_DB_URL", "").strip()
        if app_db_url:
            probes["postgres"] = (self._probe_postgres, (timeout,))

        # Advisory microservices  each exposes GET /health and is only probed
        # when its URL is configured. Reported, never readiness-gating, and
        # skipped entirely for a critical-only probe (they only add latency).
        if not critical_only:
            for name, env_var in (
                ("analytics", "ANALYTICS_SERVICE_URL"),
                ("scoring", "SCORING_SERVICE_URL"),
                ("trust_gate", "TRUST_GATE_SERVICE_URL"),
                ("ai", "AI_SERVICE_URL"),
            ):
                url = os.environ.get(env_var, "").strip()
                if url:
                    probes[name] = (self._probe_health, (url, timeout))

        if not probes:
            return {}

        checks: dict[str, str] = {}
        executor = get_executor()
        futures = {
            executor.submit(fn, *args): name for name, (fn, args) in probes.items()
        }
        try:
            for fut in as_completed(futures, timeout=timeout + 2):
                try:
                    checks[futures[fut]] = fut.result()
                except Exception as exc:
                    logger.debug("readiness: probe raised: %s", exc)
                    checks[futures[fut]] = "error"
        except _FuturesTimeoutError:
            # One or more probes didn't finish in time  mark them so /ready
            # returns 503 instead of letting the TimeoutError propagate as 500.
            for fut, name in futures.items():
                if name not in checks:
                    logger.debug("readiness: probe timed out: %s", name)
                    checks[name] = "timeout"
        return checks

    def _probe_gpas(self, url: str, timeout: float) -> str:
        try:
            # GPAS_URL is the FHIR operation base (e.g. http://host:port/ttp-fhir/fhir/gpas).
            # Strip the trailing /gpas segment to reach the FHIR server root and probe
            # /metadata  avoids the "Unknown resource type 'gpas'" WARN logged by WildFly
            # when the old /gpas/gpasService?wsdl suffix was appended to the FHIR path.
            fhir_base = url.rstrip("/").rsplit("/", 1)[0]
            probe = f"{fhir_base}/metadata"
            _ureq.urlopen(probe, timeout=timeout)  # nosec B310
            return "ok"
        except urllib.error.HTTPError:
            # Any HTTP response (400, 401, etc.) means the service is reachable
            return "ok"
        except Exception as exc:
            logger.debug("readiness: gpas unreachable: %s", exc)
            return "error"

    def _probe_fhir(self, url: str, timeout: float) -> str:
        try:
            _ureq.urlopen(f"{url.rstrip('/')}/metadata", timeout=timeout)  # nosec B310
            return "ok"
        except Exception as exc:
            logger.debug("readiness: fhir unreachable: %s", exc)
            return "error"

    def _probe_redis(self, url: str, timeout: float) -> str:
        try:
            from utils.redis_pool import get_redis

            # Reuse the process-wide pool instead of constructing a new client
            # on every /health probe (k8s hits this every 10 s).
            client = get_redis(url, decode_responses=True)
            if client is None:
                return "error"
            client.ping()
            return "ok"
        except Exception as exc:
            logger.debug("readiness: redis unreachable: %s", exc)
            return "error"

    def _probe_health(self, url: str, timeout: float) -> str:
        """Probe a service's ``GET /health``.

        Strict: any non-2xx response raises and is reported as ``error``. The
        de-identification microservices all answer 200 when healthy.
        """
        try:
            probe = url.rstrip("/") + "/health"
            _ureq.urlopen(probe, timeout=timeout)  # nosec B310
            return "ok"
        except Exception as exc:
            logger.debug("readiness: %s unreachable: %s", url, exc)
            return "error"

    def _probe_postgres(self, timeout: float) -> str:
        try:
            from integrations.postgres.pool import get_pool, pool_health, safe_putconn

            info = pool_health()
            # Lazy-init: when a URL is configured but no caller has touched the
            # pool yet (typical for the worker process when Redis is the job
            # store and the staging store creates its own pool), force a
            # one-shot probe so /ready doesn't flap to 503.
            if not info.get("available"):
                app_db_url = os.environ.get("MEDANON_APP_DB_URL", "").strip()
                if not app_db_url:
                    return "ok"  # nothing configured → not a failure
                pool = get_pool(app_db_url)
                conn = pool.getconn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                finally:
                    safe_putconn(pool, conn)
                return "ok"

            if info.get("ping") == "ok":
                return "ok"
            return "error"
        except Exception as exc:
            logger.debug("readiness: postgres unreachable: %s", exc)
            return "error"
