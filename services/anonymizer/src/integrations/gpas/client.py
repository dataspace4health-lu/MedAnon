
from utils.fhirpath import find_nodes
from utils.logging import REQUEST_ID
from utils.metrics import (
    GPAS_CALL_COUNT,
    GPAS_CACHE_HITS,
    GPAS_CACHE_MISSES,
    GPAS_LATENCY,
)
from actions.substitute import _substitute_nodes
import html
import json
import logging
import os
import re
import threading
import time
from urllib import request, error as urlerror
from urllib.parse import urlsplit, urlunsplit

gpas_log = logging.getLogger("medanon.gpas")

# ---------------------------------------------------------------------------
# gPAS TTP-FHIR Gateway client
#
# Implements the real gPAS FHIR Operations API:
#   Base: http[s]://<host>:<port>/ttp-fhir/fhir/gpas
#
#   $pseudonymizeAllowCreate  — get or create pseudonym for original value(s)
#   $pseudonymize             — look up existing pseudonym (fails if missing)
#   $dePseudonymize           — reverse look-up: pseudonym -> original value
#
# All three operations use FHIR R4 Parameters resources as request/response.
# Ref: https://www.ths-greifswald.de/wp-content/uploads/tools/fhirgw/ig/
#        2023-1-2/ImplementationGuide-markdown-Pseudonymmanagement.html
# ---------------------------------------------------------------------------

GPAS_SYSTEM = "https://ths-greifswald.de/gpas"
GPAS_FHIR_BASE_PATH = "/ttp-fhir/fhir/gpas"
GPAS_EXPORT_DOMAINS_PATH = "/gpas-web/html/internal/admin/export.xhtml"

# Bounded in-memory cache — prevents unbounded memory growth in long-running API
_GPAS_CACHE_MAX = 50_000
_GPAS_RESULT_CACHE: dict = {}
_GPAS_CACHE_LOCK = threading.Lock()


def _normalize_gpas_base(url):
    """Normalize a gPAS URL to the FHIR base endpoint.

    Accepts either:
      - the FHIR base itself: http://host:port/ttp-fhir/fhir/gpas
      - a child path such as /ttp-fhir/fhir/gpas/metadata
      - a gPAS admin UI URL such as /gpas-web/html/internal/admin/export.xhtml
    """
    parts = urlsplit(str(url).strip())
    path = parts.path.rstrip('/')

    if GPAS_FHIR_BASE_PATH in path:
        normalized_path = path.split(GPAS_FHIR_BASE_PATH, 1)[0] + GPAS_FHIR_BASE_PATH
    elif "/gpas-web/" in path or path.endswith("/gpas-web"):
        normalized_path = GPAS_FHIR_BASE_PATH
    else:
        normalized_path = path or GPAS_FHIR_BASE_PATH

    return urlunsplit((parts.scheme, parts.netloc, normalized_path, "", ""))


def _resolve_gpas_admin_url(params):
    """Return the admin export URL used to discover configured domains."""
    admin_url = params.get('gpas_admin_url') or os.environ.get('GPAS_ADMIN_URL')
    if admin_url:
        parts = urlsplit(str(admin_url).strip())
        if "/gpas-web/" in parts.path:
            path = GPAS_EXPORT_DOMAINS_PATH
        else:
            path = parts.path.rstrip('/') + GPAS_EXPORT_DOMAINS_PATH
        return urlunsplit((parts.scheme, parts.netloc, path, "", ""))

    base_url = _resolve_gpas_base(params)
    parts = urlsplit(base_url)
    return urlunsplit((parts.scheme, parts.netloc, GPAS_EXPORT_DOMAINS_PATH, "", ""))


def _parse_gpas_domains_from_html(html_text):
    # Cap label length and result count to prevent memory exhaustion from
    # a malformed or malicious gPAS admin UI response.
    labels = re.findall(r'data-item-label="([^"]{0,200})"', html_text)
    return sorted(dict.fromkeys(html.unescape(label) for label in labels))[:1000]


