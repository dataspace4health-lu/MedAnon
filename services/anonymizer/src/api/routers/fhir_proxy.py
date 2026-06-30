"""Read-only FHIR proxy for user-supplied ("custom connection") servers.

The SPA can reach the built-in source / target HAPI servers directly through
nginx (``/fhir`` and ``/fhir-target``). To let the UI assess an *arbitrary*
FHIR endpoint (a partner site, an external R4 server) we proxy GET requests
through the backend so that:

- the browser is not blocked by CORS / the SPA's ``connect-src 'self'`` CSP;
- every user-supplied URL passes the same SSRF guard used elsewhere
  (``_validate_server_url`` — rejects private / loopback / link-local targets,
  so this endpoint cannot be turned into an internal port scanner).

Only GET is supported; the response body is size-capped. This is a thin pass-
through — the client drives FHIR pagination by passing successive ``next`` link
URLs as the ``url`` parameter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request

from fastapi import APIRouter, Header, HTTPException, Query

from api.deps import _validate_server_url

_log = logging.getLogger("fhir_proxy")

router = APIRouter(tags=["fhir-proxy"])

# Cap the upstream body we will buffer (a single FHIR page; _count keeps pages
# bounded). Guards against a hostile/huge upstream exhausting memory.
_MAX_BYTES = 64 * 1024 * 1024  # 64 MB


def _fetch(url: str, token: str | None) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/fhir+json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (scheme validated)
        raw = resp.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("upstream response exceeds 64 MB cap")
    return json.loads(raw.decode("utf-8"))


@router.get("/fhir-proxy")
async def fhir_proxy(
    url: str = Query(..., description="Absolute FHIR URL (base+path+query, or a next-link)"),
    x_fhir_token: str | None = Header(default=None),
) -> dict:
    """Proxy a single GET to a user-supplied FHIR server (SSRF-guarded)."""
    await _validate_server_url(url)
    try:
        return await asyncio.to_thread(_fetch, url, x_fhir_token)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.warning("fhir-proxy fetch failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"upstream fetch failed: {exc}") from exc
