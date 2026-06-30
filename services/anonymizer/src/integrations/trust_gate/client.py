"""HTTP client for the standalone Trust Gate microservice.

Mirrors ``integrations/scoring/client.py``: a thin urllib3 client with a
configurable timeout and a circuit breaker. The Trust Gate assesses data quality
*before* privacy transformation and returns a Quality Passport (decision +
per-domain scores). On failure the caller (``pipeline/intake_gate``) degrades to
an advisory CONDITIONAL_PASS rather than blocking or false-passing.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import urllib3

from utils.circuit_breaker import CircuitBreaker

_log = logging.getLogger(__name__)

_TRUST_GATE_URL_ENV = "TRUST_GATE_SERVICE_URL"
_TIMEOUT_ENV = "TRUST_GATE_TIMEOUT_SEC"
_DEFAULT_TIMEOUT = 15.0

_HTTP: urllib3.PoolManager | None = None


def _http() -> urllib3.PoolManager:
    global _HTTP
    if _HTTP is None:
        _HTTP = urllib3.PoolManager(num_pools=4, maxsize=8, retries=False)
    return _HTTP


class TrustGateRemoteError(RuntimeError):
    """Raised when the remote Trust Gate service is unavailable or errored."""


class TrustGateClient:
    """HTTP client for the Trust Gate microservice.

    Failures route through a circuit breaker. When the breaker is OPEN or the
    request fails, ``assess_batch()`` raises ``TrustGateRemoteError`` so the
    caller can decide how to degrade.
    """

    def __init__(self, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._breaker = CircuitBreaker(
            name="trust_gate",
            failure_threshold=int(
                os.environ.get("TRUST_GATE_CB_FAILURE_THRESHOLD", "5")
            ),
            recovery_timeout_sec=float(
                os.environ.get("TRUST_GATE_CB_RECOVERY_TIMEOUT_SEC", "30")
            ),
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def assess_batch(
        self,
        resources: list[dict],
        dataset_id: str = "dataset",
        source_types: list[str] | None = None,
        config_profile: str = "auto",
        provenance: dict | None = None,
        phases: list[str] | None = None,
        targets: list[dict] | None = None,
        intended_use: str | None = None,
        use_case: str | None = None,
        full_urls: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST a batch of resources, returning the Quality Passport dict.

        ``phases`` selects which Trust Gate audit phases run (None → all);
        ``targets`` requests per-sector verdicts (None → no sector breakdown);
        ``intended_use`` makes the fitness verdict purpose-bound; ``use_case``
        resolves to a metric subset server-side (explicit phases still win).
        ``full_urls`` carries Bundle entry.fullUrl values so the gate can resolve
        intra-bundle urn:uuid / absolute references (else reference integrity is NA).
        """
        if not self._breaker.allow_request():
            raise TrustGateRemoteError("circuit breaker open")

        body = {
            "resources": resources,
            "dataset_id": dataset_id,
            "source_types": source_types or ["fhir"],
            "config_profile": config_profile,
            "provenance": provenance or {},
        }
        if full_urls:
            body["full_urls"] = full_urls
        if phases is not None:
            body["phases"] = phases
        if targets is not None:
            body["targets"] = targets
        if intended_use is not None:
            body["intended_use"] = intended_use
        if use_case is not None:
            body["use_case"] = use_case
        try:
            resp = _http().request(
                "POST",
                f"{self._base_url}/v1/trust/assess/batch",
                body=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except Exception as exc:
            self._breaker.record_failure()
            raise TrustGateRemoteError(f"transport error: {exc}") from exc

        if resp.status >= 500:
            self._breaker.record_failure()
            raise TrustGateRemoteError(f"upstream {resp.status}")
        if resp.status >= 400:
            # 4xx is a client/contract error — do not trip the breaker.
            raise TrustGateRemoteError(
                f"client error {resp.status}: {resp.data[:200]!r}"
            )

        try:
            payload: dict[str, Any] = json.loads(resp.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._breaker.record_failure()
            raise TrustGateRemoteError(f"invalid JSON: {exc}") from exc

        self._breaker.record_success()
        return payload


def get_trust_gate_client() -> TrustGateClient | None:
    """Return a configured client when ``TRUST_GATE_SERVICE_URL`` is set."""
    url = os.environ.get(_TRUST_GATE_URL_ENV, "").strip()
    if not url:
        return None
    timeout = float(os.environ.get(_TIMEOUT_ENV, str(_DEFAULT_TIMEOUT)))
    return TrustGateClient(url, timeout=timeout)
