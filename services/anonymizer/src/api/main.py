import ipaddress
import json
import logging
import os
import time
import urllib.parse
import uuid
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, Response
from functools import lru_cache

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import pipeline.config as config
from pipeline.io_formats import parse_payload_bytes, serialize_payload
from pipeline.processor import process_data
from utils.logging import REQUEST_ID, setup_logging
from utils.metrics import REQUEST_COUNT, REQUEST_LATENCY
from analytics.risk import assess_risk as _assess_risk, assess_risk_resources as _assess_risk_resources
from analytics.synthetic import generate_synthetic_patients as _generate_synthetic_patients
from integrations.fhir.client import (
    fetch_all_resource_types,
    fetch_everything,
    get_capability_statement,
    post_resource,
    upload_resources,
)

setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("medanon")

# Maximum accepted request body size (10 MB) to prevent memory exhaustion.
MAX_BODY_BYTES = int(os.environ.get("MEDANON_MAX_BODY_BYTES", 10 * 1024 * 1024))

# Optional API key auth. If MEDANON_API_KEY is set (non-empty), all endpoints
# except health/ready/metrics/docs require the X-API-Key header to match.
_API_KEY = os.environ.get("MEDANON_API_KEY", "").strip()
_OPEN_PATHS = frozenset({"/health", "/ready", "/metrics", "/docs", "/openapi.json", "/redoc"})

# Directory where config YAML files are located.
# Defaults to /code/config (set in Dockerfile); override for local dev.
_CONFIG_DIR = os.environ.get("MEDANON_CONFIG_DIR", "/code/config")

_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get(
    "MEDANON_CORS_ORIGINS", ""
).split(",") if o.strip()]

# Rate limiting. Enabled by default; set MEDANON_RATE_LIMIT_ENABLED=false to
# disable (e.g. for local dev or when running behind a reverse proxy that
# already enforces limits).
_RATE_LIMIT_ENABLED = os.environ.get("MEDANON_RATE_LIMIT_ENABLED", "true").lower() in (
    "1", "true", "yes"
)
limiter = Limiter(key_func=get_remote_address, enabled=_RATE_LIMIT_ENABLED)

