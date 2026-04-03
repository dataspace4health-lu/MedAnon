"""Remote NLP detector — calls the NLP microservice over HTTP.

Drop-in replacement for the local ``_analyze_and_replace`` calls when
``NLP_SERVICE_URL`` is set. The calling interface in ``nlp_detect_by_path``
remains unchanged; the choice of local vs. remote is made at call time.
"""

from __future__ import annotations

from utils.json_fast import dumps_bytes as _json_dumps_bytes
import logging
import os
import threading
import time

from integrations.http_client import proxy_post_json

_log = logging.getLogger("medanon.nlp.remote")


# ---------------------------------------------------------------------------
# Lightweight NLP circuit breaker (same pattern as gPAS)
# ---------------------------------------------------------------------------

class _NlpCircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._threshold = int(os.environ.get("NLP_CB_FAILURE_THRESHOLD", "5"))
        self._recovery_timeout = float(os.environ.get("NLP_CB_RECOVERY_TIMEOUT_SEC", "60"))
        self._window_start = 0.0
        self._window = float(os.environ.get("NLP_CB_WINDOW_SEC", "120"))

    def allow_request(self) -> bool:
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._last_failure_time >= self._recovery_timeout:
                    self._state = self.CLOSED
                    self._failure_count = 0
                    _log.info("nlp_circuit_breaker state=closed (recovery)")
                    return True
                return False
            return True

    @property
    def stats(self) -> dict:
        """Return a snapshot for health checks."""
        with self._lock:
            return {
                "state": self._state,
                "failure_count": self._failure_count,
                "threshold": self._threshold,
                "recovery_timeout_sec": self._recovery_timeout,
            }

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            now = time.time()
            if now - self._window_start > self._window:
                self._failure_count = 0
                self._window_start = now
            self._failure_count += 1
            self._last_failure_time = now
            if self._failure_count >= self._threshold:
                self._state = self.OPEN
                _log.warning(
                    "nlp_circuit_breaker state=open failures=%d threshold=%d",
                    self._failure_count, self._threshold,
                )


_nlp_cb = _NlpCircuitBreaker()


def nlp_circuit_breaker_stats() -> dict:
    """Return NLP circuit breaker stats snapshot for health/readiness checks."""
    return _nlp_cb.stats


def nlp_circuit_breaker_is_open() -> bool:
    """Return True if the NLP circuit breaker is currently OPEN."""
    return _nlp_cb.stats["state"] == _NlpCircuitBreaker.OPEN


def _nlp_service_url(path: str) -> str:
    base = os.environ.get("NLP_SERVICE_URL", "").rstrip("/")
    return f"{base}{path}"


def analyze_and_replace_remote(
    text: str,
    entities: list[str],
    threshold: float,
    language: str,
    mode: str,
    token_state: dict,
) -> str:
    """Call POST /v1/detect on the NLP service and return the scrubbed text.

    Token state is sent with the request and updated in-place from the response
    so that deterministic surrogate tokens remain consistent across multiple
    fields of the same resource.

    Falls back to the original *text* unchanged if the NLP service is
    unreachable — processing continues rather than failing hard.
    **Warning**: returning unscrubbed text means PHI may leak through.
    """
    if not _nlp_cb.allow_request():
        _log.warning(
            "nlp_circuit_breaker OPEN — returning unscrubbed text (PHI leak risk)"
        )
        return text

    payload = _json_dumps_bytes({
        "text": text,
        "entities": entities,
        "threshold": threshold,
        "language": language,
        "mode": mode,
        "token_state": token_state,
    })

    url = _nlp_service_url("/v1/detect")
    try:
        result = proxy_post_json(url, payload, timeout=30)
        returned_state = result.get("token_state", {})
        token_state.update(returned_state)
        _nlp_cb.record_success()
        return result["scrubbed_text"]
    except Exception as exc:
        _nlp_cb.record_failure()
        _log.warning(
            "nlp_service_error type=%s — returning unscrubbed text (PHI leak risk)",
            type(exc).__name__,
        )
        return text


def analyze_and_replace_batch_remote(
    texts: list[str],
    entities: list[str],
    threshold: float,
    language: str,
    mode: str,
    token_state: dict,
) -> list[str]:
    """Batch NLP detection — send multiple texts in one HTTP round-trip.

    Falls back to per-text sequential calls if the batch endpoint fails
    (e.g. NLP service version doesn't support ``/v1/detect/batch``).
    """
    if not texts:
        return []

    if not _nlp_cb.allow_request():
        _log.warning(
            "nlp_circuit_breaker OPEN — returning %d unscrubbed texts (PHI leak risk)",
            len(texts),
        )
        return list(texts)

    payload = _json_dumps_bytes({
        "items": [
            {
                "text": t,
                "entities": entities,
                "threshold": threshold,
                "language": language,
                "mode": mode,
            }
            for t in texts
        ],
        "token_state": token_state,
    })

    url = _nlp_service_url("/v1/detect/batch")
    try:
        result = proxy_post_json(url, payload, timeout=60)
        returned_state = result.get("token_state", {})
        token_state.update(returned_state)
        scrubbed = result.get("results", [])
        if len(scrubbed) == len(texts):
            _nlp_cb.record_success()
            return scrubbed
        _log.warning("nlp batch response length mismatch: expected %d, got %d", len(texts), len(scrubbed))
    except Exception as exc:
        _nlp_cb.record_failure()
        _log.warning("nlp_batch_error type=%s — falling back to sequential", type(exc).__name__)

    # Fallback: sequential per-text calls
    return [
        analyze_and_replace_remote(t, entities, threshold, language, mode, token_state)
        for t in texts
    ]
