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

# Client-side batch size limit: must not exceed the NLP service's NLP_MAX_BATCH_ITEMS
# setting (default 1000).  Using the same env var keeps them in sync.  Sending
# more than this in a single request causes HTTP 422, which previously tripped
# the circuit breaker and broke NLP for all subsequent chunks.
_NLP_CLIENT_BATCH_LIMIT: int = max(
    1, int(os.environ.get("NLP_MAX_BATCH_ITEMS", "1000"))
)

# HTTP timeouts to the NLP microservice.  Singletons (per-text) used to be
# hard-coded at 30s and batch at 120s; both are now overridable so operators
# can tune for slower deployments (e.g. CPU-only inference) without trip-
# storming the circuit breaker.
_NLP_DETECT_TIMEOUT: float = float(os.environ.get("NLP_DETECT_TIMEOUT_SEC", "30"))
_NLP_BATCH_TIMEOUT: float = float(os.environ.get("NLP_BATCH_TIMEOUT_SEC", "120"))

# When detect_batch_remote sub-batches a large request, sub-batches are
# independent HTTP calls (no shared mutable state) and can be issued in
# parallel.  Default 4 concurrent sub-batches matches the NLP service's
# NLP_BATCH_THREADS=4.  Set to 1 to fall back to fully sequential behaviour.
_NLP_CLIENT_SUBBATCH_PARALLEL: int = max(
    1, int(os.environ.get("NLP_CLIENT_SUBBATCH_PARALLEL", "4"))
)


def _is_client_error(exc: Exception) -> bool:
    """Return True when *exc* represents an HTTP 4xx client error.

    HTTP 4xx responses are client-side mistakes (e.g. oversized payload → 422).
    They should NOT be counted as server failures for circuit-breaker purposes;
    only 5xx and network errors indicate that the upstream service is degraded.
    """
    msg = str(exc)
    # proxy_post_json raises ValueError with the HTTP status in the message.
    return isinstance(exc, ValueError) and any(
        f"HTTP {code}" in msg
        for code in ("400", "401", "403", "404", "405", "408", "409", "410",
                     "413", "415", "422", "429")
    )


def _nlp_fallback_token(text: str | None = None, entity_type: str = "ANY") -> str:
    """Return a deterministic redaction placeholder.

    Determinism is required because identical input strings (e.g., the same
    address string appearing on two patients) MUST produce the same scrubbed
    output — otherwise the "NLP-unavailable" path becomes a covert linkage
    channel whose random nonces let an attacker correlate failed-NLP records
    by their unique placeholder.

    The token is a short blake2b hash of (entity_type, text).  When *text* is
    None (legacy callers / aggregate failures), a static placeholder is used.
    """
    if text is None:
        return "[NLP_UNAVAILABLE]"
    import hashlib

    h = hashlib.blake2b(digest_size=4)
    h.update(entity_type.encode("utf-8", errors="replace"))
    h.update(b"\x1f")
    h.update(text.encode("utf-8", errors="replace"))
    return f"[NLP_UNAVAILABLE_{h.hexdigest().upper()}]"


def _validate_detect_response(raw: dict) -> dict:
    """Validate the NLP /v1/detect response against the contract schema."""
    from api.schemas.nlp import NlpDetectResponse
    return NlpDetectResponse.model_validate(raw).model_dump()


