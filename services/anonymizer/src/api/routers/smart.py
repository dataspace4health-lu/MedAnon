"""SMART on FHIR protocol endpoints.

Implements the minimal endpoint set for SMART App Launch Framework
compatibility (HL7 FHIR SMART App Launch 2.0):

    GET  /.well-known/smart-configuration  — server capability discovery (RFC 8414)
    POST /oauth2/introspect                — token introspection (RFC 7662)

Configuration env vars (all optional):
    SMART_AUTHORIZATION_URL  — OAuth2 authorization endpoint
    SMART_TOKEN_URL          — OAuth2 token endpoint
    SMART_INTROSPECTION_URL  — Upstream introspection endpoint to proxy to
    SMART_JWKS_URL           — JWKS URI for token verification
    SMART_ISSUER             — Token issuer (iss claim); defaults to request base URL
"""

import asyncio
import hmac
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter()
_log = logging.getLogger("medanon")

_SMART_CAPABILITIES = [
    "launch-ehr",
    "launch-standalone",
    "client-public",
    "client-confidential-symmetric",
    "sso-openid-connect",
    "context-passthrough-banner",
    "context-style",
    "context-ehr-patient",
    "context-ehr-encounter",
    "context-standalone-patient",
    "context-standalone-encounter",
    "permission-offline",
    "permission-patient",
    "permission-user",
]


@router.get("/.well-known/smart-configuration")
async def smart_configuration(request: Request) -> JSONResponse:
    """Return the SMART App Launch discovery document.

    Clients use this to discover OAuth2 endpoints and supported capabilities
    before initiating an authorization flow.
    """
    base_url = str(request.base_url).rstrip("/")
    issuer = os.environ.get("SMART_ISSUER", base_url)
    authorization_url = os.environ.get(
        "SMART_AUTHORIZATION_URL", f"{base_url}/oauth2/authorize"
    )
    token_url = os.environ.get("SMART_TOKEN_URL", f"{base_url}/oauth2/token")
    introspection_url = os.environ.get(
        "SMART_INTROSPECTION_URL", f"{base_url}/oauth2/introspect"
    )
    jwks_url = os.environ.get("SMART_JWKS_URL", f"{base_url}/.well-known/jwks.json")

    config = {
        "issuer": issuer,
        "authorization_endpoint": authorization_url,
        "token_endpoint": token_url,
        "introspection_endpoint": introspection_url,
        "jwks_uri": jwks_url,
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "scopes_supported": [
            "openid",
            "fhirUser",
            "profile",
            "launch",
            "launch/patient",
            "patient/*.*",
            "patient/*.read",
            "patient/*.write",
            "user/*.*",
            "user/*.read",
            "user/*.write",
            "offline_access",
        ],
        "response_types_supported": ["code"],
        "capabilities": _SMART_CAPABILITIES,
        "code_challenge_methods_supported": ["S256"],
    }
    return JSONResponse(content=config, media_type="application/json")


@router.post("/oauth2/introspect")
async def introspect_token(
    request: Request,
    token: str = Form(...),
) -> JSONResponse:
    """Introspect an OAuth2 / SMART bearer token (RFC 7662).

    If ``SMART_INTROSPECTION_URL`` is configured and differs from this
    server's own introspect endpoint, the request is proxied upstream.
    Otherwise a local check is performed:
    - token == MEDANON_API_KEY → active, scope ``user/*.*``
    - anything else            → ``{"active": false}``
    """
    upstream_url = os.environ.get("SMART_INTROSPECTION_URL", "").strip()
    own_introspect = f"{str(request.base_url).rstrip('/')}/oauth2/introspect"

    if upstream_url and upstream_url != own_introspect:
        return await _proxy_introspect(upstream_url, token)

    api_key = os.environ.get("MEDANON_API_KEY", "").strip()
    if api_key and hmac.compare_digest(token, api_key):
        return JSONResponse(
            content={"active": True, "scope": "user/*.*", "token_type": "bearer"}
        )

    return JSONResponse(content={"active": False})


def _sync_introspect(upstream_url: str, body: bytes) -> tuple[dict, int]:
    """Sync HTTP call for upstream token introspection (runs in thread pool)."""
    req = urllib.request.Request(
        upstream_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()), resp.status


async def _proxy_introspect(upstream_url: str, token: str) -> JSONResponse:
    """Proxy a token introspection request to an upstream OAuth2 server."""
    body = urllib.parse.urlencode({"token": token}).encode("utf-8")
    try:
        data, status = await asyncio.to_thread(_sync_introspect, upstream_url, body)
        return JSONResponse(content=data, status_code=status)
    except urllib.error.HTTPError as exc:
        _log.warning("smart_introspect_upstream_error status=%d", exc.code)
        raise HTTPException(
            status_code=502, detail=f"Upstream introspection server returned {exc.code}"
        )
    except Exception as exc:
        _log.warning("smart_introspect_upstream_failure: %s", type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Failed to reach upstream introspection server"
        )
