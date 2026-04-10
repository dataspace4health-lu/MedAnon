"""FHIR R4 Subscription CRUD endpoints.

POST   /fhir/Subscription       — create subscription
GET    /fhir/Subscription/{id}  — retrieve subscription
PUT    /fhir/Subscription/{id}  — update subscription
DELETE /fhir/Subscription/{id}  — delete subscription
GET    /fhir/Subscription       — list all (admin only)
"""

import asyncio
import logging

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from api.deps import limiter
from api.services.subscriptions import SubscriptionService, SubscriptionValidationError

router = APIRouter()
logger = logging.getLogger("medanon")
_service = SubscriptionService()


@router.post("/fhir/Subscription", status_code=201)
@limiter.limit("30/minute")
async def create_subscription(request: Request, sub: dict = Body(...)):
    """Create a FHIR R4 Subscription resource.

    Only 'rest-hook' channel type is supported.
    Returns 201 with a Location header pointing to the new resource.
    """
    try:
        result = await asyncio.to_thread(_service.create, sub)
    except SubscriptionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return JSONResponse(
        status_code=201,
        content=result,
        headers={"Location": f"/fhir/Subscription/{result['id']}"},
    )


@router.get("/fhir/Subscription/{sub_id}")
@limiter.limit("60/minute")
async def get_subscription(request: Request, sub_id: str):
    """Retrieve a FHIR R4 Subscription resource by id."""
    sub = await asyncio.to_thread(_service.get, sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


@router.put("/fhir/Subscription/{sub_id}")
@limiter.limit("30/minute")
async def update_subscription(request: Request, sub_id: str, sub: dict = Body(...)):
    """Replace a FHIR R4 Subscription resource by id."""
    try:
        result = await asyncio.to_thread(_service.update, sub_id, sub)
    except SubscriptionValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return result


@router.delete("/fhir/Subscription/{sub_id}", status_code=204)
@limiter.limit("30/minute")
async def delete_subscription(request: Request, sub_id: str):
    """Delete a FHIR R4 Subscription resource by id."""
    deleted = await asyncio.to_thread(_service.delete, sub_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return Response(status_code=204)


@router.get("/fhir/Subscription")
@limiter.limit("30/minute")
async def list_subscriptions(request: Request):
    """List all subscriptions (any status). Admin role required (enforced by auth middleware)."""
    try:
        return await asyncio.to_thread(_service.list_all)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