# ---------------------------------------------------------------------------
# SSRF protection — reject server_url values targeting private/loopback space
# ---------------------------------------------------------------------------
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _validate_server_url(url: str) -> str:
    """Raise HTTP 422 if *url* is not a safe http(s) URL.

    Blocks:
    - Non-http(s) schemes (file://, gopher://, etc.)
    - Raw IP addresses in private / loopback ranges

    DNS names are allowed at this layer; tighten further with
    ALLOWED_FHIR_HOSTS env-var allowlist if needed.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid server_url")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=422,
            detail="server_url must use http or https scheme",
        )
    hostname = parsed.hostname or ""
    if not hostname:
        raise HTTPException(status_code=422, detail="server_url must contain a hostname")
    try:
        addr = ipaddress.ip_address(hostname)
        if any(addr in net for net in _PRIVATE_NETS):
            raise HTTPException(
                status_code=422,
                detail="server_url must not target private or loopback addresses",
            )
    except ValueError:
        pass  # hostname is a DNS name — permitted
    return url


# ---------------------------------------------------------------------------
# Dynamic settings validation — prevents SSRF via FHIR Parameters wrapper
# ---------------------------------------------------------------------------
_ALLOWED_DYNAMIC_SETTINGS = frozenset({
    'gpas_url', 'gpas_domain', 'gpas_operation', 'gpas_token',
    'gpas_basic_user', 'gpas_basic_pass', 'gpas_timeout_sec',
    'gpas_retry_count', 'processing_errors', 'rewrite_references',
    'rewrite_text_ids',
})
_DYNAMIC_URL_KEYS = frozenset({'gpas_url'})


def _validate_dynamic_settings(dynamic_settings: dict) -> None:
    """Raise HTTP 422 for unknown or unsafe URL-valued dynamic settings."""
    if not dynamic_settings:
        return
    for key in dynamic_settings:
        if key not in _ALLOWED_DYNAMIC_SETTINGS:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported dynamic setting: {key!r}",
            )
    for key in _DYNAMIC_URL_KEYS:
        val = dynamic_settings.get(key)
        if val:
            _validate_server_url(str(val))


@lru_cache()
def get_settings():
    # Auto-select gPAS profile when GPAS_URL is configured, otherwise local-only
    if os.environ.get('GPAS_URL'):
        return config.Settings(os.path.join(_CONFIG_DIR, 'config_gpas.yaml'))
    return config.Settings(os.path.join(_CONFIG_DIR, 'config.yaml'))


def _extract_value_part(part):
    for key, value in part.items():
        if key.startswith('value'):
            return value
    return None


def _unwrap_parameters_payload(payload):
    """Support Parameters(resource, settings) wrapper for dynamic rule settings.

    Mirrors a useful pattern from miracum/fhir-pseudonymizer while keeping the
    core processing engine unchanged.
    """
    if not isinstance(payload, dict) or payload.get('resourceType') != 'Parameters':
        return payload, None

    parameters = payload.get('parameter', [])
    dynamic_settings = {}
    wrapped_resource = None

    for p in parameters:
        if not isinstance(p, dict):
            continue
        if p.get('name') == 'settings':
            for part in p.get('part', []):
                if isinstance(part, dict) and part.get('name'):
                    dynamic_settings[part['name']] = _extract_value_part(part)
        elif p.get('name') == 'resource' and isinstance(p.get('resource'), dict):
            wrapped_resource = p['resource']

    if wrapped_resource is None:
        return payload, None
    return wrapped_resource, dynamic_settings


def _runtime_settings(base_settings, dynamic_settings=None):
    return type('RuntimeSettings', (), {
        'rules': getattr(base_settings, 'rules', []),
        'processing_errors': getattr(base_settings, 'processing_errors', 'raise'),
        'rewrite_references': getattr(base_settings, 'rewrite_references', False),
        'dynamic_rule_settings': dynamic_settings or {},
    })()


def _unwrap_to_resources(payload: Any) -> list[dict]:
    """Normalize any parsed FHIR payload to a flat list of resource dicts.

    Handles:
    - list (from NDJSON parse) → returned as-is
    - Bundle dict → extracts entry[].resource
    - single resource dict → wrapped in a list
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        if payload.get("resourceType") == "Bundle":
            return [
                entry["resource"]
                for entry in payload.get("entry", [])
                if isinstance(entry.get("resource"), dict)
            ]
        return [payload]
    return []


# ---------------------------------------------------------------------------
# Shared request helpers
# ---------------------------------------------------------------------------

async def _parse_json_body(request: Request) -> dict:
    """Read and parse a JSON request body; raises 422 on invalid JSON."""
    body = await request.body()
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc


