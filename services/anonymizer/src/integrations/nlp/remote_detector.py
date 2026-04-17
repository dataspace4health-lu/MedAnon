"""Remote NLP detector — calls the NLP microservice over HTTP.

Drop-in replacement for the local ``_analyze_and_replace`` calls when
``NLP_SERVICE_URL`` is set. The calling interface in ``nlp_scrub_by_path``
remains unchanged; the choice of local vs. remote is made at call time.
"""

from __future__ import annotations

from utils.json_fast import dumps_bytes as _json_dumps_bytes
import logging
import os

from integrations.http_client import proxy_post_json
from utils.circuit_breaker import CircuitBreaker
import urllib3

_log = logging.getLogger("medanon.nlp.remote")

# NLP failure mode: always fail-closed (redact) to prevent PHI leakage.
_NLP_FALLBACK_TEXT = "[NLP_UNAVAILABLE]"


class NlpUnavailableError(RuntimeError):
    """Raised when the NLP service is unreachable and detection cannot proceed.

    Used by detect_remote / detect_batch_remote so that callers (nlp_detect_act)
    fail-closed (redact) instead of silently passing text through unexamined.
    """


# ---------------------------------------------------------------------------
# Three-state NLP circuit breaker (shared implementation)
# ---------------------------------------------------------------------------

_nlp_cb = CircuitBreaker(
    name="nlp",
    failure_threshold=int(os.environ.get("NLP_CB_FAILURE_THRESHOLD", "5")),
    recovery_timeout_sec=float(os.environ.get("NLP_CB_RECOVERY_TIMEOUT_SEC", "60")),
    window_sec=float(os.environ.get("NLP_CB_WINDOW_SEC", "120")),
    half_open_probes=int(os.environ.get("NLP_CB_HALF_OPEN_PROBES", "2")),
    timeout_threshold=int(os.environ.get("NLP_CB_TIMEOUT_THRESHOLD", "2")),
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


def detect_remote(
    text: str,
    entities: list[str],
    threshold: float,
    language: str,
) -> list[tuple[int, int, str]]:
    """Call POST /v1/detect with detect_only=true and return raw detections.

    Returns ``[(start, end, entity_type)]`` sorted descending by start position
    (ready for right-to-left replacement).

    Raises ``NlpUnavailableError`` when the NLP service is unreachable so that
    callers (``nlp_detect_act``) fail-closed rather than treating the text as
    clean.
    """
    if not _nlp_cb.allow_request():
        _log.warning("nlp_circuit_breaker OPEN — detect unavailable")
        raise NlpUnavailableError("NLP circuit breaker OPEN")

    payload = _json_dumps_bytes(
        {
            "text": text,
            "entities": entities,
            "threshold": threshold,
            "language": language,
            "detect_only": True,
        }
    )

    url = _nlp_service_url("/v1/detect")
    try:
        result = proxy_post_json(url, payload, timeout=30)
        _nlp_cb.record_success()
        detections = result.get("detections", [])
        return [(d[0], d[1], d[2]) for d in detections]
    except Exception as exc:
        _nlp_cb.record_failure()
        _log.warning(
            "nlp_service_detect_error type=%s — detect unavailable",
            type(exc).__name__,
        )
        raise NlpUnavailableError(f"NLP detect failed: {exc}") from exc


def detect_batch_remote(
    texts: list[str],
    entities: list[str],
    threshold: float,
    language: str,
) -> list[list[tuple[int, int, str]]]:
    """Batch entity detection — one HTTP round-trip for all texts.

    Sends all texts with detect_only=True to /v1/detect/batch.
    Returns list of [(start, end, entity_type)] per text.
    Falls back to sequential per-text calls on batch failure.
    """
    if not texts:
        return []
    if not _nlp_cb.allow_request():
        _log.warning("nlp_circuit_breaker OPEN — batch detect unavailable")
        raise NlpUnavailableError("NLP circuit breaker OPEN")

    payload = _json_dumps_bytes(
        {
            "items": [
                {
                    "text": t,
                    "entities": entities,
                    "threshold": threshold,
                    "language": language,
                    "detect_only": True,
                }
                for t in texts
            ],
        }
    )

    url = _nlp_service_url("/v1/detect/batch")
    try:
        result = proxy_post_json(url, payload, timeout=120)
        all_detections = result.get("detections", [])
        if len(all_detections) == len(texts):
            _nlp_cb.record_success()
            return [
                [(d[0], d[1], d[2]) for d in dets]
                for dets in all_detections
            ]
        # Length mismatch is a protocol error — the service is misbehaving.
        # Record a CB failure so a consistently-broken service trips the breaker.
        _nlp_cb.record_failure()
        _log.warning(
            "detect_batch response length mismatch: expected %d, got %d",
            len(texts), len(all_detections),
        )
    except Exception as exc:
        _nlp_cb.record_failure()
        _log.warning(
            "detect_batch_remote_error type=%s — falling back to sequential",
            type(exc).__name__,
        )

    # Fallback: sequential per-text detection
    return [detect_remote(t, entities, threshold, language) for t in texts]


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

    payload = _json_dumps_bytes(
        {
            "text": text,
            "entities": entities,
            "threshold": threshold,
            "language": language,
            "mode": mode,
            "token_state": token_state,
        }
    )

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

    payload = _json_dumps_bytes(
        {
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
        }
    )

    url = _nlp_service_url("/v1/detect/batch")
    try:
        result = proxy_post_json(url, payload, timeout=60)
        returned_state = result.get("token_state", {})
        token_state.update(returned_state)
        scrubbed = result.get("results", [])
        if len(scrubbed) == len(texts):
            _nlp_cb.record_success()
            return scrubbed
        _log.warning(
            "nlp batch response length mismatch: expected %d, got %d",
            len(texts),
            len(scrubbed),
        )
    except Exception as exc:
        _is_timeout = isinstance(
            exc,
            (
                urllib3.exceptions.ConnectTimeoutError,
                urllib3.exceptions.ReadTimeoutError,
                urllib3.exceptions.TimeoutError,
            ),
        )
        if _is_timeout:
            _nlp_cb.record_timeout()
        else:
            _nlp_cb.record_failure()
        _log.warning(
            "nlp_batch_error type=%s cb_state=%s — %s",
            type(exc).__name__,
            _nlp_cb.state,
            "CB open, returning placeholders" if not _nlp_cb.allow_request() else "falling back to sequential",
        )
        # If batch failure tripped the circuit breaker, bail immediately rather than
        # spawning hundreds of sub-batch HTTP calls under degradation.
        if not _nlp_cb.allow_request():
            return [_NLP_FALLBACK_TEXT] * len(texts)

    # Fallback: sub-batch retry (batches of 10) then sequential per-text calls
    sub_batch_size = 10
    if len(texts) > sub_batch_size:
        results = []
        for i in range(0, len(texts), sub_batch_size):
            sub = texts[i : i + sub_batch_size]
            sub_payload = _json_dumps_bytes(
                {
                    "items": [
                        {
                            "text": t,
                            "entities": entities,
                            "threshold": threshold,
                            "language": language,
                            "mode": mode,
                        }
                        for t in sub
                    ],
                    "token_state": token_state,
                }
            )
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
                results.append(
                    analyze_and_replace_remote(
                        t, entities, threshold, language, mode, token_state
                    )
                )
        return results

    return [
        analyze_and_replace_remote(t, entities, threshold, language, mode, token_state)
        for t in texts
    ]
