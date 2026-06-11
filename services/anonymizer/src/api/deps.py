"""Shared FastAPI dependencies — rate limiter, settings loader, SSRF protection, helpers.

Imported by main.py and all router modules. No imports from api.main or api.routers
to keep the dependency graph acyclic.
"""

import asyncio
import ipaddress
import os
import socket
import urllib.parse
from typing import Any

from fastapi import HTTPException, Query

from pydantic import ValidationError as _ValidationError

from api.schemas.processing import DynamicSettings as _DynamicSettings

import pipeline.config as config
from pipeline.config.service import get_settings  # noqa: F401 — re-exported for router imports

# ---------------------------------------------------------------------------
# Rate limiting (slowapi dependency)
# ---------------------------------------------------------------------------
try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.util import get_remote_address
except ImportError:
    # slowapi is optional — no-op fallback for tests and lightweight deployments.
    class _NoOpLimiter:
        def __init__(self, **kw):
            pass

        def limit(self, *a, **kw):
            def decorator(fn):
                return fn

            return decorator

    Limiter = _NoOpLimiter
    RateLimitExceeded = None

    def get_remote_address(r):
        return "127.0.0.1"

    _rate_limit_exceeded_handler = None

_RATE_LIMIT_ENABLED = os.environ.get("MEDANON_RATE_LIMIT_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)


def _get_client_ip(request) -> str:
    """Return the real client IP for rate-limiting purposes.

    Prefers X-Real-IP (set by the trusted UI nginx to $remote_addr, which cannot
    be spoofed by the client).  Falls back to the direct connection IP from ASGI
    scope.  Explicitly ignores X-Forwarded-For because its first element can be
    forged by a client to bypass rate limiting.
    """
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip
    # ASGI direct-connection IP — safe when no trusted proxy is in front.
    client = getattr(request, "client", None)
    if client and client.host:
        return client.host
    return "unknown"


# Use Redis-backed storage when available so rate-limit counters are shared
# across all anonymizer replicas (required for correct horizontal scaling).
# Falls back to in-process memory when MEDANON_REDIS_URL is not set.
_redis_url = os.environ.get("MEDANON_REDIS_URL", "")
_limiter_storage_uri = (
    _redis_url.rstrip("/") + "/2"  # DB 2: rate-limit counters
    if _redis_url
    else "memory://"
)
limiter = Limiter(
    key_func=_get_client_ip,
    enabled=_RATE_LIMIT_ENABLED,
    storage_uri=_limiter_storage_uri,
)

# Maximum accepted request body size (10 MB default).
MAX_BODY_BYTES = int(os.environ.get("MEDANON_MAX_BODY_BYTES", 10 * 1024 * 1024))

# ---------------------------------------------------------------------------
# SSRF protection — reject server_url values targeting private/loopback space
# ---------------------------------------------------------------------------
_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 carrier-grade NAT
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),  # IPv6 link-local
    ipaddress.ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6 (belt-and-suspenders)
]


def _effective_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the canonical IP for SSRF checks.

    IPv4-mapped IPv6 addresses (``::ffff:x.x.x.x``) are unmapped to their
    underlying IPv4 form so that private-range checks against IPv4 networks
    (e.g. ``127.0.0.0/8``) are not silently bypassed.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


