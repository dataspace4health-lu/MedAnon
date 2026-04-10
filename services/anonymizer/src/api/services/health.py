"""Health check service — probes upstream dependencies."""

import logging
import os
import urllib.error
import urllib.request as _ureq
from concurrent.futures import as_completed

from utils.thread_pool import get_executor

logger = logging.getLogger("medanon")


class HealthCheckService:
    """Probes configured upstream services (gPAS, FHIR, NLP) for readiness."""

    def check_readiness(self, timeout: float | None = None) -> dict[str, str]:
        """Probe all configured upstream services **in parallel**.

        Returns a dict of ``{service_name: "ok"|"error"}`` for each configured
        upstream.  Empty dict when no upstreams are configured.

        Worst-case latency is bounded by the single slowest probe (~timeout)
        rather than the sum of all probes.
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

        nlp_model = os.environ.get("MEDANON_NLP_MODEL", "")
        if nlp_model:
            probes["nlp"] = (self._probe_nlp, ())

        if not probes:
            return {}

        checks: dict[str, str] = {}
        executor = get_executor()
        futures = {
            executor.submit(fn, *args): name for name, (fn, args) in probes.items()
        }
        for fut in as_completed(futures, timeout=timeout + 2):
            checks[futures[fut]] = fut.result()
        return checks

    def _probe_gpas(self, url: str, timeout: float) -> str:
        try:
            # GPAS_URL is the FHIR operation base (e.g. http://host:port/ttp-fhir/fhir/gpas).
            # Strip the trailing /gpas segment to reach the FHIR server root and probe
            # /metadata — avoids the "Unknown resource type 'gpas'" WARN logged by WildFly
            # when the old /gpas/gpasService?wsdl suffix was appended to the FHIR path.
            fhir_base = url.rstrip("/").rsplit("/", 1)[0]
            probe = f"{fhir_base}/metadata"
            _ureq.urlopen(probe, timeout=timeout)
            return "ok"
        except urllib.error.HTTPError:
            # Any HTTP response (400, 401, etc.) means the service is reachable
            return "ok"
        except Exception as exc:
            logger.debug("readiness: gpas unreachable: %s", exc)
            return "error"

    def _probe_fhir(self, url: str, timeout: float) -> str:
        try:
            _ureq.urlopen(f"{url.rstrip('/')}/metadata", timeout=timeout)
            return "ok"
        except Exception as exc:
            logger.debug("readiness: fhir unreachable: %s", exc)
            return "error"

    def _probe_redis(self, url: str, timeout: float) -> str:
        try:
            import redis as _redis

            client = _redis.StrictRedis.from_url(
                url, socket_connect_timeout=timeout, socket_timeout=timeout
            )
            client.ping()
            return "ok"
        except Exception as exc:
            logger.debug("readiness: redis unreachable: %s", exc)
            return "error"

    def _probe_nlp(self) -> str:
        try:
            from integrations.nlp.detector import _get_analyzer

            _get_analyzer()
            return "ok"
        except Exception as exc:
            logger.debug("readiness: nlp engine not ready: %s", exc)
            return "error"