def list_gpas_domains(params):
    """Fetch and return configured gPAS domain names from the admin UI."""
    url = _resolve_gpas_admin_url(params)
    auth_headers = _resolve_gpas_headers(params)
    headers = {'Accept': 'text/html'}
    if 'Authorization' in auth_headers:
        headers['Authorization'] = auth_headers['Authorization']
    req = request.Request(url=url, headers=headers, method='GET')
    with request.urlopen(req, timeout=float(params.get('gpas_timeout_sec', 30))) as resp:
        charset = resp.headers.get_content_charset('utf-8')
        body = resp.read().decode(charset, errors='replace')
    return _parse_gpas_domains_from_html(body)


def _resolve_gpas_base(params):
    """Return the gPAS base URL (``[base]``) from params or environment."""
    base = params.get('gpas_url') or os.environ.get('GPAS_URL')
    if not base:
        raise ValueError(
            'gPAS base URL is required. Set params.gpas_url or env GPAS_URL '
            '(e.g. https://<host>:<port>/ttp-fhir/fhir/gpas)'
        )
    return _normalize_gpas_base(base)


def _resolve_gpas_headers(params):
    """Build HTTP headers for a gPAS FHIR request."""
    headers = {
        'Content-Type': 'application/fhir+json',
        'Accept': 'application/fhir+json',
        'X-Request-ID': REQUEST_ID.get('-'),
    }
    token = params.get('gpas_token') or os.environ.get('GPAS_TOKEN')
    basic_user = params.get('gpas_basic_user') or os.environ.get('GPAS_BASIC_USER')
    basic_pass = params.get('gpas_basic_pass') or os.environ.get('GPAS_BASIC_PASS')

    if token and basic_user and basic_pass:
        gpas_log.warning(
            "Both GPAS_TOKEN (Bearer) and GPAS_BASIC_USER/GPAS_BASIC_PASS are set. "
            "Bearer token takes precedence — Basic auth credentials will be ignored."
        )

    if token:
        headers['Authorization'] = f'Bearer {token}'
    elif basic_user and basic_pass:
        import base64
        auth = base64.b64encode(f"{basic_user}:{basic_pass}".encode('utf-8')).decode('ascii')
        headers['Authorization'] = f'Basic {auth}'
    return headers


def _is_cache_enabled(params):
    raw = params.get('gpas_cache_enabled', os.environ.get('GPAS_CACHE_ENABLED', 'true'))
    return str(raw).strip().lower() not in ('false', '0', 'no', 'off')


def _cache_get(cache_key):
    with _GPAS_CACHE_LOCK:
        value = _GPAS_RESULT_CACHE.get(cache_key)
    if value is not None:
        GPAS_CACHE_HITS.inc()
    else:
        GPAS_CACHE_MISSES.inc()
    return value


