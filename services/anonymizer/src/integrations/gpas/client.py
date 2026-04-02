"""gPAS client — public API for pseudonymization and de-pseudonymization.

Internal implementation is split across focused sub-modules:
  .circuit_breaker — _CircuitBreaker state machine + singleton
  .protocol        — FHIR Parameters builders and response parsers
  .transport       — HTTP retry loop, URL resolution, cache helpers, domain listing

All call sites import from this module; the sub-modules are internal.
"""

import json
import os

from utils.fhirpath import find_nodes
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
    domain = params.get('gpas_domain') or os.environ.get('GPAS_DOMAIN')
    if not domain:
        raise ValueError('gPAS domain is required (params.gpas_domain or env GPAS_DOMAIN)')

    operation = params.get('gpas_operation', 'pseudonymizeAllowCreate')
    use_cache = _is_cache_enabled(params)

    result = {}
    uncached = []
    for val in values:
        s = str(val)
        if use_cache:
            # Include base_url in the cache key so that different gPAS instances
            # (e.g. staging vs. production) never share cached pseudonyms.
            cache_key = ('pseudonymize', base_url, domain, operation, s)
            cached = _cache_get(cache_key)
            if cached is not None:
                result[s] = cached
                continue
        uncached.append(s)

    if uncached:
        unique_uncached = list(dict.fromkeys(uncached))
        fhir_request = _build_pseudonymize_params(domain, unique_uncached)
        resp_json = _call_gpas_operation(base_url, operation, fhir_request, params)
        mapping = _parse_pseudonymize_response(resp_json)

        for orig, psn in mapping.items():
            result[orig] = psn
            if use_cache:
                _cache_set(('pseudonymize', base_url, domain, operation, orig), psn)

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
    domain = params.get('gpas_domain') or os.environ.get('GPAS_DOMAIN')
    if not domain:
        raise ValueError('gPAS domain is required (params.gpas_domain or env GPAS_DOMAIN)')

    pseudonym_value = str(el['value'])

    # Include base_url in the cache key so different gPAS instances don't collide.
    cache_key = ('depseudonymize', base_url, domain, pseudonym_value)
    if _is_cache_enabled(params):
        cached = _cache_get(cache_key)
        if cached is not None:
            original = cached
        else:
            fhir_request = _build_depseudonymize_params(domain, [pseudonym_value])
            resp_json = _call_gpas_operation(base_url, 'dePseudonymize', fhir_request, params)
            mapping = _parse_depseudonymize_response(resp_json)
            original = mapping.get(pseudonym_value)
            if original is not None:
                _cache_set(cache_key, original)
    else:
        fhir_request = _build_depseudonymize_params(domain, [pseudonym_value])
        resp_json = _call_gpas_operation(base_url, 'dePseudonymize', fhir_request, params)
        mapping = _parse_depseudonymize_response(resp_json)
        original = mapping.get(pseudonym_value)

    if original is None:
        raise ValueError(f'gPAS did not return an original for pseudonym (path={el["path"]})')

    path = el['path'].split('.')[1:]
    if len(path) == 0:
        resource.clear()
        return
    ret = find_nodes(resource, path[:-1], [])
    _substitute_nodes(ret, path[-1], el['value'], original)