def _validate_batch_response(raw: dict) -> dict:
    """Validate the NLP /v1/detect/batch response against the contract schema."""
    from api.schemas.nlp import NlpBatchResponse
    return NlpBatchResponse.model_validate(raw).model_dump()


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
    recovery_timeout_sec=float(os.environ.get("NLP_CB_RECOVERY_TIMEOUT_SEC", "30")),
    window_sec=float(os.environ.get("NLP_CB_WINDOW_SEC", "60")),
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

    Fast path: checks the process-local L1 LRU + Redis L2 cache before making
    an HTTP round-trip.  Phase B of the NLP batch orchestrator (``_batch_detect_prewarm``)
    populates both caches for every text in the current chunk, so Phase-C
    per-field calls are L1 hits and avoid the network entirely.
    """
    # Fast path: L1 + L2 cache lookup (populated by _batch_detect_prewarm in Phase B).
    # This eliminates the HTTP round-trip for every text that was already seen during
    # the same bulk run — the dominant cost for large bulk jobs.
    from integrations.nlp.cache import lookup_many, store_many
    _cached, _keys = lookup_many([text], entities, threshold, language)
    if _cached[0] is not None:
        return _cached[0]

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
        from utils.bulkhead import bulkhead, UpstreamSaturated
        try:
            with bulkhead("nlp", wait_sec=float(os.environ.get("BULKHEAD_NLP_WAIT_SEC", "1"))):
                raw = proxy_post_json(url, payload, timeout=_NLP_DETECT_TIMEOUT)
        except UpstreamSaturated as exc:
            raise NlpUnavailableError("NLP bulkhead saturated") from exc
        result = _validate_detect_response(raw)
        _nlp_cb.record_success()
        detections = result.get("detections") or []
        hits = [(d[0], d[1], d[2]) for d in detections]
        try:
            store_many(_keys, [hits])
        except Exception:
            pass  # cache must never break the data path
        return hits
    except NlpUnavailableError:
        raise
    except Exception as exc:
        _nlp_cb.record_failure()
        _log.warning(
            "nlp_service_detect_error type=%s — detect unavailable",
            type(exc).__name__,
        )
        raise NlpUnavailableError(f"NLP detect failed: {exc}") from exc


def _detect_batch_chunk(
    texts: list[str],
    entities: list[str],
    threshold: float,
    language: str,
) -> list[list[tuple[int, int, str]]]:
    """Send a single batch of ≤ _NLP_CLIENT_BATCH_LIMIT texts to /v1/detect/batch.

    Private helper called by detect_batch_remote after client-side sub-batching.
    Falls back to sequential per-text detection on server errors (5xx / network),
    but NOT on 4xx client errors — those indicate a configuration problem.
    """
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
        from utils.bulkhead import bulkhead, UpstreamSaturated
        try:
            with bulkhead("nlp", wait_sec=float(os.environ.get("BULKHEAD_NLP_WAIT_SEC", "1"))):
                raw = proxy_post_json(url, payload, timeout=_NLP_BATCH_TIMEOUT)
        except UpstreamSaturated as exc:
            raise NlpUnavailableError("NLP bulkhead saturated") from exc
        result = _validate_batch_response(raw)
        all_detections = result.get("detections") or []
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
        # Only record as a server failure when the error is NOT a client-side
        # 4xx response.  HTTP 429 (rate-limit) counts as a CB event because it
        # signals capacity pressure; other 4xx errors are caller mistakes and
        # must not degrade the circuit breaker state.
        is_rate_limit = isinstance(exc, ValueError) and "HTTP 429" in str(exc)
        if not _is_client_error(exc) or is_rate_limit:
            _nlp_cb.record_failure()
        _log.warning(
            "detect_batch_chunk_error type=%s — falling back to sequential",
            type(exc).__name__,
        )

    # Fallback: sequential per-text detection.
    # Re-check the circuit breaker before each call — a run of failures during
    # the batch may have tripped it while iterating.
    results: list[list[tuple[int, int, str]]] = []
    for t in texts:
        if not _nlp_cb.allow_request():
            raise NlpUnavailableError("NLP circuit breaker OPEN during sequential fallback")
        results.append(detect_remote(t, entities, threshold, language))
    return results


def detect_batch_remote(
    texts: list[str],
    entities: list[str],
    threshold: float,
    language: str,
) -> list[list[tuple[int, int, str]]]:
    """Batch entity detection — one or more HTTP round-trips for all texts.

    Automatically sub-batches when ``len(texts) > _NLP_CLIENT_BATCH_LIMIT`` to
    respect the NLP service's ``NLP_MAX_BATCH_ITEMS`` cap.  Previously, sending
    all texts in one request triggered HTTP 422 errors that incremented the NLP
    circuit-breaker failure counter, opening the CB after five chunks and causing
    NLP scrubbing to fail for the remainder of the bulk export.

    Cached: results are memoised by content hash via :mod:`integrations.nlp.cache`,
    so repeat runs over the same dataset (and intra-run duplicates) skip the
    HTTP round-trip entirely.

    Returns list of [(start, end, entity_type)] per text.
    """
    if not texts:
        return []

    # Check the cross-run NLP detection cache *first*.  Hits do not need the
    # circuit breaker or the NLP service at all.
    from integrations.nlp.cache import lookup_many, store_many

    cached, keys = lookup_many(texts, entities, threshold, language)
    misses_idx = [i for i, c in enumerate(cached) if c is None]
    if not misses_idx:
        return [c for c in cached]  # type: ignore[misc]

    if not _nlp_cb.allow_request():
        _log.warning("nlp_circuit_breaker OPEN — batch detect unavailable")
        raise NlpUnavailableError("NLP circuit breaker OPEN")

    miss_texts = [texts[i] for i in misses_idx]
    if len(miss_texts) <= _NLP_CLIENT_BATCH_LIMIT:
        miss_results = _detect_batch_chunk(miss_texts, entities, threshold, language)
    else:
        # Sub-batches are independent HTTP calls — issue them concurrently to
        # avoid serialising N round-trips behind one another.  We still respect
        # the circuit breaker: it is checked before submission, and a sub-batch
        # that fails is propagated through future.result() below.
        chunks = [
            miss_texts[i : i + _NLP_CLIENT_BATCH_LIMIT]
            for i in range(0, len(miss_texts), _NLP_CLIENT_BATCH_LIMIT)
        ]
        if _NLP_CLIENT_SUBBATCH_PARALLEL <= 1 or len(chunks) == 1:
            miss_results = []
            for chunk in chunks:
                if not _nlp_cb.allow_request():
                    raise NlpUnavailableError(
                        "NLP circuit breaker OPEN during sub-batching"
                    )
                miss_results.extend(
                    _detect_batch_chunk(chunk, entities, threshold, language)
                )
        else:
            from concurrent.futures import ThreadPoolExecutor

            if not _nlp_cb.allow_request():
                raise NlpUnavailableError(
                    "NLP circuit breaker OPEN during sub-batching"
                )
            workers = min(_NLP_CLIENT_SUBBATCH_PARALLEL, len(chunks))
            # Per-call pool so a slow batch can never exhaust the global one.
            # Sub-batches are CPU-light (HTTP wait) so the pool is cheap.
            with ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="nlp-subbatch"
            ) as pool:
                # Preserve ordering: collect futures by index, then read in order.
                futures = [
                    pool.submit(
                        _detect_batch_chunk, chunk, entities, threshold, language
                    )
                    for chunk in chunks
                ]
                miss_results = []
                for fut in futures:
                    miss_results.extend(fut.result())

    # Persist the new detections, then merge cached + fresh in original order.
    miss_keys = [keys[i] for i in misses_idx]
    try:
        store_many(miss_keys, miss_results)
    except Exception as exc:  # pragma: no cover — cache must never break the path
        _log.warning("nlp_cache_store_failed: %s", exc)

    out: list[list[tuple[int, int, str]]] = list(cached)  # type: ignore[arg-type]
    for idx, det in zip(misses_idx, miss_results):
        out[idx] = det
    return out  # type: ignore[return-value]


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
        return _nlp_fallback_token(text)

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
        from utils.bulkhead import bulkhead, UpstreamSaturated
        try:
            with bulkhead("nlp", wait_sec=float(os.environ.get("BULKHEAD_NLP_WAIT_SEC", "1"))):
                raw = proxy_post_json(url, payload, timeout=_NLP_DETECT_TIMEOUT)
        except UpstreamSaturated:
            _log.warning("nlp_bulkhead_saturated — returning redacted placeholder")
            return _nlp_fallback_token(text)
        result = _validate_detect_response(raw)
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
        return _nlp_fallback_token(text)


def analyze_and_replace_batch_remote(
    texts: list[str],
    entities: list[str],
    threshold: float,
    language: str,
    mode: str,
    token_state: dict,
) -> list[str]:
    """Batch NLP detection — one or more HTTP round-trips for all texts.

    Automatically sub-batches when ``len(texts) > _NLP_CLIENT_BATCH_LIMIT`` to
    avoid HTTP 422 from the NLP service.  Token state is threaded through all
    sub-batches in order so surrogate tokens remain consistent within a call.

    Falls back to per-text sequential calls if the batch endpoint fails.
    """
    if not texts:
        return []

    if not _nlp_cb.allow_request():
        _log.warning(
            "nlp_circuit_breaker OPEN — returning %d redacted placeholders",
            len(texts),
        )
        return [_nlp_fallback_token(t) for t in texts]

    # Pre-batch proactively to avoid NLP service 422 errors.
    if len(texts) > _NLP_CLIENT_BATCH_LIMIT:
        all_results: list[str] = []
        for i in range(0, len(texts), _NLP_CLIENT_BATCH_LIMIT):
            if not _nlp_cb.allow_request():
                all_results.extend(_nlp_fallback_token(t) for t in texts[i:])
                break
            chunk_results = analyze_and_replace_batch_remote(
                texts[i : i + _NLP_CLIENT_BATCH_LIMIT],
                entities, threshold, language, mode, token_state,
            )
            all_results.extend(chunk_results)
        return all_results

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
        raw = proxy_post_json(url, payload, timeout=60)
        result = _validate_batch_response(raw)
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
        elif not _is_client_error(exc):
            # Only record server/network failures — never 4xx client errors.
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
            return [_nlp_fallback_token(t) for t in texts]

    # Fallback: sub-batch retry (use same limit as detect_batch_remote)
    sub_batch_size = _NLP_CLIENT_BATCH_LIMIT
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
                sub_raw = proxy_post_json(url, sub_payload, timeout=60)
                sub_result = _validate_batch_response(sub_raw)
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