def _get_timeout(req_data: dict, default: float = 30.0) -> float:
    """Extract and validate the timeout field; raises 422 on bad value."""
    try:
        return float(req_data.get("timeout", default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid timeout value: {exc}") from exc


def _resolve_resource_types(server_url: str, req_data: dict, token, timeout: float) -> list:
    """Return resource_types from request data or auto-discover from /metadata.

    Raises 502 if the source server is unreachable.
    """
    resource_types = req_data.get("resource_types")
    if not resource_types:
        try:
            resource_types = get_capability_statement(server_url, token=token, timeout=timeout)
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach FHIR server: {exc}",
            ) from exc
    return resource_types


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(title="MedAnon", version="2.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["POST", "GET"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    """Enforce X-API-Key header when MEDANON_API_KEY env var is set."""
    if _API_KEY and request.url.path not in _OPEN_PATHS:
        provided = request.headers.get("X-API-Key", "")
        if provided != _API_KEY:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def enforce_body_size(request: Request, call_next):
    """Reject requests whose Content-Length exceeds MAX_BODY_BYTES."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            cl_int = int(content_length)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")
        if cl_int > MAX_BODY_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024*1024)} MB limit",
            )
    return await call_next(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Propagate or generate an X-Request-ID for end-to-end tracing."""
    rid = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    token = REQUEST_ID.set(rid)
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        REQUEST_ID.reset(token)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Record per-endpoint request count and latency for Prometheus."""
    start = time.perf_counter()
    response = await call_next(request)
    REQUEST_LATENCY.labels(endpoint=request.url.path).observe(
        time.perf_counter() - start
    )
    REQUEST_COUNT.labels(
        endpoint=request.url.path,
        status_code=str(response.status_code),
    ).inc()
    return response


@app.get("/")
async def read_root(settings: config.Settings = Depends(get_settings)):
    return RedirectResponse("/docs")


@app.get("/health")
def health():
    """Liveness probe — returns 200 when the process is alive."""
    return {"status": "ok"}


@app.get("/ready")
def readiness(request: Request):
    """Readiness probe — verifies that configured upstream services are reachable.

    Checks /metadata on gPAS and FHIR servers (when their URLs are configured).
    Returns 200 + {"ready": true} when all checks pass, 503 otherwise.
    Timeout is controlled by MEDANON_READY_TIMEOUT (default 5 s).

    When an API key is configured, unauthenticated callers receive only
    {"ready": true/false} without upstream service details to avoid
    leaking internal network topology.
    """
    import urllib.request as _ureq
    import urllib.error
    timeout = float(os.environ.get("MEDANON_READY_TIMEOUT", "5.0"))
    checks: dict[str, str] = {}

    gpas_url = os.environ.get("GPAS_URL", "")
    if gpas_url:
        try:
            _ureq.urlopen(gpas_url.rstrip("/"), timeout=timeout)
            checks["gpas"] = "ok"
        except urllib.error.HTTPError:
            # Any HTTP response (400, 401, etc.) means the service is reachable
            checks["gpas"] = "ok"
        except Exception as exc:
            logger.debug("readiness: gpas unreachable: %s", exc)
            checks["gpas"] = "error"

    fhir_url = os.environ.get("FHIR_SOURCE_URL", "")
    if fhir_url:
        try:
            _ureq.urlopen(f"{fhir_url.rstrip('/')}/metadata", timeout=timeout)
            checks["fhir"] = "ok"
        except Exception as exc:
            logger.debug("readiness: fhir unreachable: %s", exc)
            checks["fhir"] = "error"

    ready = all(v == "ok" for v in checks.values())

    # Unauthenticated callers only get the binary ready status — no topology details
    caller_authenticated = (
        not _API_KEY or request.headers.get("X-API-Key", "") == _API_KEY
    )
    body = {"ready": ready}
    if caller_authenticated:
        body["checks"] = checks

    return Response(
        content=json.dumps(body),
        status_code=200 if ready else 503,
        media_type="application/json",
    )


@app.get("/metrics")
def metrics():
    """Prometheus metrics endpoint."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/process")
@limiter.limit("60/minute")
def process(request: Request, resource: Any = Body(...), settings: config.Settings = Depends(get_settings)):
    """Process a single FHIR resource or a FHIR Bundle.

    Accepts any valid FHIR JSON object or a JSON array of resources.
    Applies the configured rules and returns the pseudonymized/de-identified result.
    """
    if not isinstance(resource, (dict, list)):
        raise HTTPException(status_code=422, detail="Request body must be a FHIR JSON object or array")

    resource, dynamic_settings = _unwrap_parameters_payload(resource)
    if dynamic_settings:
        _validate_dynamic_settings(dynamic_settings)
    runtime_settings = _runtime_settings(settings, dynamic_settings)

    resource_type = resource.get("resourceType", "unknown") if isinstance(resource, dict) else "array"
    logger.info("Processing request: resourceType=%s", resource_type)

    try:
        result = process_data(resource, runtime_settings)
        logger.info("Processing complete: resourceType=%s", resource_type)
        return result
    except ValueError as exc:
        logger.warning("Validation error processing resourceType=%s: %s", resource_type, exc)
        raise HTTPException(status_code=422, detail="Invalid input") from exc
    except NotImplementedError as exc:
        logger.warning("Not-implemented action for resourceType=%s: %s", resource_type, exc)
        raise HTTPException(status_code=400, detail="Unsupported operation") from exc
    except Exception as exc:
        # Do not use logger.exception — traceback may contain PHI from resource processing
        logger.error("Unexpected error processing resourceType=%s: %s", resource_type, type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Unexpected processing error") from exc


@app.post("/process/ndjson")
@limiter.limit("20/minute")
async def process_ndjson(request: Request, settings: config.Settings = Depends(get_settings)):
    """Process a stream of newline-delimited FHIR resources (NDJSON / x-ndjson).

    Each non-empty line must be a valid JSON object representing a FHIR resource.
    Lines starting with '//' are treated as comments and skipped.
    Returns a streaming NDJSON response — one processed JSON object per line.
    """
    import json

    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024*1024)} MB limit")

    lines = body.decode("utf-8").splitlines()
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for lineno, raw in enumerate(lines, start=1):
            if await request.is_disconnected():
                logger.info("NDJSON: client disconnected at line %d", lineno)
                break
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("NDJSON line %d: JSON parse error — %s", lineno, exc)
                yield json.dumps({"error": f"line {lineno}: invalid JSON — {exc}"}) + "\n"
                continue
            try:
                result = process_data(resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                logger.error("NDJSON line %d: processing error: %s", lineno, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": f"line {lineno}: processing error"}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@app.post('/process/raw')
@limiter.limit("60/minute")
async def process_raw(
    request: Request,
    output_format: str = 'json',
    input_format: str = 'auto',
    settings: config.Settings = Depends(get_settings),
):
    """Black-box endpoint supporting JSON, XML, and NDJSON input/output.

    Input detection:
    - input_format=auto uses Content-Type to infer json/xml/ndjson
    - input_format can be forced to json/xml/ndjson

    Output:
    - output_format in {json, xml, ndjson}
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024*1024)} MB limit")

    try:
        payload = parse_payload_bytes(
            body,
            in_format=input_format,
            content_type=request.headers.get('content-type'),
        )
    except ValueError as exc:
        logger.warning("parse error in /process/raw: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid input format") from exc

    try:
        payload, dynamic_settings = _unwrap_parameters_payload(payload)
        if dynamic_settings:
            _validate_dynamic_settings(dynamic_settings)
        runtime_settings = _runtime_settings(settings, dynamic_settings)

        if isinstance(payload, list):
            result = [process_data(item, runtime_settings) for item in payload]
        else:
            result = process_data(payload, runtime_settings)
        text, media_type = serialize_payload(result, out_format=output_format)
        return Response(content=text, media_type=media_type)
    except ValueError as exc:
        logger.warning("validation error in /process/raw: %s", exc)
        raise HTTPException(status_code=422, detail="Invalid input") from exc
    except Exception as exc:
        # Do not use logger.exception — traceback may contain PHI
        logger.error('Unexpected error in /process/raw: %s', type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail='Unexpected processing error') from exc


