"""gPAS client  public API for pseudonymization and de-pseudonymization.

Internal implementation is split across focused sub-modules:
  .circuit_breaker  _CircuitBreaker state machine + singleton
  .protocol         FHIR Parameters builders and response parsers
  .transport        HTTP retry loop, URL resolution, cache helpers, domain listing

All call sites import from this module; the sub-modules are internal.
"""

import logging
import os
import threading
from concurrent.futures import as_completed, ThreadPoolExecutor

from utils.fhirpath import find_nodes
from utils.thread_pool import get_executor
from utils.fhirpath import _substitute_nodes

from .circuit_breaker import GpasUnavailableError  # noqa: F401  re-exported for callers
from .transport import (
    _resolve_gpas_base,
    _is_cache_enabled,
    _blind_identifier,
    _cache_get_many,
    _cache_set_many,
    _call_gpas_operation,
    list_gpas_domains,  # noqa: F401  re-exported for callers
)
from .protocol import (
    _build_pseudonymize_params,
    _build_depseudonymize_params,
    _parse_pseudonymize_response,
    _parse_depseudonymize_response,
)

_GPAS_MAX_BATCH = int(os.environ.get("GPAS_MAX_BATCH_SIZE", "500"))
_log = logging.getLogger("medanon.gpas")

# Dedicated executor for gPAS sub-batches.
#
# Why a separate pool?  When ``gpas_pseudonymize_batch`` is called from a
# worker thread of the global ``utils.thread_pool`` executor (which happens
# constantly during bulk processing), submitting sub-batches back to that
# same pool risks livelock if every worker is itself blocked on its own
# nested future.  Using a dedicated pool eliminates the deadlock concern,
# so sub-batches always run in parallel regardless of where the call
# originated.
#
# Default 8: at the default batch size of 1000 there are at most 2 gPAS
# sub-batches (ceil(1000/500)), so 8 workers is already surplus  it covers
# future batch-size increases without wasting thread budget now.  Raise to 16
# only when MEDANON_BATCH_SIZE ≥ 5000 and gPAS can absorb the load.
# Override via GPAS_SUBBATCH_PARALLEL.
_GPAS_SUBBATCH_PARALLEL = max(1, int(os.environ.get("GPAS_SUBBATCH_PARALLEL", "8")))
_subbatch_executor: ThreadPoolExecutor | None = None
_subbatch_executor_lock = threading.Lock()


