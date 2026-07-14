"""Read-only FHIR proxy for user-supplied ("custom connection") servers.

The SPA can reach the built-in source / target HAPI servers directly through
nginx (``/fhir`` and ``/fhir-target``). To let the UI assess an *arbitrary*
FHIR endpoint (a partner site, an external R4 server) we proxy GET requests
through the backend so that:

- the browser is not blocked by CORS / the SPA's ``connect-src 'self'`` CSP;
- every user-supplied URL passes the same SSRF guard used elsewhere
  (``_validate_server_url``  rejects private / loopback / link-local targets,
  so this endpoint cannot be turned into an internal port scanner).

Only GET is supported; the response body is size-capped. This is a thin pass-
through  the client drives FHIR pagination by passing successive ``next`` link
URLs as the ``url`` parameter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from urllib.parse import urlsplit

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


def _same_origin(a: str, b: str) -> bool:
    """True when two URLs share scheme + host + port (case-insensitive host)."""
    pa, pb = urlsplit(a), urlsplit(b)
    return (
        pa.scheme.lower() == pb.scheme.lower()
        and (pa.hostname or "").lower() == (pb.hostname or "").lower()
        and pa.port == pb.port
    )


def _resolve_saved_source(
    source_id: str, url: str | None, path: str | None
) -> tuple[str, str | None]:
    """Resolve a saved input source to (effective_url, decrypted_token).

    The bearer token is decrypted here, server-side, and never returned to the
    browser. An absolute ``url`` (a paginated next-link) is honoured only when it
    shares the saved server's origin  this prevents the caller from aiming the
    saved credential at an arbitrary host. Otherwise the effective URL is the
    saved base plus the relative ``path``.
    """
    from integrations.sql_source.secrets import decrypt_secret
    from pipeline.connectors import get_source_store

    store = get_source_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="Input-source store not initialised (set MEDANON_APP_DB_URL).",
        )
    meta = store.get(source_id)
    if meta is None:
        raise HTTPException(
            status_code=404, detail=f"Input source not found: {source_id}"
        )

    base = (meta.get("server_url") or "").rstrip("/")
    if url:
        if not _same_origin(url, base):
            raise HTTPException(
                status_code=400,
                detail="next-link host does not match the saved source server",
            )
        effective = url
    else:
        rel = path or "/metadata"
        if not rel.startswith("/"):
            rel = "/" + rel
        effective = base + rel

    enc = store.get_encrypted_token(source_id)
    token = decrypt_secret(enc) if enc else None
    return effective, token


@router.get("/fhir-proxy")
async def fhir_proxy(
    url: str | None = Query(
        default=None,
        description="Absolute FHIR URL (base+path+query, or a next-link). "
        "Required unless source_id is given.",
    ),
    source_id: str | None = Query(
        default=None,
        description="Saved input-source id. Its base URL and (encrypted) bearer "
        "token are resolved server-side; the token never reaches the browser.",
    ),
    path: str | None = Query(
        default=None,
        description="Relative FHIR path used with source_id, e.g. '/Patient?_count=50'.",
    ),
    x_fhir_token: str | None = Header(default=None),
) -> dict:
    """Proxy a single GET to a source FHIR server (SSRF-guarded).

    Two modes: ``source_id`` resolves a saved source's URL + server-side token;
    otherwise ``url`` is a caller-supplied server with an optional ``X-FHIR-Token``.
    """
    if source_id:
        effective_url, token = _resolve_saved_source(source_id, url, path)
    elif url:
        effective_url, token = url, x_fhir_token
    else:
        raise HTTPException(
            status_code=422, detail="either 'url' or 'source_id' is required"
        )

    await _validate_server_url(effective_url)
    try:
        return await asyncio.to_thread(_fetch, effective_url, token)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.warning("fhir-proxy fetch failed for %s: %s", effective_url, exc)
        raise HTTPException(
            status_code=502, detail=f"upstream fetch failed: {exc}"
        ) from exc