@app.post('/process/from-server')
@limiter.limit("10/minute")
async def process_from_server(
    request: Request,
    settings: config.Settings = Depends(get_settings),
):
    """Fetch FHIR resources directly from a FHIR server, anonymize, and return NDJSON.

    Request body (JSON):
    ```json
    {
      "server_url": "http://host:8080/fhir",
      "resource_types": ["Patient", "Observation"],
      "params": {"_count": 100},
      "token": "optional-bearer-token"
    }
    ```

    Returns streaming NDJSON — one anonymized resource per line.
    """
    req_data = await _parse_json_body(request)

    server_url = req_data.get("server_url")
    if not server_url:
        raise HTTPException(status_code=422, detail="server_url is required")
    _validate_server_url(server_url)

    extra_params = req_data.get("params") or {}
    token = req_data.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = _get_timeout(req_data)
    resource_types = _resolve_resource_types(server_url, req_data, token, timeout)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for _rt, resource in fetch_all_resource_types(
            server_url, resource_types, params=extra_params, token=token, timeout=timeout
        ):
            if await request.is_disconnected():
                logger.info("from-server: client disconnected, stopping stream")
                break
            try:
                result = process_data(resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                logger.error("Error processing resource type=%s: %s", _rt, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": "processing error", "resourceType": _rt}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@app.post('/process/everything')
@limiter.limit("10/minute")
async def process_everything(
    request: Request,
    settings: config.Settings = Depends(get_settings),
):
    """Fetch all resources via FHIR $everything, anonymize, and return NDJSON.

    Calls ``GET {server_url}/{resource_type}/{resource_id}/$everything`` and
    de-identifies every resource in the response Bundle.

    Request body (JSON):
    ```json
    {
      "server_url":    "http://host:8080/fhir",
      "resource_type": "Patient",
      "resource_id":   "DDME",
      "params":        {"_count": 50},
      "token":         "optional-bearer-token"
    }
    ```

    Returns streaming NDJSON — one anonymized resource per line.
    """
    req_data = await _parse_json_body(request)

    server_url = req_data.get("server_url") or os.environ.get("FHIR_SOURCE_URL")
    if not server_url:
        raise HTTPException(status_code=422, detail="server_url is required")
    _validate_server_url(server_url)

    resource_type = req_data.get("resource_type")
    if not resource_type:
        raise HTTPException(status_code=422, detail="resource_type is required")

    resource_id = req_data.get("resource_id")
    if not resource_id:
        raise HTTPException(status_code=422, detail="resource_id is required")

    extra_params = req_data.get("params") or {}
    token = req_data.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = _get_timeout(req_data)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for resource in fetch_everything(
            server_url, resource_type, resource_id,
            params=extra_params, token=token, timeout=timeout,
        ):
            if await request.is_disconnected():
                logger.info("everything: client disconnected, stopping stream")
                break
            try:
                result = process_data(resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                _rtype = resource.get("resourceType", resource_type)
                logger.error("Error processing resource type=%s: %s", _rtype, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": "processing error", "resourceType": _rtype}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@app.post("/process/and-upload")
@limiter.limit("10/minute")
async def process_and_upload(
    request: Request,
    settings: config.Settings = Depends(get_settings),
):
    """De-identify a FHIR resource (or Bundle) and upload the result to a FHIR server.

    Request body (JSON):
    ```json
    {
      "target_server_url": "http://hapi-fhir:8080/fhir",
      "resource": { ...FHIR resource or Bundle... },
      "target_token": "optional-bearer-token",
      "timeout": 30
    }
    ```

    Returns a JSON summary:
    ```json
    { "uploaded": 2, "errors": 0, "results": [...] }
    ```
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds {MAX_BODY_BYTES // (1024*1024)} MB limit")
    try:
        req_data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON body: {exc}") from exc

    target_url = req_data.get("target_server_url") or os.environ.get("FHIR_TARGET_URL")
    if not target_url:
        raise HTTPException(status_code=422, detail="target_server_url is required")
    _validate_server_url(target_url)

    resource = req_data.get("resource")
    if not resource or not isinstance(resource, dict):
        raise HTTPException(status_code=422, detail="resource must be a FHIR JSON object")

    target_token = req_data.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = _get_timeout(req_data)

    resource, dynamic_settings = _unwrap_parameters_payload(resource)
    if dynamic_settings:
        _validate_dynamic_settings(dynamic_settings)
    runtime_settings = _runtime_settings(settings, dynamic_settings)

    try:
        deidentified = process_data(resource, runtime_settings)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Do not log exception — traceback may contain PHI
        logger.error("process_and_upload: de-identification error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="De-identification error") from exc

    # Flatten Bundle entries or wrap single resource into a list
    if isinstance(deidentified, dict) and deidentified.get("resourceType") == "Bundle":
        resources_to_upload = [
            entry["resource"]
            for entry in deidentified.get("entry", [])
            if isinstance(entry.get("resource"), dict)
        ]
    elif isinstance(deidentified, list):
        resources_to_upload = deidentified
    else:
        resources_to_upload = [deidentified]

    results = list(upload_resources(target_url, resources_to_upload, token=target_token, timeout=timeout))
    uploaded = sum(1 for r in results if r["success"])
    errors = sum(1 for r in results if not r["success"])

    logger.info("process_and_upload: uploaded=%d errors=%d target=%s", uploaded, errors, target_url)
    return {"uploaded": uploaded, "errors": errors, "results": results}


@app.post("/process/round-trip")
@limiter.limit("10/minute")
async def process_round_trip(
    request: Request,
    settings: config.Settings = Depends(get_settings),
):
    """Fetch from a source FHIR server, de-identify, and upload to a target server.

    Request body (JSON):
    ```json
    {
      "source_server_url": "http://source-fhir:8080/fhir",
      "target_server_url": "http://hapi-fhir:8080/fhir",
      "resource_types":    ["Patient", "Observation"],
      "params":            {"_count": 100},
      "source_token":      "optional",
      "target_token":      "optional",
      "timeout":           30
    }
    ```

    If ``resource_types`` is omitted, types are auto-discovered from
    the source server's ``/metadata`` capability statement.

    Returns streaming NDJSON — one status line per resource.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail=f"Request body exceeds {MAX_BODY_BYTES // (1024*1024)} MB limit")

    req_data = await _parse_json_body(request)

    source_url = req_data.get("source_server_url") or os.environ.get("FHIR_SOURCE_URL")
    target_url = req_data.get("target_server_url") or os.environ.get("FHIR_TARGET_URL")
    if not source_url:
        raise HTTPException(status_code=422, detail="source_server_url is required")
    if not target_url:
        raise HTTPException(status_code=422, detail="target_server_url is required")
    _validate_server_url(source_url)
    _validate_server_url(target_url)

    extra_params = req_data.get("params") or {}
    source_token = req_data.get("source_token") or os.environ.get("FHIR_SOURCE_TOKEN")
    target_token = req_data.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = _get_timeout(req_data)
    resource_types = _resolve_resource_types(source_url, req_data, source_token, timeout)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for _rt, resource in fetch_all_resource_types(
            source_url, resource_types, params=extra_params, token=source_token, timeout=timeout
        ):
            if await request.is_disconnected():
                logger.info("round-trip: client disconnected, stopping stream")
                break
            source_id = resource.get("id")
            try:
                deidentified = process_data(resource, runtime_settings)
                resp = post_resource(target_url, deidentified, token=target_token, timeout=timeout)
                yield json.dumps({
                    "resourceType": _rt,
                    "target_id": resp.get("id"),
                    "status": "ok",
                }) + "\n"
            except Exception as exc:
                # Do not log exception — traceback may contain PHI
                logger.error("round_trip error type=%s: %s", _rt, type(exc).__name__, exc_info=False)
                yield json.dumps({
                    "resourceType": _rt,
                    "status": "error",
                    "error": "processing error",
                }) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@app.post("/analyse/risk")
@limiter.limit("30/minute")
async def analyse_risk(request: Request):
    """Compute re-identification risk metrics on de-identified FHIR resources.

    Accepts NDJSON, JSON (single resource or Bundle), or XML input.
    Format is auto-detected from the Content-Type header.

    Patient resources supply quasi-identifiers (gender, birth year, zip prefix)
    for k-anonymity.  Condition resources are correlated via ``subject.reference``
    and used for l-diversity.  Other resource types are ignored.

    Returns a JSON risk report with:
    - k-anonymity (min_k, equivalence classes)
    - Prosecutor / journalist / marketer re-identification risk scores
    - Risk level: low (k>=5) / medium (k>=3) / high (k>=2) / critical (k=1)
    - l-diversity (if Condition resources are present)
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")
    try:
        payload = parse_payload_bytes(body, content_type=content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse input: {exc}") from exc

    resources = _unwrap_to_resources(payload)

    try:
        report = _assess_risk_resources(resources)
    except ValueError as exc:
        logger.warning("analyse_risk: value error: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("analyse_risk: unexpected error type=%s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Risk analysis error") from exc

    return JSONResponse(content=report)


@app.post("/process/batch")
@limiter.limit("20/minute")
async def process_batch(request: Request, settings: config.Settings = Depends(get_settings)):
    """Process FHIR resources in any format (JSON, NDJSON, XML) and stream NDJSON output.

    Accepts:
    - ``application/x-ndjson`` — one resource per line (same as /process/ndjson)
    - ``application/json`` / ``application/fhir+json`` — single resource or Bundle
    - ``application/xml`` / ``application/fhir+xml`` — single resource or Bundle

    Bundles are automatically unwrapped: each ``entry.resource`` is processed
    individually.  Returns a streaming NDJSON response.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")
    try:
        payload = parse_payload_bytes(body, content_type=content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse input: {exc}") from exc

    resources = _unwrap_to_resources(payload)
    runtime_settings = _runtime_settings(settings)

    async def _generate():
        for idx, resource in enumerate(resources):
            if await request.is_disconnected():
                logger.info("process_batch: client disconnected at resource %d", idx)
                break
            try:
                result = process_data(resource, runtime_settings)
                yield json.dumps(result) + "\n"
            except Exception as exc:
                logger.error("process_batch resource %d: %s", idx, type(exc).__name__, exc_info=False)
                yield json.dumps({"error": f"resource {idx}: processing error"}) + "\n"

    return StreamingResponse(_generate(), media_type="application/x-ndjson")


@app.post("/generate/synthetic")
@limiter.limit("30/minute")
async def generate_synthetic(
    request: Request,
    count: int = Query(100, ge=1, le=10000,
                       description="Number of synthetic Patient resources to generate"),
    seed: int | None = Query(None, description="Random seed for reproducibility"),
):
    """Generate synthetic FHIR Patient resources from a de-identified input dataset.

    Accepts de-identified FHIR Patient resources (NDJSON, JSON Bundle, or XML),
    extracts statistical distributions (gender, birth year, zip prefix), and
    returns *count* synthetic Patient resources as NDJSON.

    Synthetic resources are tagged with the ``SYN`` code in ``meta.tag`` so
    downstream systems can distinguish them from real de-identified data.

    Args:
        count: Number of synthetic patients to return (1–10 000, default 100).
        seed: Optional random seed — set for reproducible output.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")
    try:
        payload = parse_payload_bytes(body, content_type=content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse input: {exc}") from exc

    resources = _unwrap_to_resources(payload)
    patients = [r for r in resources if r.get("resourceType") == "Patient"]

    if not patients:
        raise HTTPException(
            status_code=422,
            detail="No Patient resources found in input — provide de-identified Patient FHIR resources",
        )

    try:
        synthetic = _generate_synthetic_patients(patients, count=count, seed=seed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("generate_synthetic: unexpected error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Synthetic generation error") from exc

    async def _stream():
        for patient in synthetic:
            yield json.dumps(patient) + "\n"

    return StreamingResponse(_stream(), media_type="application/x-ndjson")
