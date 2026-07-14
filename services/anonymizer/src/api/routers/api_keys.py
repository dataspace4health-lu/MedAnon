"""Per-client API key management endpoints (admin-only).

POST   /v1/api-keys              → create  (201, returns raw key once)
GET    /v1/api-keys              → list    (metadata only, no raw keys)
GET    /v1/api-keys/{id}         → get one
DELETE /v1/api-keys/{id}         → revoke  (204)
POST   /v1/api-keys/{id}/rotate  → atomic rotate (201, returns new raw key)
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.schemas.api_keys import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyResponse,
)
from api.services.api_keys import (
    ApiKeyAlreadyRevoked,
    ApiKeyNotFound,
    ApiKeyService,
    ApiKeyStoreUnavailable,
)

router = APIRouter()
logger = logging.getLogger("medanon.api_keys_router")

_service = ApiKeyService()


def _require_admin(request: Request) -> None:
    from api.auth import AuthContext

    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin role required")


def _handle_store_unavailable(exc: ApiKeyStoreUnavailable) -> None:
    raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/api-keys", status_code=201)
async def create_api_key(body: ApiKeyCreateRequest, request: Request):
    """Create a new per-client API key.

    **The raw key is returned exactly once**  store it securely.
    Subsequent reads return only metadata (no key material).
    """
    _require_admin(request)
    try:
        key_id, raw_key, meta = await asyncio.to_thread(
            _service.create,
            client_id=body.client_id,
            role=body.role,
            expires_at=body.expires_at,
            description=body.description,
        )
    except ApiKeyStoreUnavailable as exc:
        _handle_store_unavailable(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(
        status_code=201,
        content=ApiKeyCreateResponse(raw_key=raw_key, **meta).model_dump(),
    )


@router.get("/api-keys")
async def list_api_keys(request: Request):
    """List all API keys (metadata only  no raw keys or hashes)."""
    _require_admin(request)
    try:
        keys = await asyncio.to_thread(_service.list_all)
    except ApiKeyStoreUnavailable as exc:
        _handle_store_unavailable(exc)
    return {"api_keys": [ApiKeyResponse(**k).model_dump() for k in keys]}


@router.get("/api-keys/{key_id}")
async def get_api_key(key_id: str, request: Request):
    """Get metadata for a specific API key."""
    _require_admin(request)
    try:
        meta = await asyncio.to_thread(_service.get, key_id)
    except ApiKeyStoreUnavailable as exc:
        _handle_store_unavailable(exc)
    except ApiKeyNotFound:
        raise HTTPException(status_code=404, detail=f"API key {key_id!r} not found")
    return ApiKeyResponse(**meta).model_dump()


@router.delete("/api-keys/{key_id}", status_code=204)
async def revoke_api_key(key_id: str, request: Request):
    """Revoke an API key immediately (zero-downtime; no service restart needed)."""
    _require_admin(request)
    try:
        await asyncio.to_thread(_service.revoke, key_id)
    except ApiKeyStoreUnavailable as exc:
        _handle_store_unavailable(exc)
    except ApiKeyNotFound:
        raise HTTPException(status_code=404, detail=f"API key {key_id!r} not found")
    except ApiKeyAlreadyRevoked:
        raise HTTPException(
            status_code=409, detail=f"API key {key_id!r} is already revoked"
        )


@router.post("/api-keys/{key_id}/rotate", status_code=201)
async def rotate_api_key(key_id: str, request: Request):
    """Atomically rotate an API key.

    Creates a replacement key with the same client_id and role, then revokes
    the old key in a single transaction.  Returns the new raw key (once only).
    """
    _require_admin(request)
    try:
        new_id, new_raw, meta = await asyncio.to_thread(_service.rotate, key_id)
    except ApiKeyStoreUnavailable as exc:
        _handle_store_unavailable(exc)
    except ApiKeyNotFound:
        raise HTTPException(status_code=404, detail=f"API key {key_id!r} not found")
    return JSONResponse(
        status_code=201,
        content=ApiKeyCreateResponse(raw_key=new_raw, **meta).model_dump(),
    )
