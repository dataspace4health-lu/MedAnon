"""gPAS client — public API for pseudonymization and de-pseudonymization.

Internal implementation is split across focused sub-modules:
  .circuit_breaker — _CircuitBreaker state machine + singleton
  .protocol        — FHIR Parameters builders and response parsers
  .transport       — HTTP retry loop, URL resolution, cache helpers, domain listing

All call sites import from this module; the sub-modules are internal.
"""

import logging
import os
from concurrent.futures import as_completed

from utils.fhirpath import find_nodes
from utils.thread_pool import get_executor
from actions.substitute import _substitute_nodes

from .circuit_breaker import GpasUnavailableError  # noqa: F401 — re-exported for callers
from .transport import (
    _resolve_gpas_base,
    _is_cache_enabled,
    _cache_get,
    _cache_set,
    _call_gpas_operation,
    list_gpas_domains,  # noqa: F401 — re-exported for callers
)
from .protocol import (
    _build_pseudonymize_params,
    _build_depseudonymize_params,
    _parse_pseudonymize_response,
    _parse_depseudonymize_response,
)

_GPAS_MAX_BATCH = int(os.environ.get("GPAS_MAX_BATCH_SIZE", "500"))
_log = logging.getLogger("medanon.gpas")


# ---------------------------------------------------------------------------
# Batch pseudonymization
# ---------------------------------------------------------------------------


def gpas_pseudonymize_batch(values, params):
    """Pseudonymize multiple values via gPAS in a single HTTP call.

    Args:
        values: list of original string values to pseudonymize
        params: rule params (gpas_url, gpas_domain, etc.)

    Returns:
        dict mapping original_value -> pseudonym_value
    """
    if not values:
        return {}

    base_url = _resolve_gpas_base(params)
    domain = params.get("gpas_domain") or os.environ.get("GPAS_DOMAIN")
    if not domain:
        raise ValueError(
            "gPAS domain is required (params.gpas_domain or env GPAS_DOMAIN)"
        )

    operation = params.get("gpas_operation", "pseudonymizeAllowCreate")
    use_cache = _is_cache_enabled(params)

    result = {}
    uncached = []
    for val in values:
        s = str(val)
        if use_cache:
            # Include base_url in the cache key so that different gPAS instances
            # (e.g. staging vs. production) never share cached pseudonyms.
            cache_key = ("pseudonymize", base_url, domain, operation, s)
            cached = _cache_get(cache_key)
            if cached is not None:
                result[s] = cached
                continue
        uncached.append(s)

    if uncached:
        unique_uncached = list(dict.fromkeys(uncached))

        def _call_chunk(chunk):
            fhir_request = _build_pseudonymize_params(domain, chunk)
            resp_json = _call_gpas_operation(base_url, operation, fhir_request, params)
            return _parse_pseudonymize_response(resp_json)

        if len(unique_uncached) <= _GPAS_MAX_BATCH:
            # Single batch — common fast path
            mapping = _call_chunk(unique_uncached)
        else:
            # Split into sub-batches and run in parallel.
            # Use submit + as_completed instead of pool.map so that:
            #  1. Successful chunk results are preserved even if other chunks fail
            #  2. Successful results are cached immediately per-chunk
            #  3. Only truly-failed chunks are retried
            chunks = [
                unique_uncached[i : i + _GPAS_MAX_BATCH]
                for i in range(0, len(unique_uncached), _GPAS_MAX_BATCH)
            ]
            mapping = {}
            failed_chunks = []
            # Use the process-wide shared executor instead of a nested
            # ThreadPoolExecutor — avoids spawning N*8 threads when multiple
            # jobs run concurrently.  The global pool caps concurrency.
            futures = {get_executor().submit(_call_chunk, c): c for c in chunks}
            for future in as_completed(futures):
                try:
                    partial = future.result()
                    mapping.update(partial)
                    # Cache successful results immediately (not deferred to
                    # after the loop) so they survive even if later chunks fail.
                    if use_cache:
                        for orig, psn in partial.items():
                            _cache_set(
                                ("pseudonymize", base_url, domain, operation, orig), psn
                            )
                except GpasUnavailableError:
                    # Circuit breaker open — re-raise immediately, no retry
                    raise
                except Exception as exc:
                    failed_chunk = futures[future]
                    _log.warning(
                        "gpas sub-batch failed (%d values): %s — will retry",
                        len(failed_chunk),
                        type(exc).__name__,
                    )
                    failed_chunks.append(failed_chunk)

            # Retry failed chunks once (sequentially to avoid thundering herd)
            for retry_chunk in failed_chunks:
                try:
                    partial = _call_chunk(retry_chunk)
                    mapping.update(partial)
                    if use_cache:
                        for orig, psn in partial.items():
                            _cache_set(
                                ("pseudonymize", base_url, domain, operation, orig), psn
                            )
                except GpasUnavailableError:
                    raise
                except Exception as exc:
                    _log.error(
                        "gpas sub-batch retry failed (%d values): %s",
                        len(retry_chunk),
                        type(exc).__name__,
                    )
                    raise

        # Cache results from the single-batch fast path (multi-chunk results
        # are already cached immediately per-chunk inside the parallel loop).
        if use_cache and len(unique_uncached) <= _GPAS_MAX_BATCH:
            for orig, psn in mapping.items():
                _cache_set(("pseudonymize", base_url, domain, operation, orig), psn)

        result.update(mapping)

    return result


def gpas_depseudonymize_by_path(resource, el, params):
    """De-pseudonymize a single matched value via gPAS $dePseudonymize.

    Required params:
        gpas_url: gPAS base URL
        gpas_domain: gPAS domain name

    Optional params:
        gpas_token / GPAS_TOKEN env: Bearer token for auth
        gpas_timeout_sec: HTTP timeout (default 30)
    """
    base_url = _resolve_gpas_base(params)
    domain = params.get("gpas_domain") or os.environ.get("GPAS_DOMAIN")
    if not domain:
        raise ValueError(
            "gPAS domain is required (params.gpas_domain or env GPAS_DOMAIN)"
        )

    pseudonym_value = str(el["value"])

    # Include base_url in the cache key so different gPAS instances don't collide.
    cache_key = ("depseudonymize", base_url, domain, pseudonym_value)
    if _is_cache_enabled(params):
        cached = _cache_get(cache_key)
        if cached is not None:
            original = cached
        else:
            fhir_request = _build_depseudonymize_params(domain, [pseudonym_value])
            resp_json = _call_gpas_operation(
                base_url, "dePseudonymize", fhir_request, params
            )
            mapping = _parse_depseudonymize_response(resp_json)
            original = mapping.get(pseudonym_value)
            if original is not None:
                _cache_set(cache_key, original)
    else:
        fhir_request = _build_depseudonymize_params(domain, [pseudonym_value])
        resp_json = _call_gpas_operation(
            base_url, "dePseudonymize", fhir_request, params
        )
        mapping = _parse_depseudonymize_response(resp_json)
        original = mapping.get(pseudonym_value)

    if original is None:
        raise ValueError(
            f"gPAS did not return an original for pseudonym (path={el['path']})"
        )

    path = el["path"].split(".")[1:]
    if len(path) == 0:
        resource.clear()
        return
    ret = find_nodes(resource, path[:-1], [])
    _substitute_nodes(ret, path[-1], el["value"], original)