async def _validate_server_url(url: str) -> str:
    """Raise HTTP 422 if *url* is not a safe http(s) URL.

    Blocks:
    - Non-http(s) schemes (file://, gopher://, etc.)
    - Raw IP addresses in private / loopback ranges
    - DNS names that resolve to private / loopback addresses
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
        raise HTTPException(
            status_code=422, detail="server_url must contain a hostname"
        )
    try:
        addr = _effective_ip(ipaddress.ip_address(hostname))
        if any(addr in net for net in _PRIVATE_NETS):
            raise HTTPException(
                status_code=422,
                detail="server_url must not target private or loopback addresses",
            )
    except ValueError:
        # hostname is a DNS name — resolve and check against private ranges.
        # DNS resolution failure is not an SSRF concern (host simply doesn't
        # exist from this network); the connection will fail at request time.
        err = await asyncio.to_thread(check_hostname_ssrf, hostname)
        if err and "resolves to private address" in err:
            raise HTTPException(
                status_code=422,
                detail=f"server_url rejected: {err}",
            )
    return url


def check_hostname_ssrf(hostname: str) -> str | None:
    """Resolve *hostname* via DNS and check all addresses against private ranges.

    Returns an error message if any resolved address is private/loopback,
    or ``None`` when the hostname is safe.
    """
    try:
        results = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}"
    for family, _, _, _, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            addr = _effective_ip(ipaddress.ip_address(ip_str))
            if any(addr in net for net in _PRIVATE_NETS):
                return f"Hostname {hostname} resolves to private address {ip_str}"
        except ValueError:
            continue
    return None


async def _get_url_from_request_or_env(
    user_url: str | None,
    env_var: str,
    field_name: str = "server_url",
) -> str:
    """Get URL from an optional user-provided value or environment variable.

    SSRF protection applies ONLY to user-provided URLs (untrusted input).
    Environment variables are trusted configuration set by administrators.
    """
    env_url = os.environ.get(env_var)

    if user_url:
        await _validate_server_url(user_url)
        return user_url.rstrip("/")
    elif env_url:
        return env_url.rstrip("/")
    else:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} required (provide in request body or set {env_var} env var)",
        )


# ---------------------------------------------------------------------------
# settings validation — prevents SSRF via FHIR Parameters wrapper
# ---------------------------------------------------------------------------


async def _validate_dynamic_settings(dynamic_settings: dict) -> None:
    """Raise HTTP 422 for unknown or unsafe URL-valued dynamic settings.

    Uses ``DynamicSettings`` (extra='forbid') to reject unrecognised keys,
    then SSRF-validates any user-supplied URL values.
    """
    if not dynamic_settings:
        return
    try:
        parsed = _DynamicSettings.model_validate(dynamic_settings)
    except _ValidationError as exc:
        # Surface extra-field errors as the well-known "Unsupported dynamic setting" message
        for err in exc.errors():
            if err.get("type") == "extra_forbidden" and err.get("loc"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Unsupported dynamic setting: {err['loc'][0]!r}",
                ) from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if parsed.gpas_url:
        await _validate_server_url(parsed.gpas_url)


def get_settings_dep(
    config_profile: str = Query(
        "auto",
        description="Config profile: auto, minimal, gpas, gdpr, hipaa, research, structural, value-masking",
    ),
) -> config.Settings:
    """FastAPI dependency that reads config_profile from the query string."""
    return get_settings(config_profile)


# ---------------------------------------------------------------------------
# Shared processing helpers
# ---------------------------------------------------------------------------


def _runtime_settings(base_settings, dynamic_settings=None):
    """Build a lightweight runtime settings object merging base config + dynamic overrides."""
    from api.schemas.processing import RuntimeSettings

    # Propagate filename so the rule_matcher index cache can use a stable key.
    filename = getattr(base_settings, "filename", None)
    return RuntimeSettings(
        rules=getattr(base_settings, "rules", []),
        processing_errors=getattr(base_settings, "processing_errors", "raise"),
        rewrite_references=getattr(base_settings, "rewrite_references", False),
        rewrite_text_ids=getattr(base_settings, "rewrite_text_ids", False),
        dynamic_rule_settings=dynamic_settings or {},
        filename=filename,
        domain_map=getattr(base_settings, "domain_map", {}),
    )


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


def _unwrap_parameters_payload(payload):
    """Support Parameters(resource, settings) wrapper for dynamic rule settings.

    Mirrors a useful pattern from miracum/fhir-pseudonymizer while keeping the
    core processing engine unchanged.
    """
    if not isinstance(payload, dict) or payload.get("resourceType") != "Parameters":
        return payload, None

    parameters = payload.get("parameter", [])
    dynamic_settings = {}
    wrapped_resource = None

    for p in parameters:
        if not isinstance(p, dict):
            continue
        if p.get("name") == "settings":
            for part in p.get("part", []):
                if isinstance(part, dict) and part.get("name"):
                    dynamic_settings[part["name"]] = _extract_value_part(part)
        elif p.get("name") == "resource" and isinstance(p.get("resource"), dict):
            wrapped_resource = p["resource"]

    if wrapped_resource is None:
        return payload, None
    return wrapped_resource, dynamic_settings


def _extract_value_part(part):
    for key, value in part.items():
        if key.startswith("value"):
            return value
    return None
