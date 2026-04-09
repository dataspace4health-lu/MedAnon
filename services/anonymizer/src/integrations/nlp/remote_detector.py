"""Remote NLP detector — calls the NLP microservice over HTTP.

Drop-in replacement for the local ``_analyze_and_replace`` calls when
``NLP_SERVICE_URL`` is set. The calling interface in ``nlp_detect_by_path``
remains unchanged; the choice of local vs. remote is made at call time.
"""

from __future__ import annotations

from utils.json_fast import dumps_bytes as _json_dumps_bytes
import logging
import os

from integrations.http_client import proxy_post_json
from utils.circuit_breaker import CircuitBreaker

_log = logging.getLogger("medanon.nlp.remote")

# NLP failure mode: always fail-closed (redact) to prevent PHI leakage.
_NLP_FALLBACK_TEXT = "[NLP_UNAVAILABLE]"


# ---------------------------------------------------------------------------
# Three-state NLP circuit breaker (shared implementation)
# ---------------------------------------------------------------------------

_nlp_cb = CircuitBreaker(
    name="nlp",
    failure_threshold=int(os.environ.get("NLP_CB_FAILURE_THRESHOLD", "5")),
    recovery_timeout_sec=float(os.environ.get("NLP_CB_RECOVERY_TIMEOUT_SEC", "60")),
    window_sec=float(os.environ.get("NLP_CB_WINDOW_SEC", "120")),
    half_open_probes=int(os.environ.get("NLP_CB_HALF_OPEN_PROBES", "2")),
)


def nlp_circuit_breaker_stats() -> dict:
    """Return NLP circuit breaker stats snapshot for health/readiness checks."""
    return _nlp_cb.stats


def nlp_circuit_breaker_is_open() -> bool:
    """Return True if the NLP circuit breaker is currently OPEN."""
    return _nlp_cb.state == CircuitBreaker.OPEN


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

    Falls back to ``[NLP_UNAVAILABLE]`` placeholder if the NLP service is
    unreachable — fail-closed to prevent PHI leakage.
    """
    if not _nlp_cb.allow_request():
        _log.warning("nlp_circuit_breaker OPEN — returning redacted placeholder")
        return _NLP_FALLBACK_TEXT

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
            "nlp_service_error type=%s — returning redacted placeholder",
            type(exc).__name__,
        )
        return _NLP_FALLBACK_TEXT


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
            "nlp_circuit_breaker OPEN — returning %d redacted placeholders",
            len(texts),
        )
        return [_NLP_FALLBACK_TEXT] * len(texts)

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

    # Fallback: sub-batch retry (batches of 10) then sequential per-text calls
    sub_batch_size = 10
    if len(texts) > sub_batch_size:
        results = []
        for i in range(0, len(texts), sub_batch_size):
            sub = texts[i:i + sub_batch_size]
            sub_payload = _json_dumps_bytes({
                "items": [
                    {"text": t, "entities": entities, "threshold": threshold,
                     "language": language, "mode": mode}
                    for t in sub
                ],
                "token_state": token_state,
            })
            try:
                sub_result = proxy_post_json(url, sub_payload, timeout=60)
                returned_state = sub_result.get("token_state", {})
                token_state.update(returned_state)
                scrubbed = sub_result.get("results", [])
                if len(scrubbed) == len(sub):
                    _nlp_cb.record_success()
                    results.extend(scrubbed)
                    continue
            except Exception:
                pass
            # Sub-batch failed — fall back to sequential for this sub-batch
            for t in sub:
                results.append(analyze_and_replace_remote(t, entities, threshold, language, mode, token_state))
        return results

    return [
        analyze_and_replace_remote(t, entities, threshold, language, mode, token_state)
        for t in texts
    ]