def _get_subbatch_executor() -> ThreadPoolExecutor:
    """Lazily create the dedicated sub-batch executor."""
    global _subbatch_executor
    if _subbatch_executor is None:
        with _subbatch_executor_lock:
            if _subbatch_executor is None:
                _subbatch_executor = ThreadPoolExecutor(
                    max_workers=_GPAS_SUBBATCH_PARALLEL,
                    thread_name_prefix="gpas-subbatch",
                )
    return _subbatch_executor


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
    if use_cache:
        cache_keys = [
            ("pseudonymize", base_url, domain, operation, _blind_identifier(str(val)))
            for val in values
        ]
        cached_results = _cache_get_many(cache_keys)
        for val, key in zip(values, cache_keys):
            s = str(val)
            if key in cached_results:
                result[s] = cached_results[key]
            else:
                uncached.append(s)
    else:
        uncached = [str(val) for val in values]

    if uncached:
        unique_uncached = list(dict.fromkeys(uncached))

        def _call_chunk(chunk):
            fhir_request = _build_pseudonymize_params(domain, chunk)
            resp_json = _call_gpas_operation(base_url, operation, fhir_request, params)
            return _parse_pseudonymize_response(resp_json)

        if len(unique_uncached) <= _GPAS_MAX_BATCH:
            # Single batch  common fast path
            mapping = _call_chunk(unique_uncached)
        else:
            # Split into sub-batches and submit them to the dedicated
            # gPAS sub-batch executor.  This pool is independent of the
            # global ``utils.thread_pool`` executor, so we can safely
            # parallelise even when the caller is itself running on a
            # global-pool worker thread (no nested-pool livelock risk).
            chunks = [
                unique_uncached[i : i + _GPAS_MAX_BATCH]
                for i in range(0, len(unique_uncached), _GPAS_MAX_BATCH)
            ]
            mapping = {}
            failed_chunks = []
            executor = _get_subbatch_executor()

            futures = {executor.submit(_call_chunk, c): c for c in chunks}
            for future in as_completed(futures):
                try:
                    partial = future.result()
                    mapping.update(partial)
                    if use_cache:
                        _cache_set_many(
                            {
                                (
                                    "pseudonymize",
                                    base_url,
                                    domain,
                                    operation,
                                    _blind_identifier(orig),
                                ): psn
                                for orig, psn in partial.items()
                            }
                        )
                except GpasUnavailableError:
                    raise
                except Exception as exc:
                    failed_chunk = futures[future]
                    _log.warning(
                        "gpas sub-batch failed (%d values): %s  will retry",
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
                        _cache_set_many(
                            {
                                (
                                    "pseudonymize",
                                    base_url,
                                    domain,
                                    operation,
                                    _blind_identifier(orig),
                                ): psn
                                for orig, psn in partial.items()
                            }
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
            _cache_set_many(
                {
                    (
                        "pseudonymize",
                        base_url,
                        domain,
                        operation,
                        _blind_identifier(orig),
                    ): psn
                    for orig, psn in mapping.items()
                }
            )

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

    # De-pseudonymize is NEVER cached: the cache value would be the original
    # patient identifier (PHI).  Original values must only be stored in gPAS
    # (the authorised TTP), not in Redis or any other backing store we control.
    fhir_request = _build_depseudonymize_params(domain, [pseudonym_value])
    resp_json = _call_gpas_operation(base_url, "dePseudonymize", fhir_request, params)
    mapping = _parse_depseudonymize_response(resp_json)
    original = mapping.get(pseudonym_value)

    if original is None:
        raise ValueError(
            f"gPAS did not return an original for pseudonym (path={el['path']})"
        )

    path = el["path"].split(".")[1:]
    if len(path) == 0:
        # Empty path after stripping resourceType means the rule targeted the
        # root resource itself.  Clearing the entire resource silently was the
        # bug  fail loudly so callers can fix the rule expression.
        raise ValueError(
            f"Empty path after removing resource type root in gpas_depseudonymize  "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(resource, path[:-1], [])
    _substitute_nodes(ret, path[-1], el["value"], original)


def gpas_depseudonymize_batch(values, params):
    """De-pseudonymize multiple values via gPAS in a single HTTP call.

    Mirrors :func:`gpas_pseudonymize_batch` but calls ``$dePseudonymize``.

    Args:
        values: list of pseudonym strings to reverse-lookup
        params: rule params (gpas_url, gpas_domain, etc.)

    Returns:
        dict mapping pseudonym_value -> original_value
    """
    if not values:
        return {}

    base_url = _resolve_gpas_base(params)
    domain = params.get("gpas_domain") or os.environ.get("GPAS_DOMAIN")
    if not domain:
        raise ValueError(
            "gPAS domain is required (params.gpas_domain or env GPAS_DOMAIN)"
        )

    # De-pseudonymize results are NEVER cached  the cache value would be the
    # original patient identifier (PHI).  Always fetch directly from gPAS.
    uncached = [str(val) for val in values]

    result = {}
    if uncached:
        unique_uncached = list(dict.fromkeys(uncached))

        def _call_chunk(chunk):
            fhir_request = _build_depseudonymize_params(domain, chunk)
            resp_json = _call_gpas_operation(
                base_url, "dePseudonymize", fhir_request, params
            )
            return _parse_depseudonymize_response(resp_json)

        if len(unique_uncached) <= _GPAS_MAX_BATCH:
            mapping = _call_chunk(unique_uncached)
        else:
            chunks = [
                unique_uncached[i : i + _GPAS_MAX_BATCH]
                for i in range(0, len(unique_uncached), _GPAS_MAX_BATCH)
            ]
            mapping = {}
            failed_chunks = []
            futures = {get_executor().submit(_call_chunk, c): c for c in chunks}
            for future in as_completed(futures):
                try:
                    partial = future.result()
                    mapping.update(partial)
                except GpasUnavailableError:
                    raise
                except Exception as exc:
                    failed_chunk = futures[future]
                    _log.warning(
                        "gpas depseudonymize sub-batch failed (%d values): %s  skipping",
                        len(failed_chunk),
                        type(exc).__name__,
                    )
                    failed_chunks.append(failed_chunk)
            if failed_chunks:
                total_failed = sum(len(c) for c in failed_chunks)
                raise RuntimeError(
                    f"gpas_depseudonymize_batch: {total_failed} values failed across "
                    f"{len(failed_chunks)} chunk(s)"
                )

        result.update(mapping)

    return result