def _cache_set(cache_key, value):
    with _GPAS_CACHE_LOCK:
        if len(_GPAS_RESULT_CACHE) >= _GPAS_CACHE_MAX:
            # Evict the oldest 10% of entries rather than clearing everything at
            # once, which would cause a thundering herd of gPAS requests.
            evict_count = max(1, _GPAS_CACHE_MAX // 10)
            keys_to_evict = list(_GPAS_RESULT_CACHE.keys())[:evict_count]
            for k in keys_to_evict:
                _GPAS_RESULT_CACHE.pop(k, None)
        _GPAS_RESULT_CACHE[cache_key] = value


# -- FHIR Parameters builders -----------------------------------------------

def _build_pseudonymize_params(domain, original_values):
    """Build a FHIR Parameters request for $pseudonymize[AllowCreate].

    Args:
        domain: gPAS domain name (string, e.g. "MIRACUM")
        original_values: list of original string values to pseudonymize
    """
    params_list = [{"name": "target", "valueString": domain}]
    for val in original_values:
        params_list.append({"name": "original", "valueString": str(val)})
    return {
        "resourceType": "Parameters",
        "parameter": params_list,
    }


def _build_depseudonymize_params(domain, pseudonym_values):
    """Build a FHIR Parameters request for $dePseudonymize.

    Args:
        domain: gPAS domain name
        pseudonym_values: list of pseudonym strings to reverse-lookup
    """
    params_list = [{"name": "target", "valueString": domain}]
    for val in pseudonym_values:
        params_list.append({"name": "pseudonym", "valueString": str(val)})
    return {
        "resourceType": "Parameters",
        "parameter": params_list,
    }


# -- FHIR Parameters response parsers ---------------------------------------

def _parse_pseudonymize_response(resp_json):
    """Parse a $pseudonymize[AllowCreate] response into a mapping dict.

    Returns:
        dict mapping original_value -> pseudonym_value
    Raises:
        ValueError for any ``error`` entries in the response.
    """
    mapping = {}
    errors = []
    for param in resp_json.get("parameter", []):
        name = param.get("name")
        parts = {p["name"]: p for p in param.get("part", [])}
        if name == "pseudonym":
            orig = parts.get("original", {}).get("valueIdentifier", {}).get("value")
            psn = parts.get("pseudonym", {}).get("valueIdentifier", {}).get("value")
            if orig is not None and psn is not None:
                mapping[orig] = psn
        elif name == "error":
            orig_id = parts.get("original", {}).get("valueIdentifier", {}).get("value", "?")
            code = parts.get("error-code", {}).get("valueCoding", {}).get("code", "unknown")
            errors.append(f"original={orig_id} error={code}")
    if errors:
        raise ValueError(f"gPAS returned errors: {'; '.join(errors)}")
    return mapping


def _parse_depseudonymize_response(resp_json):
    """Parse a $dePseudonymize response into a mapping dict.

    Returns:
        dict mapping pseudonym_value -> original_value
    """
    mapping = {}
    errors = []
    for param in resp_json.get("parameter", []):
        name = param.get("name")
        parts = {p["name"]: p for p in param.get("part", [])}
        if name == "original":
            psn = parts.get("pseudonym", {}).get("valueIdentifier", {}).get("value")
            orig = parts.get("original", {}).get("valueIdentifier", {}).get("value")
            if psn is not None and orig is not None:
                mapping[psn] = orig
        elif name == "error":
            psn_id = parts.get("pseudonym", {}).get("valueIdentifier", {}).get("value", "?")
            code = parts.get("error-code", {}).get("valueCoding", {}).get("code", "unknown")
            errors.append(f"pseudonym={psn_id} error={code}")
    if errors:
        raise ValueError(f"gPAS returned errors: {'; '.join(errors)}")
    return mapping


# -- HTTP transport ----------------------------------------------------------

def _call_gpas_operation(base_url, operation, fhir_params, params):
    """POST a FHIR Parameters resource to a gPAS $operation endpoint.

    Args:
        base_url: gPAS [base] URL (e.g. https://host:port/ttp-fhir/fhir/gpas)
        operation: FHIR operation name without $ (e.g. "pseudonymizeAllowCreate")
        fhir_params: dict — the FHIR Parameters JSON body
        params: rule params (for headers / timeout config)
    Returns:
        Parsed JSON response (dict)
    """
    url = f"{base_url}/${operation}"
    timeout_sec = float(params.get('gpas_timeout_sec', 30))
    payload = json.dumps(fhir_params).encode('utf-8')

    gpas_log.info("calling %s with %d parameter(s)", url,
                  len(fhir_params.get("parameter", [])))

    retry_count = int(params.get('gpas_retry_count', os.environ.get('GPAS_RETRY_COUNT', 2)))
    retry_backoff = float(params.get('gpas_retry_backoff_sec', os.environ.get('GPAS_RETRY_BACKOFF_SEC', 0.2)))

    t0 = time.perf_counter()
    for attempt in range(retry_count + 1):
        req = request.Request(
            url=url,
            data=payload,
            headers=_resolve_gpas_headers(params),
            method='POST',
        )
        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                charset = resp.headers.get_content_charset('utf-8')
                body = resp.read().decode(charset)
                GPAS_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
                GPAS_CALL_COUNT.labels(operation=operation, status='ok').inc()
                return json.loads(body)
        except urlerror.HTTPError as exc:
            should_retry = exc.code in (429, 500, 502, 503, 504)
            if should_retry and attempt < retry_count:
                time.sleep(retry_backoff * (2 ** attempt))
                continue

            detail = exc.read().decode('utf-8', errors='replace') if exc.fp else str(exc)
            try:
                diagnostics = []
                outcome = json.loads(detail)
                for issue in outcome.get('issue', []):
                    if issue.get('diagnostics'):
                        diagnostics.append(issue['diagnostics'])
                detail_message = '; '.join(diagnostics) if diagnostics else detail
            except Exception:
                detail_message = detail

            if 'Unknown domain' in detail_message:
                try:
                    domains = list_gpas_domains(params)
                    if domains:
                        detail_message = (
                            f"{detail_message} Available domains: {', '.join(domains)}"
                        )
                except Exception:
                    pass
            GPAS_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            GPAS_CALL_COUNT.labels(operation=operation, status='error').inc()
            raise ValueError(f'gPAS HTTP {exc.code} on ${operation}: {detail_message}') from exc
        except urlerror.URLError as exc:
            if attempt < retry_count:
                time.sleep(retry_backoff * (2 ** attempt))
                continue
            GPAS_LATENCY.labels(operation=operation).observe(time.perf_counter() - t0)
            GPAS_CALL_COUNT.labels(operation=operation, status='error').inc()
            raise ValueError(f'gPAS connection error on ${operation}: {exc.reason}') from exc

    raise ValueError(f'gPAS request failed on ${operation}')


# -- Standalone pseudonymization helper --------------------------------------

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


def gpas_pseudonymize_value(original_value, params):
    """Pseudonymize a single value via gPAS and return the pseudonym string.

    This is a lower-level helper used by the reference-rewriting pass.
    It shares the same cache as the path-based action handlers.
    """
    mapping = gpas_pseudonymize_batch([str(original_value)], params)
    return mapping.get(str(original_value))


# -- Public action handlers --------------------------------------------------
# These follow the same signature as all other SPE-FHIR-BlackBox actions:
#   action_by_path(resource, el, params)
# where el = {'path': 'Patient.id', 'value': 'some-value'}

def gpas_pseudonymize_by_path(resource, el, params):
    """Pseudonymize a single matched value via gPAS $pseudonymizeAllowCreate.

    Required params:
        gpas_url: gPAS base URL (e.g. https://host:port/ttp-fhir/fhir/gpas)
        gpas_domain: gPAS domain name (e.g. "MIRACUM")

    Optional params:
        gpas_operation: "pseudonymizeAllowCreate" (default) or "pseudonymize"
        gpas_token / GPAS_TOKEN env: Bearer token for auth
        gpas_timeout_sec: HTTP timeout (default 30)
    """
    original_value = str(el['value']) if not isinstance(el['value'], dict) else json.dumps(el['value'])
    mapping = gpas_pseudonymize_batch([original_value], params)
    pseudonym = mapping.get(original_value)

    if pseudonym is None:
        raise ValueError(f'gPAS did not return a pseudonym for value (path={el["path"]})')

    path = el['path'].split('.')[1:]
    if len(path) == 0:
        resource.clear()
        return
    ret = find_nodes(resource, path[:-1], [])
    _substitute_nodes(ret, path[-1], el['value'], pseudonym)


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


