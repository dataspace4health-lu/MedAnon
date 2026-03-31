"""Health check service — probes upstream dependencies."""

import logging
import os
import urllib.error
import urllib.request as _ureq

logger = logging.getLogger("medanon")


class HealthCheckService:
    """Probes configured upstream services (gPAS, FHIR, NLP) for readiness."""

    def check_readiness(self, timeout: float | None = None) -> dict[str, str]:
        """Probe all configured upstream services.

        Returns a dict of ``{service_name: "ok"|"error"}`` for each configured
        upstream.  Empty dict when no upstreams are configured.
        """
        if timeout is None:
            timeout = float(os.environ.get("MEDANON_READY_TIMEOUT", "5.0"))

        checks: dict[str, str] = {}

        gpas_url = os.environ.get("GPAS_URL", "")
        if gpas_url:
            checks["gpas"] = self._probe_gpas(gpas_url, timeout)

        fhir_url = os.environ.get("FHIR_SOURCE_URL", "")
        if fhir_url:
            checks["fhir"] = self._probe_fhir(fhir_url, timeout)

        nlp_model = os.environ.get("MEDANON_NLP_MODEL", "")
        if nlp_model:
            checks["nlp"] = self._probe_nlp()

        return checks

    def _probe_gpas(self, url: str, timeout: float) -> str:
        try:
            _ureq.urlopen(url.rstrip("/"), timeout=timeout)
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

    def _probe_nlp(self) -> str:
        try:
            from integrations.nlp.detector import _get_analyzer
            _get_analyzer()
            return "ok"
        except Exception as exc:
            logger.debug("readiness: nlp engine not ready: %s", exc)
            return "error"
