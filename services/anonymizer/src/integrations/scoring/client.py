"""HTTP client for the standalone scoring microservice.

Mirrors the analytics/NLP integration pattern: a thin urllib3 client with
configurable timeout, single-shot retry and circuit-breaker fallback to local
in-process scoring on failure. Returns a ``ScoreResult`` dataclass so callers
do not need to know whether scoring ran locally or remotely.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import urllib3

from pipeline.scoring.models import (
    Evidence,
    ModuleScore,
    PrivacyDecision,
    ScoreResult,
)
from utils.circuit_breaker import CircuitBreaker

_log = logging.getLogger(__name__)

_SCORING_URL_ENV = "SCORING_SERVICE_URL"
_TIMEOUT_ENV = "SCORING_TIMEOUT_SEC"
_DEFAULT_TIMEOUT = 10.0

_HTTP: urllib3.PoolManager | None = None


def _http() -> urllib3.PoolManager:
    global _HTTP
    if _HTTP is None:
        _HTTP = urllib3.PoolManager(num_pools=4, maxsize=8, retries=False)
    return _HTTP


def _decode_privacy(d: dict) -> PrivacyDecision:
    return PrivacyDecision(
        risk_score=float(d.get("risk_score", 0.0)),
        passed=bool(d.get("passed", False)),
        threshold=float(d.get("threshold", 0.0)),
        attacker_risk=float(d.get("attacker_risk", 0.0)),
        identifier_risk=float(d.get("identifier_risk", 0.0)),
        text_risk=float(d.get("text_risk", 0.0)),
        config_identifier_risk=float(d.get("config_identifier_risk", 0.0)),
        evidence=[
            Evidence(
                check=e.get("check", ""),
                value=float(e.get("value", 0.0)),
                details=e.get("details", {}),
                severity=e.get("severity", "info"),
            )
            for e in d.get("evidence", [])
        ],
    )


def _decode_module(d: dict | None) -> ModuleScore | None:
    if not d:
        return None
    return ModuleScore(
        name=d.get("name", ""),
        score=float(d.get("score", 0.0)),
        evidence=[
            Evidence(
                check=e.get("check", ""),
                value=float(e.get("value", 0.0)),
                details=e.get("details", {}),
                severity=e.get("severity", "info"),
            )
            for e in d.get("evidence", [])
        ],
        gates_applied=list(d.get("gates_applied", [])),
    )


def _decode_score_result(payload: dict) -> ScoreResult:
    return ScoreResult(
        composite=float(payload.get("composite", 0.0)),
        decision=str(payload.get("decision", "FAIL")),
        privacy=_decode_privacy(payload.get("privacy") or {}),
        utility=_decode_module(payload.get("utility")),
        quality=_decode_module(payload.get("quality")),
        resource_type=payload.get("resource_type", "Unknown"),
        resource_id=payload.get("resource_id"),
        scored_at=payload.get("scored_at", ScoreResult.now_iso()),
        config_profile=payload.get("config_profile", "auto"),
    )


class RemoteScoringClient:
    """HTTP client for the scoring microservice.

    Failures route through a circuit breaker. When the breaker is OPEN or the
    request fails, ``score()`` raises ``ScoringRemoteError`` so the caller can
    decide whether to fall back to local scoring.
    """

    def __init__(self, base_url: str, timeout: float = _DEFAULT_TIMEOUT) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._breaker = CircuitBreaker(
            name="scoring",
            failure_threshold=int(os.environ.get("SCORING_CB_FAILURE_THRESHOLD", "5")),
            recovery_timeout_sec=float(
                os.environ.get("SCORING_CB_RECOVERY_TIMEOUT_SEC", "30")
            ),
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    def score(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        config_profile: str = "auto",
        error_count: int = 0,
        total_count: int = 1,
    ) -> ScoreResult:
        if not self._breaker.allow_request():
            raise ScoringRemoteError("circuit breaker open")

        body = {
            "original": original,
            "deidentified": deidentified,
            "manifest_entries": manifest_entries,
            "config_profile": config_profile,
            "error_count": error_count,
            "total_count": total_count,
        }
        try:
            resp = _http().request(
                "POST",
                f"{self._base_url}/v1/score",
                body=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
            )
        except Exception as exc:
            self._breaker.record_failure()
            raise ScoringRemoteError(f"transport error: {exc}") from exc

        if resp.status >= 500:
            self._breaker.record_failure()
            raise ScoringRemoteError(f"upstream {resp.status}")
        if resp.status >= 400:
            # 4xx is a client/contract error — do not trip the breaker.
            raise ScoringRemoteError(f"client error {resp.status}: {resp.data[:200]!r}")

        try:
            payload: dict[str, Any] = json.loads(resp.data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._breaker.record_failure()
            raise ScoringRemoteError(f"invalid JSON: {exc}") from exc

        self._breaker.record_success()
        return _decode_score_result(payload)


class ScoringRemoteError(RuntimeError):
    """Raised when the remote scoring service is unavailable or errored."""


def get_remote_scoring_client() -> RemoteScoringClient | None:
    """Return a configured client when ``SCORING_SERVICE_URL`` is set."""
    url = os.environ.get(_SCORING_URL_ENV, "").strip()
    if not url:
        return None
    timeout = float(os.environ.get(_TIMEOUT_ENV, str(_DEFAULT_TIMEOUT)))
    return RemoteScoringClient(url, timeout=timeout)
