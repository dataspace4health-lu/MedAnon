"""Data-permit governance endpoints (WS8b, D7.2 §2 / EHDS Arts 45-49).

Thin REST surface over :class:`pipeline.permits.PermitService`. All routes
require the ``admin`` role (data-governance is a privileged operation). The
lifecycle is state-machine enforced in the domain layer; illegal transitions
return HTTP 409.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.deps import limiter
from pipeline.permits import (
    PermitNotFoundError,
    PermitService,
    PermitTransitionError,
)

router = APIRouter()
logger = logging.getLogger("medanon")

_service = PermitService()


async def _json_body(request: Request) -> dict:
    import json

    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="body must be a JSON object")
    return data


def _actor(request: Request) -> str:
    return str(getattr(request.state, "principal", None) or "admin")


@router.get("/permits")
@limiter.limit("60/minute")
async def list_permits(request: Request):
    return JSONResponse(content=[p.to_dict() for p in _service.list()])


@router.post("/permits")
@limiter.limit("30/minute")
async def create_permit(request: Request):
    data = await _json_body(request)
    permit = _service.create(data)
    return JSONResponse(status_code=201, content=permit.to_dict())


@router.get("/permits/{permit_id}")
@limiter.limit("60/minute")
async def get_permit(permit_id: str, request: Request):
    try:
        return JSONResponse(content=_service.get(permit_id).to_dict())
    except PermitNotFoundError:
        raise HTTPException(status_code=404, detail=f"permit {permit_id} not found")


async def _transition(request: Request, permit_id: str, action: str):
    data = await _json_body(request)
    actor = _actor(request)
    try:
        if action == "submit":
            permit = _service.submit(permit_id)
        elif action == "approve":
            permit = _service.approve(permit_id, by=actor)
        elif action == "reject":
            permit = _service.reject(permit_id, by=actor, reason=data.get("reason", ""))
        elif action == "revoke":
            permit = _service.revoke(permit_id, by=actor, reason=data.get("reason", ""))
        else:  # pragma: no cover - guarded by route definitions
            raise HTTPException(status_code=400, detail="unknown action")
    except PermitNotFoundError:
        raise HTTPException(status_code=404, detail=f"permit {permit_id} not found")
    except PermitTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse(content=permit.to_dict())


@router.post("/permits/{permit_id}/submit")
@limiter.limit("30/minute")
async def submit_permit(permit_id: str, request: Request):
    return await _transition(request, permit_id, "submit")


@router.post("/permits/{permit_id}/approve")
@limiter.limit("30/minute")
async def approve_permit(permit_id: str, request: Request):
    return await _transition(request, permit_id, "approve")


@router.post("/permits/{permit_id}/reject")
@limiter.limit("30/minute")
async def reject_permit(permit_id: str, request: Request):
    return await _transition(request, permit_id, "reject")


@router.post("/permits/{permit_id}/revoke")
@limiter.limit("30/minute")
async def revoke_permit(permit_id: str, request: Request):
    return await _transition(request, permit_id, "revoke")
