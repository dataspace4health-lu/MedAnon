"""HTTP client for the terminology server sidecar.

Calls the FHIR ``CodeSystem/$validate-code`` operation to confirm a (system,
code) pair is valid. The client is a process-wide singleton so its (system,
code) cache and circuit-breaker state persist across requests: common codes
(LOINC vitals, SNOMED problem list) repeat heavily across batches, so a
per-request client would re-issue the same ``$validate-code`` round-trips on
every batch. Fail-soft: unreachable terminology → :class:`TerminologyUnavailable`
so the domain marks itself "not assessed" instead of failing or false-passing.
"""

from __future__ import annotations

import json
import logging
import os
from threading import Lock

import urllib3

from breaker import CircuitBreaker

_log = logging.getLogger("trust_gate.terminology")

_HTTP: urllib3.PoolManager | None = None


def _http() -> urllib3.PoolManager:
    global _HTTP
    if _HTTP is None:
        _HTTP = urllib3.PoolManager(num_pools=4, maxsize=8, retries=False)
    return _HTTP


class TerminologyUnavailable(RuntimeError):
    """Raised when the terminology server cannot be reached or errored."""


class TerminologyClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._cache: dict[tuple[str, str], bool] = {}
        self._cache_lock = Lock()
        self._breaker = CircuitBreaker(
            name="trust_gate_terminology",
            failure_threshold=int(
                os.environ.get("TRUST_GATE_TERMINOLOGY_CB_THRESHOLD", "5")
            ),
            recovery_timeout_sec=float(
                os.environ.get("TRUST_GATE_TERMINOLOGY_CB_RECOVERY_SEC", "30")
            ),
        )

    def validate_code(self, system: str, code: str) -> bool:
        """Return True when (system, code) is valid per the terminology server."""
        key = (system, code)
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]

        if not self._breaker.allow_request():
            raise TerminologyUnavailable("circuit breaker open")

        url = f"{self._base_url}/CodeSystem/$validate-code"
        fields = {"system": system, "code": code}
        try:
            resp = _http().request(
                "GET",
                url,
                fields=fields,
                headers={"Accept": "application/fhir+json"},
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001
            self._breaker.record_failure()
            raise TerminologyUnavailable(f"transport error: {exc}") from exc

        if resp.status >= 500:
            self._breaker.record_failure()
            raise TerminologyUnavailable(f"upstream {resp.status}")

        try:
            params = json.loads(resp.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._breaker.record_failure()
            raise TerminologyUnavailable(f"invalid JSON: {exc}") from exc

        self._breaker.record_success()
        result = _extract_result(params)
        with self._cache_lock:
            self._cache[key] = result
        return result


def _extract_result(params: dict) -> bool:
    """Pull the boolean ``result`` from a Parameters response."""
    if not isinstance(params, dict):
        return False
    for p in params.get("parameter", []):
        if isinstance(p, dict) and p.get("name") == "result":
            return bool(p.get("valueBoolean", False))
    return False


_CLIENT: TerminologyClient | None = None
_CLIENT_KEY: tuple[str, float] | None = None
_CLIENT_LOCK = Lock()


def get_terminology_client() -> TerminologyClient | None:
    """Return the process-wide terminology client (``None`` when unconfigured).

    Reuses a singleton keyed on (url, timeout) so the code cache and breaker
    state survive across requests; rebuilds only if the env config changes.
    """
    url = os.environ.get("TRUST_GATE_TERMINOLOGY_URL", "").strip()
    if not url:
        return None
    timeout = float(os.environ.get("TRUST_GATE_TERMINOLOGY_TIMEOUT_SEC", "10"))
    key = (url, timeout)
    global _CLIENT, _CLIENT_KEY
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_KEY != key:
            _CLIENT = TerminologyClient(url, timeout=timeout)
            _CLIENT_KEY = key
        return _CLIENT
